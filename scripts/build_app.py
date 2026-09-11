"""Build the native app and point it at the installed CLI environment."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = Path.home() / "Applications" / "Devin Switch.app"


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def bundle_runtime(app: Path, workspace: Path) -> None:
    subprocess.run(
        (
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--name",
            "ds-runtime",
            "--distpath",
            str(app / "Contents/Resources"),
            "--workpath",
            str(workspace / "freeze"),
            "--specpath",
            str(workspace),
            "--paths",
            str(ROOT / "src"),
            "--exclude-module",
            "tkinter",
            str(ROOT / "src/devin_switch/__main__.py"),
        ),
        check=True,
        env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": "14.0"},
    )
    licenses = app / "Contents/Resources/Licenses"
    licenses.mkdir(parents=True)
    shutil.copyfile(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", licenses / "Python.txt")
    distribution = importlib.metadata.distribution("pyinstaller")
    for entry in distribution.files or ():
        if entry.name == "COPYING.txt":
            shutil.copyfile(distribution.locate_file(entry), licenses / "PyInstaller.txt")
            break
    else:
        raise SystemExit("The PyInstaller license was not found; refusing to distribute.")


def build_app(app: Path, *, standalone: bool, workspace: Path) -> None:
    executable = app / "Contents/MacOS/DevinSwitch"
    resources = app / "Contents/Resources"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    if standalone:
        bundle_runtime(app, workspace)
    else:
        tool_directory = Path(subprocess.check_output(("uv", "tool", "dir"), text=True).strip())
        python = tool_directory / "devin-switch" / "bin" / "python"
        if not python.is_file():
            raise SystemExit("Install the CLI first: make app")
        (resources / "bridge.json").write_text(json.dumps({"python": str(python)}))
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
            str(ROOT / "macos/AccountViews.swift"),
            "-o",
            str(executable),
        ),
        check=True,
    )
    iconset = workspace / "AppIcon.iconset"
    subprocess.run(("swift", str(ROOT / "macos/AppIcon.swift"), str(iconset)), check=True)
    subprocess.run(
        ("iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")),
        check=True,
    )
    version = project_version()
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "DevinSwitch",
                "CFBundleIdentifier": "local.devinswitch.desktop",
                "CFBundleName": "Devin Switch",
                "CFBundleIconFile": "AppIcon",
                "CFBundleDisplayName": "Devin Switch",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "LSMinimumSystemVersion": "14.0",
            },
            handle,
        )
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(ROOT / name, resources / name)
    subprocess.run(("codesign", "--force", "--sign", "-", str(app)), check=True)
    subprocess.run(("codesign", "--verify", "--deep", "--strict", str(app)), check=True)


def create_dmg(app: Path, destination: Path, workspace: Path) -> Path:
    name = f"Devin-Switch-{project_version()}-macos-{platform.machine()}.dmg"
    disk = destination / name
    if disk.exists():
        raise SystemExit(f"Refusing to overwrite an existing disk image: {disk}")
    stage = workspace / "disk"
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    for source, target in (
        ("README.md", "Read Me.md"),
        ("LICENSE", "LICENSE"),
        ("NOTICE", "NOTICE"),
    ):
        shutil.copyfile(ROOT / source, stage / target)
    subprocess.run(
        (
            "hdiutil",
            "create",
            "-volname",
            "Devin Switch",
            "-srcfolder",
            str(stage),
            "-format",
            "UDZO",
            "-ov",
            str(disk),
        ),
        check=True,
    )
    subprocess.run(("hdiutil", "verify", str(disk)), check=True)
    with disk.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    disk.with_suffix(".dmg.sha256").write_text(f"{checksum}  {disk.name}\n")
    return disk


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local app or portable macOS disk image.")
    parser.add_argument("--standalone", action="store_true", help="Bundle Python using PyInstaller")
    parser.add_argument(
        "--dmg", action="store_true", help="Create a disk image; requires --standalone"
    )
    parser.add_argument(
        "--output", type=Path, help="App destination; existing outputs are not replaced"
    )
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The native app must be built on macOS.")
    if args.dmg and not args.standalone:
        parser.error("--dmg requires --standalone")
    app = (args.output or (ROOT / "dist/Devin Switch.app" if args.standalone else APP)).resolve()
    if args.standalone and app.exists():
        parser.error(f"Output already exists; choose a fresh --output path: {app}")
    if not args.standalone and (app / "Contents/Resources/ds-runtime").exists():
        parser.error("A portable app already exists here; choose a different --output path.")
    app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ds-build-") as temporary:
        workspace = Path(temporary)
        staged = workspace / "Devin Switch.app"
        build_app(staged, standalone=args.standalone, workspace=workspace)
        shutil.copytree(staged, app, symlinks=True, dirs_exist_ok=not args.standalone)
        if args.dmg:
            print(f"Created {create_dmg(app, app.parent, workspace)}")
    print(f"Built {app}")


if __name__ == "__main__":
    main()
