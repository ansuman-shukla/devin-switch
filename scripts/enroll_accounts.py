"""Sign new plus-address Devin accounts into Devin Switch through a local Chrome.

Run from the repository (see docs/enrolling-accounts.md):

    uv run --locked --with playwright python scripts/enroll_accounts.py \
        --email you@gmail.com --first 27 --last 52 --first-alias 53

For every number N in [--first, --last] this:

1. creates a Chrome profile folder "Plus N" (no Google account) and registers
   `<prefix>-<alias>` bound to it with `ds add`, unless it already exists;
2. runs `ds login` in a private PTY and reads the one-time sign-in URL;
3. opens that URL in a fresh, isolated browser context, signs up (or logs in)
   as `you+N@gmail.com`, and reads Devin's emailed code from the signed-in
   Gmail tab, checking the recipient;
4. picks the organization with the requested plan, then types the CLI code
   straight into the waiting `ds login` prompt. Codes are never printed;
5. confirms with Devin's usage endpoint that the alias reports the expected
   email and plan, and closes leftover Devin sign-in windows.

It never purchases anything and stops at the first unexpected page.
"""

import argparse
import asyncio
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path

CHROME_APP = "/Applications/Google Chrome.app"
CHROME_DATA = Path.home() / "Library/Application Support/Google/Chrome"
LOGIN_URL = re.compile(r"https://app\.devin\.ai/auth/cli/continue\?\S*?cli_pkce_marker=1")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
LONG_VALUE = re.compile(r"[A-Za-z0-9_\-\.]{20,}")
CODE_SUBJECT = 'subject:"Devin Login Code" in:anywhere newer_than:15m'
RATE_LIMIT_WAIT = 300
CLEANUP_SCRIPT = """
on run keep
  set closed to 0
  tell application "Google Chrome"
    set ids to id of every window
    repeat with wid in ids
      set w to window id (contents of wid)
      set stale to true
      repeat with t in (every tab of w)
        set u to URL of t
        if not (u starts with "https://app.devin.ai/auth/") then set stale to false
        if keep contains u then set stale to false
      end repeat
      if stale then
        close w
        set closed to closed + 1
      end if
    end repeat
  end tell
  return closed
end run
"""


class EnrollError(Exception):
    pass


class RateLimited(EnrollError):
    pass


@dataclass(frozen=True)
class Target:
    number: int
    alias: str
    email: str
    profile: str


def plus_address(base: str, number: int) -> str:
    local, at, domain = base.partition("@")
    if not at or not local or not domain or "+" in local:
        raise EnrollError(f"Expected a plain address like you@gmail.com, got {base!r}.")
    return f"{local}+{number}@{domain}"


def targets(args: argparse.Namespace) -> list[Target]:
    if args.last < args.first:
        raise EnrollError("--last must be at least --first.")
    return [
        Target(
            number,
            f"{args.prefix}-{args.first_alias + number - args.first}",
            plus_address(args.email, number),
            f"{args.profile_prefix} {number}",
        )
        for number in range(args.first, args.last + 1)
    ]


def login_url(output: bytes) -> str | None:
    text = re.sub(rb"[\r\n]", b"", ANSI.sub(b"", output)).decode(errors="ignore")
    match = LOGIN_URL.search(text)
    return match.group(0) if match else None


def redact(text: str) -> str:
    return LONG_VALUE.sub("<redacted>", text)


