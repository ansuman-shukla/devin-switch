"""Copy items out of Chrome's data folder, asking the user with a popup when macOS blocks it.

macOS 27 lets only Chrome and apps with Full Disk Access read that folder, and never prompts.
Switch copies directly when it can. Otherwise it hands the request to the Chrome access helper
app (macos/ChromeAccess.swift), the only app given Full Disk Access, which shows an Allow/Deny
dialog and copies nothing unless the user allows it. Readable data never shows a dialog.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from devin_switch.store import SwitchError, write_json

APP_NAME = "Devin Switch Chrome Access.app"
HELPER_DATA = Path.home() / "Library/Application Support/Google/Chrome"
TIMEOUT = 120
MISSING = (
    "macOS blocks reading Chrome's data. Install the Chrome access helper once with `make "
    "chrome-access` in the devin-switch repository and give “Devin Switch Chrome Access” Full "
    "Disk Access under System Settings → Privacy & Security; Switch then asks with a popup "
    "whenever it needs Chrome's data."
)


class ChromeBlocked(SwitchError):
    """macOS 27 App Data Protection denied reading Chrome's data folder."""


def helper() -> Path | None:
    configured = os.environ.get("DS_CHROME_ACCESS_APP")
    candidates = (
        (Path(configured),)
        if configured
        else (Path.home() / "Applications" / APP_NAME, Path("/Applications") / APP_NAME)
    )
    return next((path for path in candidates if path.is_dir()), None)


def validate(items: Sequence[str]) -> None:
    if not items:
        raise SwitchError("Name at least one Chrome item to copy.")
    for item in items:
        if item.startswith("/") or any(part in ("", ".", "..") for part in item.split("/")):
            raise SwitchError(f"Use a path inside Chrome's data folder, not {item!r}.")


def copy_item(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    if source.is_dir():
        try:
            shutil.copytree(source, target, symlinks=True)
        except shutil.Error:
            pass  # Chrome may rewrite storage while it is copied; keep what was readable.
    else:
        shutil.copy2(source, target)


def copy(root: Path, items: Sequence[str], destination: Path, reason: str) -> None:
    """Copy items (paths relative to root) into destination under their own names.

    Missing items are skipped. When macOS blocks root, the user is asked with a popup first.
    """
    validate(items)
    try:
        for item in items:
            copy_item(root / item, destination / PurePosixPath(item).name)
    except PermissionError:
        ask(root, items, destination, reason)


def read_text(root: Path, item: str, reason: str) -> str:
    with tempfile.TemporaryDirectory(prefix="ds-chrome-") as folder:
        copy(root, (item,), Path(folder), reason)
        return (Path(folder) / PurePosixPath(item).name).read_text()


def ask(root: Path, items: Sequence[str], destination: Path, reason: str) -> None:
    app = helper()
    if app is None:
        raise ChromeBlocked(MISSING)
    if root != HELPER_DATA:
        raise ChromeBlocked(f"macOS blocks reading {root}; the popup only covers {HELPER_DATA}.")
    with tempfile.TemporaryDirectory(prefix="ds-chrome-access-") as folder:
        request, result = Path(folder) / "request.json", Path(folder) / "result.json"
        write_json(
            request,
            {
                "items": list(items),
                "destination": str(destination.resolve()),
                "reason": reason,
                "requester": os.getcwd(),
            },
        )
        launch(app, request, result)
        try:
            outcome = json.loads(result.read_text())
        except (OSError, ValueError):
            outcome = {"decision": "error", "message": "it gave no answer"}
    decision = outcome.get("decision")
    if decision == "allow":
        return
    if decision == "deny":
        raise ChromeBlocked("Access to Chrome's data was denied in the popup.")
    if decision == "timeout":
        raise ChromeBlocked(
            f"Nobody answered the Chrome access popup within {TIMEOUT // 60} minutes."
        )
    raise SwitchError(f"The Chrome access helper failed: {outcome.get('message') or 'unknown'}")


def launch(app: Path, request: Path, result: Path) -> None:
    """Start the helper through Launch Services so macOS checks its access, not the caller's."""
    subprocess.run(
        (
            "/usr/bin/open",
            "-W",
            "-n",
            str(app),
            "--args",
            "--request",
            str(request),
            "--result",
            str(result),
            "--timeout",
            str(TIMEOUT),
        ),
        check=False,
        capture_output=True,
        timeout=TIMEOUT + 60,
    )
