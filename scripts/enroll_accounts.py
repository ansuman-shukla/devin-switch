"""Sign new plus-address Devin accounts into Devin Switch through a local Chrome.

Run from the repository (see docs/enrolling-accounts.md):

    uv run --locked --with playwright python scripts/enroll_accounts.py \
        --email you@gmail.com --first 27 --last 52 --first-alias 53

For every number N in [--first, --last] this:

1. registers `<prefix>-<alias>` with `ds add`, unless it already exists;
2. runs `ds login --no-browser` in a private PTY and reads the one-time sign-in URL;
3. opens that URL in a fresh context of a separate headless Chrome, signs up (or logs in)
   as `you+N@gmail.com`, and reads Devin's emailed code from the signed-in
   Gmail tab, checking the recipient;
4. picks the organization with the requested plan, then types the CLI code
   straight into the waiting `ds login` prompt. Codes are never printed;
5. confirms with Devin's usage endpoint that the alias reports the expected
   email and plan.

Only the Gmail tab of your Chrome is used; no other window opens.

It never purchases anything and stops at the first unexpected page.
"""

import argparse
import asyncio
import fcntl
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

CHROME_DATA = Path.home() / "Library/Application Support/Google/Chrome"
LOGIN_URL = re.compile(r"https://app\.devin\.ai/auth/cli/continue\?\S*?cli_pkce_marker=1")
ANY_LOGIN_URL = re.compile(r"https://\S*?cli_pkce_marker=1")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
LONG_VALUE = re.compile(r"[A-Za-z0-9_\-\.]{20,}")
CODE_SUBJECT = 'subject:"Devin Login Code" in:anywhere newer_than:15m'
TERMS = re.compile("agree to the Terms of Service")
RATE_LIMIT_WAIT = 300
OTHER_LOGIN_RETRIES = 3


class EnrollError(Exception):
    pass


class RateLimited(EnrollError):
    pass


class OtherLogin(EnrollError):
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


def flat_text(output: bytes) -> str:
    return re.sub(rb"[\r\n]", b"", ANSI.sub(b"", output)).decode(errors="ignore")


def login_url(output: bytes) -> str | None:
    match = LOGIN_URL.search(flat_text(output))
    return match.group(0) if match else None


def other_login_url(output: bytes) -> bool:
    return login_url(output) is None and bool(ANY_LOGIN_URL.search(flat_text(output)))


def redact(text: str) -> str:
    return LONG_VALUE.sub("<redacted>", text)


def store():
    from devin_switch.store import Store

    root = os.environ.get("DS_HOME", "~/.local/share/devin-switch")
    return Store(Path(root).expanduser().resolve())


