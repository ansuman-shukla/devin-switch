"""Build the native app and point it at the installed CLI environment."""

import json
import platform
import plistlib
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = Path.home() / "Applications" / "Devin Switch.app"


def main() -> None:
    tool_directory = Path(subprocess.check_output(("uv", "tool", "dir"), text=True).strip())
    python = tool_directory / "devin-switch" / "bin" / "python"
    if not python.is_file():
        raise SystemExit("Install the CLI first: uv tool install --force .")
    executable = APP / "Contents/MacOS/DevinSwitch"
    resources = APP / "Contents/Resources"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        (
            "swiftc",
            "-parse-as-library",
            "-O",
            "-target",
            f"{platform.machine()}-apple-macos14.0",
            "-framework",
            "SwiftUI",
            "-framework",
            "AppKit",
            str(ROOT / "macos/DevinSwitch.swift"),
            "-o",
            str(executable),
        ),
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="ds-icon-") as temporary:
        iconset = Path(temporary) / "AppIcon.iconset"
        subprocess.run(("swift", str(ROOT / "macos/AppIcon.swift"), str(iconset)), check=True)
        subprocess.run(
            ("iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")),
            check=True,
        )
    with (APP / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "DevinSwitch",
                "CFBundleIdentifier": "local.devinswitch.desktop",
                "CFBundleName": "Devin Switch",
                "CFBundleIconFile": "AppIcon",
                "CFBundleDisplayName": "Devin Switch",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.2.0",
                "CFBundleVersion": "2",
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "LSMinimumSystemVersion": "14.0",
            },
            handle,
        )
    (resources / "bridge.json").write_text(json.dumps({"python": str(python)}))
    subprocess.run(("codesign", "--force", "--sign", "-", str(APP)), check=True)
    print(f"Installed {APP}")


if __name__ == "__main__":
    main()
