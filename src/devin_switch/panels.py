"""Desktop side panels: the live whiteboard and the agent browser, beside the chat."""

import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from devin_switch.executable import command_prefix
from devin_switch.store import SwitchError

PUBLISHER = "devin-switch"
NAME = "panels"
EXTENSION_ID = f"{PUBLISHER}.{NAME}"


def extensions_directory() -> Path:
    return Path(os.environ.get("DS_DESKTOP_EXTENSIONS") or Path.home() / ".devin/extensions")


def installed_version() -> str | None:
    try:
        entries = json.loads((extensions_directory() / "extensions.json").read_text())
    except (OSError, ValueError):
        return None
    for entry in entries if isinstance(entries, list) else ():
        identifier = entry.get("identifier", {}) if isinstance(entry, dict) else {}
        if str(identifier.get("id", "")).lower() == EXTENSION_ID:
            return str(entry.get("version") or "")
    return None


SOURCE = Path(__file__).parent / "desktop_extension"
DESKTOP_CLI = Path("/Applications/Devin.app/Contents/Resources/app/bin/devin-desktop")
CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\
<Default Extension=".json" ContentType="application/json"/>\
<Default Extension=".js" ContentType="application/javascript"/>\
<Default Extension=".css" ContentType="text/css"/>\
<Default Extension=".vsixmanifest" ContentType="text/xml"/></Types>
"""


def manifest() -> dict:
    return json.loads((SOURCE / "package.json").read_text())


def vsix_manifest(package: dict) -> str:
    field = lambda key: escape(str(package[key]), {'"': "&quot;"})  # noqa: E731
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{field("name")}" Version="{field("version")}" \
Publisher="{field("publisher")}"/>
    <DisplayName>{field("displayName")}</DisplayName>
    <Description xml:space="preserve">{field("description")}</Description>
    <Categories>Other</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" \
Value="{escape(package["engines"]["vscode"])}"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" \
Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def build_vsix(destination: Path) -> Path:
    """Package the extension with this ds's command line so it never guesses a PATH."""
    package = manifest()
    vsix = destination / f"{EXTENSION_ID}-{package['version']}.vsix"
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("extension.vsixmanifest", vsix_manifest(package))
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, f"extension/{path.relative_to(SOURCE).as_posix()}")
        archive.writestr("extension/ds.json", json.dumps({"command": command_prefix()}))
    return vsix


def desktop_cli() -> Path:
    cli = Path(os.environ.get("DS_DESKTOP_CLI") or DESKTOP_CLI)
    if not cli.exists():
        raise SwitchError(f"The Desktop app's command line tool is missing: {cli}")
    return cli


def install() -> str:
    cli = desktop_cli()
    with tempfile.TemporaryDirectory(prefix="ds-panels-") as temporary:
        vsix = build_vsix(Path(temporary))
        result = subprocess.run(
            (str(cli), "--install-extension", str(vsix), "--force"),
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
    if result.returncode:
        raise SwitchError(
            f"Installing the panels failed: {(result.stderr or result.stdout).strip()}"
        )
    version = manifest()["version"]
    if installed_version() != version:
        raise SwitchError("The Desktop app did not record the panels extension; see its log.")
    return "\n".join(
        (
            f"Installed the Switch panels {version} into the Desktop app.",
            "Commands: 'Switch: Open Whiteboard', 'Switch: Open Agent Browser' and "
            "'Switch: Show Running Subagents'.",
            "Whiteboards from chats now open beside the chat in the window showing their folder.",
            "If the commands are missing, run 'Developer: Reload Window' once.",
        )
    )


def status() -> str:
    packaged, current = manifest()["version"], installed_version()
    if current is None:
        return f"Switch panels are not installed (available: {packaged}). Run: ds panels install"
    note = "" if current == packaged else f"; {packaged} is available, run: ds panels install"
    return f"Switch panels {current} are installed in the Desktop app{note}."


def command(args) -> int:
    if args.action == "install":
        print(install())
    elif args.action == "status":
        print(status())
    return 0