def chrome_profiles() -> dict:
    try:
        return json.loads((CHROME_DATA / "Local State").read_text())["profile"]["info_cache"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return {}


def store():
    from devin_switch.store import Store

    root = os.environ.get("DS_HOME", "~/.local/share/devin-switch")
    return Store(Path(root).expanduser().resolve())


def ensure_accounts(plan: list[Target], ds: str) -> None:
    existing = {account.name: account for account in store().accounts()}
    for target in plan:
        account = existing.get(target.alias)
        if account and account.chrome_profile != target.profile:
            raise EnrollError(
                f"{target.alias} exists but uses Chrome profile {account.chrome_profile!r}, "
                f"not {target.profile!r}. Choose another --first-alias."
            )
    for target in plan:
        if target.profile not in chrome_profiles():
            subprocess.run(
                (
                    "/usr/bin/open",
                    "-na",
                    CHROME_APP,
                    "--args",
                    f"--profile-directory={target.profile}",
                    "about:blank",
                ),
                check=True,
            )
            time.sleep(2)
    deadline = time.time() + 90
    while missing := [t.profile for t in plan if t.profile not in chrome_profiles()]:
        if time.time() > deadline:
            raise EnrollError(f"Chrome did not register profiles: {', '.join(missing)}")
        time.sleep(3)
    for target in plan:
        if target.alias not in existing:
            subprocess.run(
                (ds, "add", target.alias, "--chrome-profile", target.profile), check=True
            )


def verify(target: Target, plan_name: str) -> str:
    from devin_switch import usage

    for _ in range(5):
        reading = usage.refresh_account(store(), store().account(target.alias), force=True)
        if reading.status == "ok":
            break
        time.sleep(3)
    if reading.email != target.email or reading.plan != plan_name:
        raise EnrollError(
            f"{target.alias} reports {reading.email} on {reading.plan!r} "
            f"(expected {target.email} on {plan_name!r}): {reading.message}"
        )
    return f"{reading.email} on {reading.plan}"


class Login:
    """A `ds login` process on a private PTY so the code never reaches a terminal."""

    def __init__(self, ds: str, alias: str) -> None:
        self.master, child = pty.openpty()
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
        self.process = subprocess.Popen(
            (ds, "login", alias),
            stdin=child,
            stdout=child,
            stderr=child,
            env=dict(os.environ, COLUMNS="1000"),
            start_new_session=True,
        )
        os.close(child)
        self.output = b""

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.master], [], [], 0.3)
            if ready:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError:
                    return

    def text(self) -> str:
        return ANSI.sub(b"", self.output).decode(errors="ignore")

    async def wait_url(self) -> str:
        loop = asyncio.get_running_loop()
        for _ in range(30):
            await loop.run_in_executor(None, self.pump, 1)
            if url := login_url(self.output):
                return url
            if "already has a saved login" in self.text() or self.process.poll() is not None:
                break
        raise EnrollError("ds login did not print a sign-in URL: " + redact(self.text())[-300:])

    async def submit(self, code: str) -> str:
        os.write(self.master, code.encode())
        await asyncio.sleep(0.5)
        os.write(self.master, b"\r")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.pump, 20)
        status = await loop.run_in_executor(None, lambda: self.process.wait(timeout=60))
        last = redact(self.text()).strip().splitlines()[-1:] or [""]
        if status:
            raise EnrollError(f"ds login exited {status}: {last[0][:200]}")
        return last[0]

    def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
        os.close(self.master)


