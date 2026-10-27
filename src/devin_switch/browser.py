"""Use existing Chrome profiles without reading cookies or passwords."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devin_switch import chrome_access
from devin_switch.chrome_access import ChromeBlocked
from devin_switch.store import SwitchError

CHROME_DATA = Path(
    os.environ.get("DS_CHROME_DATA") or Path.home() / "Library/Application Support/Google/Chrome"
)
CHROME_APP = Path("/Applications/Google Chrome.app")
MANUAL_LOGIN_URL = "https://app.devin.ai/auth/cli/token"


@dataclass(frozen=True)
class ChromeProfile:
    directory: str
    name: str
    email: str


BLOCKED = "macOS lets only Chrome and apps with Full Disk Access read Chrome's profiles."


def profiles(reason: str | None = None) -> tuple[ChromeProfile, ...]:
    """Chrome's profile index. When macOS blocks it, ask with a popup only if a reason is given."""
    try:
        try:
            text = (CHROME_DATA / "Local State").read_text()
        except PermissionError as exc:
            if reason is None:
                raise ChromeBlocked(BLOCKED) from exc
            text = chrome_access.read_text(CHROME_DATA, "Local State", reason)
        cache = json.loads(text)["profile"]["info_cache"]
        return tuple(
            ChromeProfile(directory, value.get("name", ""), value.get("user_name", ""))
            for directory, value in sorted(cache.items())
        )
    except FileNotFoundError as exc:
        raise SwitchError("No local Google Chrome profiles found.") from exc
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as exc:
        raise SwitchError("Chrome's profile index could not be read.") from exc


def has_profile(directory: str) -> bool:
    """Whether Chrome has this profile; macOS still lets anyone see its folder exists."""
    try:
        return directory in {item.directory for item in profiles()}
    except ChromeBlocked:
        plain = directory not in ("", ".", "..") and "/" not in directory
        return plain and (CHROME_DATA / directory).is_dir()


def open_login(profile: str) -> None:
    if not has_profile(profile):
        raise SwitchError(f"Chrome profile {profile!r} is missing. Run: ds profiles")
    if not CHROME_APP.is_dir():
        raise SwitchError("Google Chrome is not installed in /Applications.")
    subprocess.run(
        (
            "/usr/bin/open",
            "-na",
            str(CHROME_APP),
            "--args",
            f"--profile-directory={profile}",
            MANUAL_LOGIN_URL,
        ),
        check=True,
        timeout=15,
    )
