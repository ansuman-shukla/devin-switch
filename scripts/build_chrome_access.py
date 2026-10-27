"""Build and install the Chrome access helper app, keeping its Full Disk Access grant.

macOS 27 lets only Chrome and apps with Full Disk Access read Chrome's data folder. The helper
is the one app given that access; it asks with an Allow/Deny dialog before each copy. macOS ties
the grant to the app's code signature, so an unchanged build is left in place instead of being
replaced with an identical copy that would look new.
"""

import argparse
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Devin Switch Chrome Access"
APP = Path.home() / "Applications" / f"{NAME}.app"
IDENTIFIER = "local.devinswitch.chrome-access"
SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def build(app: Path) -> None:
    executable = app / "Contents/MacOS/ChromeAccess"
    executable.parent.mkdir(parents=True)
    subprocess.run(
        (
            "swiftc",
            "-O",
            "-parse-as-library",
            "-target",
            f"{platform.machine()}-apple-macos14.0",
            "-framework",
            "AppKit",
            str(ROOT / "macos/ChromeAccess.swift"),
            "-o",
            str(executable),
        ),
        check=True,
    )
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "ChromeAccess",
                "CFBundleIdentifier": IDENTIFIER,
                "CFBundleName": NAME,
                "CFBundleDisplayName": NAME,
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "14.0",
                "LSUIElement": True,
                "NSPrincipalClass": "NSApplication",
            },
            handle,
        )
    subprocess.run(
        ("codesign", "--force", "--sign", "-", "--identifier", IDENTIFIER, str(app)), check=True
    )


def cdhash(app: Path) -> str | None:
    if not app.is_dir():
        return None
    result = subprocess.run(
        ("codesign", "-dvvv", str(app)), capture_output=True, text=True, check=False
    )
    match = re.search(r"^CDHash=(\w+)$", result.stderr, re.MULTILINE)
    return match.group(1) if match else None


def install(destination: Path, open_settings: bool) -> bool:
    """Install the helper; True when it changed and needs Full Disk Access granted."""
    with tempfile.TemporaryDirectory() as workspace:
        built = Path(workspace) / destination.name
        build(built)
        if cdhash(built) == cdhash(destination):
            print(f"{destination} is up to date; its Full Disk Access grant is kept.")
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(built, destination, symlinks=True)
    print(
        f"Installed {destination}.\n"
        f"Give it Full Disk Access once: System Settings → Privacy & Security → Full Disk Access → "
        f"+ → {destination}. Remove any older “{NAME}” entry first."
    )
    if open_settings:
        subprocess.run(("/usr/bin/open", SETTINGS), check=False)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--destination", type=Path, default=APP)
    parser.add_argument("--no-settings", action="store_true", help="Do not open System Settings")
    args = parser.parse_args()
    install(args.destination, not args.no_settings)


if __name__ == "__main__":
    main()
