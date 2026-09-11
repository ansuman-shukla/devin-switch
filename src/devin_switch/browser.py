"""Use existing Chrome profiles without reading cookies or passwords."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devin_switch.store import SwitchError

CHROME_DATA = Path.home() / "Library/Application Support/Google/Chrome"
CHROME_APP = Path("/Applications/Google Chrome.app")
MANUAL_LOGIN_URL = "https://app.devin.ai/auth/cli/token"


@dataclass(frozen=True)
class ChromeProfile:
    directory: str
    name: str
    email: str


def profiles() -> tuple[ChromeProfile, ...]:
    try:
        state = json.loads((CHROME_DATA / "Local State").read_text())
        cache = state["profile"]["info_cache"]
        return tuple(
            ChromeProfile(directory, value.get("name", ""), value.get("user_name", ""))
            for directory, value in sorted(cache.items())
        )
    except FileNotFoundError as exc:
        raise SwitchError("No local Google Chrome profiles found.") from exc
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as exc:
        raise SwitchError("Chrome's profile index could not be read.") from exc


def open_login(profile: str) -> None:
    if profile not in {item.directory for item in profiles()}:
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