def ensure_accounts(plan: list[Target], ds: str) -> None:
    existing = {account.name: account for account in store().accounts()}
    for target in plan:
        account = existing.get(target.alias)
        if account and account.chrome_profile not in (None, target.profile):
            raise EnrollError(
                f"{target.alias} exists but uses Chrome profile {account.chrome_profile!r}, "
                f"not {target.profile!r}. Choose another --first-alias."
            )
    for target in plan:
        if target.alias not in existing:
            subprocess.run((ds, "add", target.alias), check=True)


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
            (ds, "login", alias, "--no-browser"),
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

    def finish(self, seconds: float) -> int | None:
        end = time.time() + seconds
        while self.process.poll() is None and time.time() < end:
            self.pump(1)
        self.pump(0.5)
        return self.process.poll()

    def text(self) -> str:
        return ANSI.sub(b"", self.output).decode(errors="ignore")

    async def wait_url(self) -> str:
        loop = asyncio.get_running_loop()
        for _ in range(30):
            await loop.run_in_executor(None, self.pump, 1)
            if url := login_url(self.output):
                return url
            if other_login_url(self.output):
                raise OtherLogin("ds login offered a non-Devin sign-in page")
            if "already has a saved login" in self.text() or self.process.poll() is not None:
                break
        raise EnrollError("ds login did not print a sign-in URL: " + redact(self.text())[-300:])

    async def submit(self, code: str) -> str:
        os.write(self.master, code.encode())
        await asyncio.sleep(0.5)
        os.write(self.master, b"\r")
        status = await asyncio.get_running_loop().run_in_executor(None, self.finish, 80)
        last = redact(self.text()).strip().splitlines()[-1:] or [""]
        if status is None:
            raise EnrollError(f"ds login did not finish after the code: {last[0][:200]}")
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
        from devin_switch import chrome_access

        port, path = chrome_access.read_text(
            CHROME_DATA, "DevToolsActivePort", "Connect the enroll script to Chrome to read Gmail"
        ).split()
        print("Connecting to Chrome. Approve the remote debugging prompt if it appears.")
        self.browser = await playwright.chromium.connect_over_cdp(
            f"ws://127.0.0.1:{port}{path}", timeout=300_000
        )
        seen = []
        for index, context in enumerate(self.browser.contexts):
            for page in context.pages:
                if page.url.startswith("https://mail.google.com/"):
                    title = await page.title()
                    seen.append(f"context {index}: {title[:80]!r}")
                    if self.args.email.lower() in title.lower():
                        self.gmail = page
                        return
        page = await self.browser.contexts[0].new_page()
        await page.goto(f"https://mail.google.com/mail/?authuser={self.args.email}", timeout=60_000)
        await page.wait_for_timeout(8000)
        title = await page.title()
        if page.url.startswith("https://mail.google.com/") and (
            self.args.email.lower() in title.lower()
        ):
            self.gmail = page
            return
        seen.append(f"opened: {title[:80]!r} at {page.url.split('?')[0][:60]}")
        print(
            f"  contexts: {len(self.browser.contexts)}, "
            f"pages: {sum(len(c.pages) for c in self.browser.contexts)}, "
            f"gmail tabs: {seen or 'none'}"
        )
        raise EnrollError(
            f"Open Gmail signed in as {self.args.email} in the Chrome profile where "
            "remote debugging is enabled, then retry."
        )

    async def launch(self, playwright) -> None:
        """A separate headless Chrome for Devin's pages, so nothing opens on screen."""
        self.playwright = playwright
        self.devin = await playwright.chromium.launch(channel="chrome", headless=True)

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
        if signup:
            await page.get_by_role("checkbox", name=TERMS).check()
        await page.get_by_role("button", name="Sign up" if signup else "Log in", exact=True).click()
        await page.wait_for_timeout(4000)
        body = await page.inner_text("body")
        if "Rate limit" in body:
            raise RateLimited("Devin rate limit")
        return "We've sent" in body

    async def cli_code(self, target: Target, url: str) -> str:
        if not self.devin.is_connected():
            print("  Devin's headless Chrome closed; relaunching", flush=True)
            await self.launch(self.playwright)
        context = await self.devin.new_context()
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

    async def enroll(self, target: Target) -> str:
        waits = others = 0
        while True:
            login = Login(self.args.ds, target.alias)
            try:
                url = await login.wait_url()
                await login.submit(await self.cli_code(target, url))
                return verify(target, self.args.plan)
            except RateLimited:
                waits += 1
                if waits > self.args.rate_limit_retries:
                    raise EnrollError(
                        "Still rate limited; rerun later. Finished accounts are skipped."
                    ) from None
                print(f"  rate limited; waiting {RATE_LIMIT_WAIT // 60} minutes", flush=True)
                delay = RATE_LIMIT_WAIT
            except OtherLogin:
                others += 1
                if others > OTHER_LOGIN_RETRIES:
                    raise EnrollError(
                        "ds login keeps offering a non-Devin sign-in page; rerun later."
                    ) from None
                print("  ds login offered a non-Devin sign-in page; retrying", flush=True)
                delay = 5
            finally:
                login.close()
            await asyncio.sleep(delay)


def signed_in(alias: str) -> bool:
    from devin_switch.native import Native, find_binary

    return Native(store(), find_binary()).authenticated(store().account(alias))


async def run(args: argparse.Namespace, plan: list[Target]) -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        enroller = Enroller(args)
        await enroller.connect(playwright)
        await enroller.launch(playwright)
        pending = list(plan)
        for attempt in range(1, args.passes + 1):
            failed = []
            for target in pending:
                try:
                    if signed_in(target.alias):
                        print(
                            f"{target.alias}: already signed in; {verify(target, args.plan)}",
                            flush=True,
                        )
                        continue
                    print(f"{target.alias} <- {target.email}", flush=True)
                    print(f"  ok: {await enroller.enroll(target)}", flush=True)
                # Broad on purpose: a restart needs a new Chrome approval, so keep the connection.
                except Exception as exc:  # noqa: BLE001
                    if not enroller.browser.is_connected():
                        raise
                    print(f"  failed (pass {attempt}): {redact(str(exc))[:300]}", flush=True)
                    failed.append(target)
                await asyncio.sleep(args.pause)
            if not failed:
                print("ALL DONE", flush=True)
                return 0
            pending = failed
            names = ", ".join(t.alias for t in failed)
            print(f"pass {attempt} left {len(failed)}: {names}; next pass in {args.pass_wait}s")
            await asyncio.sleep(args.pass_wait)
    print("GAVE UP on: " + ", ".join(t.alias for t in pending), flush=True)
    return 1


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
    result.add_argument("--passes", type=int, default=1, help="Passes over failed accounts")
    result.add_argument("--pass-wait", type=int, default=600, help="Seconds between passes")
    result.add_argument("--ds", default="ds", help="Path to the ds command")
    result.add_argument("--dry-run", action="store_true", help="Print the mapping and exit")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        plan = targets(args)
        for target in plan:
            print(f"{target.alias:<14} {target.email}")
        if args.dry_run:
            return 0
        ensure_accounts(plan, args.ds)
        return asyncio.run(run(args, plan))
    except EnrollError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
