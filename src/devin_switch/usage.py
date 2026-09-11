"""Read account quotas from the same status service used by the installed client."""

import hashlib
import json
import math
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace

from devin_switch.store import Account, Store, SwitchError, write_json

STATUS_URL = "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus"
REFRESH_SECONDS = 60
STALE_SECONDS = 120
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class Window:
    used_percent: float | None = None
    resets_at: float | None = None
    state: str = "unavailable"


@dataclass(frozen=True)
class Usage:
    status: str = "unavailable"
    daily: Window = Window()
    weekly: Window = Window()
    email: str | None = None
    plan: str | None = None
    fetched_at: float | None = None
    checked_at: float | None = None
    message: str = "Refresh to read usage."


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward saved credentials to a redirected destination.
        return None


def credential_bytes(store: Store, account: Account) -> bytes:
    path = store.credentials(account)
    if path.is_symlink():
        raise SwitchError("Sign in again: the saved login is not a regular file.")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def quota_window(plan: dict, info: dict, period: str) -> Window:
    if info.get(f"hide{period.title()}Quota") is True:
        return Window(state="not_applicable")
    reset = finite_number(plan.get(f"{period}QuotaResetAtUnix", 0))
    if not 0 < reset <= 253402300799:
        return Window()
    # Connect uses proto3 JSON: scalar zeroes are omitted. A recognized quota plan
    # and a reset timestamp establish that an omitted remaining-percent means 0%.
    remaining = finite_number(plan.get(f"{period}QuotaRemainingPercent", 0))
    if not 0 <= remaining <= 100:
        raise ValueError("Invalid quota percentage")
    return Window(used_percent=100 - remaining, resets_at=reset, state="available")


def parse_status(payload: object, now: float) -> Usage:
    if not isinstance(payload, dict) or not isinstance(payload.get("userStatus"), dict):
        raise ValueError("Missing user status")
    user = payload["userStatus"]
    plan = user.get("planStatus")
    if not isinstance(plan, dict) or not isinstance(plan.get("planInfo"), dict):
        raise ValueError("Missing plan status")
    info = plan["planInfo"]
    email = user.get("email")
    name = info.get("planName")
    if (email is not None and not isinstance(email, str)) or (
        name is not None and not isinstance(name, str)
    ):
        raise ValueError("Invalid account identity")
    if info.get("billingStrategy") not in ("BILLING_STRATEGY_QUOTA", 2):
        return Usage(
            email=email,
            plan=name,
            fetched_at=now,
            checked_at=now,
            message="This plan does not report daily or weekly quotas.",
        )
    daily = quota_window(plan, info, "daily")
    weekly = quota_window(plan, info, "weekly")
    complete = all(window.state != "unavailable" for window in (daily, weekly))
    return Usage(
        status="ok" if complete else "unavailable",
        daily=daily,
        weekly=weekly,
        email=email,
        plan=name,
        fetched_at=now,
        checked_at=now,
        message="" if complete else "Devin did not report every quota window.",
    )


def fetch_status(credentials: bytes) -> object:
    try:
        data = tomllib.loads(credentials.decode())
        key = data.get("windsurf_api_key")
        if not isinstance(key, str) or not key:
            raise ValueError("Missing credential")
    except (ValueError, UnicodeError) as exc:
        raise SwitchError("Sign in again to read usage.") from exc
    if data.get("api_server_url", "https://server.codeium.com") != "https://server.codeium.com":
        raise SwitchError("Usage is unavailable for this custom API server.")
    request = urllib.request.Request(
        STATUS_URL,
        data=json.dumps(
            {
                "metadata": {
                    "apiKey": key,
                    "ideName": "devin-switch",
                    "ideVersion": "0.3.0",
                    "extensionName": "devin-switch",
                    "extensionVersion": "0.3.0",
                }
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirects()).open(request, timeout=15) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise SwitchError("Devin returned an oversized usage response.")
            return json.loads(content)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SwitchError("Sign in again to read usage.") from None
        if exc.code == 429:
            raise SwitchError(
                "Usage requests are temporarily rate limited. Try again later."
            ) from None
        raise SwitchError(f"Usage request failed (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SwitchError("Could not reach Devin. Check your connection and refresh.") from None


def read_cached(store: Store, account: Account, *, now: float | None = None) -> Usage:
    now = time.time() if now is None else now
    try:
        credentials = credential_bytes(store, account)
        if not credentials:
            return Usage(status="sign_in", message="Sign in to see usage.")
        cache = json.loads((store.directory(account.name) / "usage.json").read_text())
        if cache["credential_fingerprint"] != fingerprint(credentials):
            return Usage()
        record = cache["usage"]
        usage = Usage(
            **{**record, "daily": Window(**record["daily"]), "weekly": Window(**record["weekly"])}
        )
        if usage.checked_at is not None:
            finite_number(usage.checked_at)
        if usage.fetched_at is not None and (
            now - finite_number(usage.fetched_at) > STALE_SECONDS
            or any(
                w.resets_at is not None and finite_number(w.resets_at) <= now
                for w in (usage.daily, usage.weekly)
            )
        ):
            return replace(
                usage, status="stale", message=usage.message or "Refresh for current usage."
            )
        return usage
    except SwitchError as exc:
        return Usage(status="sign_in", message=str(exc))
    except (OSError, ValueError, TypeError, KeyError):
        return Usage()


def refresh_account(store: Store, account: Account, *, force: bool = False) -> Usage:
    now = time.time()
    previous = read_cached(store, account, now=now)
    if (
        not force
        and previous.checked_at is not None
        and 0 <= now - previous.checked_at < REFRESH_SECONDS
    ):
        return previous
    try:
        credentials = credential_bytes(store, account)
    except (SwitchError, OSError):
        return Usage(status="sign_in", message="Sign in again to read usage.")
    if not credentials:
        return Usage(status="sign_in", message="Sign in to see usage.")
    try:
        try:
            result = parse_status(fetch_status(credentials), now)
        except (ValueError, TypeError, KeyError):
            raise SwitchError("Devin returned an unreadable usage response.") from None
    except SwitchError as exc:
        result = replace(
            previous,
            status="stale" if previous.fetched_at else "unavailable",
            checked_at=now,
            message=str(exc),
        )
    # A sign-in may have completed while the read-only request was running.
    # Never attribute the previous credential's response to the newly saved login.
    try:
        current = credential_bytes(store, account)
        if current and current == credentials:
            write_json(
                store.directory(account.name) / "usage.json",
                {"credential_fingerprint": fingerprint(current), "usage": asdict(result)},
            )
        else:
            return Usage(message="The saved login changed. Refresh usage.")
    except (OSError, SwitchError):
        return replace(result, message="Usage loaded, but its local cache could not be saved.")
    return result


def refresh_all(store: Store, *, force: bool = False) -> None:
    def refresh(account: Account) -> None:
        refresh_account(store, account, force=force)

    with store.lock("usage.lock"), ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(refresh, store.accounts()))