class Enroller:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.used_codes: set[str] = set()

    async def connect(self, playwright) -> None:
        port, path = (CHROME_DATA / "DevToolsActivePort").read_text().split()
        print("Connecting to Chrome. Approve the remote debugging prompt if it appears.")
        self.browser = await playwright.chromium.connect_over_cdp(
            f"ws://127.0.0.1:{port}{path}", timeout=300_000
        )
        main = self.browser.contexts[0]
        for page in main.pages:
            if page.url.startswith("https://mail.google.com/") and (
                self.args.email.lower() in (await page.title()).lower()
            ):
                self.gmail = page
                return
        raise EnrollError(
            f"Open Gmail signed in as {self.args.email} in the Chrome profile where "
            "remote debugging is enabled, then retry."
        )

    async def search(self, query: str) -> None:
        await self.gmail.evaluate("location.hash = '#inbox'")
        await self.gmail.wait_for_timeout(1500)
        await self.gmail.evaluate("q => location.hash = '#search/' + encodeURIComponent(q)", query)
        await self.gmail.wait_for_timeout(4000)

    async def email_code(self, email: str) -> str:
        for _ in range(12):
            await self.search(CODE_SUBJECT)
            rows = self.gmail.locator("tr.zA:visible")
            if await rows.count():
                await rows.first.click()
                await self.gmail.wait_for_timeout(3000)
                recipients = self.gmail.locator("span.g2")
                recipient = (
                    await recipients.last.get_attribute("email") if await recipients.count() else ""
                )
                body = await self.gmail.locator("div.a3s").last.inner_text()
                match = re.search(r"\b(\d{6})\b", body)
                if (
                    match
                    and (recipient or "").lower() == email.lower()
                    and match.group(1) not in self.used_codes
                ):
                    self.used_codes.add(match.group(1))
                    await self.gmail.evaluate("location.hash = '#inbox'")
                    return match.group(1)
            await self.gmail.wait_for_timeout(5000)
        raise EnrollError(f"No Devin code arrived for {email} (checked Inbox and Spam).")

    async def request_code(self, page, url: str, email: str, signup: bool) -> bool:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        if "/auth/login" not in page.url:
            return False
        if signup:
            await (
                page.get_by_role("link", name="Sign up")
                .or_(page.get_by_role("button", name="Sign up"))
                .first.click()
            )
            await page.wait_for_timeout(2500)
        await page.get_by_label("Email address").fill(email)
        await page.get_by_role("button", name="Sign up" if signup else "Log in", exact=True).click()
        await page.wait_for_timeout(4000)
        body = await page.inner_text("body")
        if "Rate limit" in body:
            raise RateLimited("Devin rate limit")
        return "We've sent" in body

    async def cli_code(self, target: Target, url: str) -> str:
        context = await self.browser.new_context()
        try:
            page = await context.new_page()
            sent = await self.request_code(page, url, target.email, signup=True)
            if not sent and "/auth/login" in page.url:
                sent = await self.request_code(page, url, target.email, signup=False)
            if sent:
                await page.get_by_label("Verification code").fill(
                    await self.email_code(target.email)
                )
                await page.wait_for_timeout(6000)
            if "/auth/cli/continue" not in page.url:
                await page.goto(url, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(2000)
            body = await page.inner_text("body")
            if "Copy your login code" not in body:
                if f"Signed in as {target.email}" not in body:
                    raise EnrollError("Unexpected page: " + redact(body)[:300])
                await page.get_by_text(self.args.plan, exact=True).first.click()
                await page.wait_for_timeout(5000)
                body = await page.inner_text("body")
            if "Copy your login code" not in body:
                raise EnrollError("No login code page: " + redact(body)[:300])
            values = LONG_VALUE.findall(body)
            if len(values) != 1:
                raise EnrollError(f"Expected one login code on the page, found {len(values)}.")
            return values[0]
        finally:
            await context.close()

    def cleanup(self) -> None:
        keep = sorted({page.url for c in self.browser.contexts[1:] for page in c.pages})
        subprocess.run(("osascript", "-e", CLEANUP_SCRIPT, *keep), capture_output=True)

    async def enroll(self, target: Target) -> str:
        for _ in range(self.args.rate_limit_retries + 1):
            login = Login(self.args.ds, target.alias)
            try:
                url = await login.wait_url()
                await login.submit(await self.cli_code(target, url))
                return verify(target, self.args.plan)
            except RateLimited:
                print(f"  rate limited; waiting {RATE_LIMIT_WAIT // 60} minutes", flush=True)
            finally:
                login.close()
                self.cleanup()
            await asyncio.sleep(RATE_LIMIT_WAIT)
        raise EnrollError("Still rate limited; rerun later. Finished accounts are skipped.")


def signed_in(alias: str) -> bool:
    from devin_switch.native import Native, find_binary

    return Native(store(), find_binary()).authenticated(store().account(alias))


async def run(args: argparse.Namespace, plan: list[Target]) -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        enroller = Enroller(args)
        await enroller.connect(playwright)
        for target in plan:
            if signed_in(target.alias):
                print(f"{target.alias}: already signed in; {verify(target, args.plan)}")
                continue
            print(f"{target.alias} <- {target.email}", flush=True)
            print(f"  ok: {await enroller.enroll(target)}", flush=True)
            await asyncio.sleep(args.pause)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    result.add_argument("--email", required=True, help="Base Gmail address, e.g. you@gmail.com")
    result.add_argument("--first", type=int, required=True, help="First +N number")
    result.add_argument("--last", type=int, required=True, help="Last +N number (inclusive)")
    result.add_argument(
        "--first-alias", type=int, required=True, help="Alias number used for --first"
    )
    result.add_argument("--prefix", default="ansuman", help="Alias prefix (default: ansuman)")
    result.add_argument("--profile-prefix", default="Plus", help="Chrome profile folder prefix")
    result.add_argument("--plan", default="Teams", help="Organization plan to select and verify")
    result.add_argument("--pause", type=int, default=45, help="Seconds between accounts")
    result.add_argument("--rate-limit-retries", type=int, default=8)
    result.add_argument("--ds", default="ds", help="Path to the ds command")
    result.add_argument("--dry-run", action="store_true", help="Print the mapping and exit")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        plan = targets(args)
        for target in plan:
            print(f"{target.alias:<14} {target.profile:<10} {target.email}")
        if args.dry_run:
            return 0
        ensure_accounts(plan, args.ds)
        return asyncio.run(run(args, plan))
    except EnrollError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
