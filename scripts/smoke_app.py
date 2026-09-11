import argparse
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def verify(app: Path) -> None:
    subprocess.run(("codesign", "--verify", "--deep", "--strict", str(app)), check=True)
    with (app / "Contents/Info.plist").open("rb") as handle:
        metadata = plistlib.load(handle)
    assert metadata["LSMinimumSystemVersion"] == "14.0"
    assert not (app / "Contents/Resources/bridge.json").exists()
    with tempfile.TemporaryDirectory(prefix="ds-portability-") as temporary:
        root = Path(temporary)
        relocated = root / "Folder with spaces/Devin Switch.app"
        shutil.copytree(app, relocated, symlinks=True)
        runtime = relocated / "Contents/Resources/ds-runtime/ds-runtime"
        binary = root / "fake-devin"
        binary.write_text(
            '#!/bin/sh\nif [ "$1 $2" = "auth status" ]; then\n'
            "  printf 'Logged in as test@example.invalid\\n'\n"
            "else\n  printf 'offline-native-smoke\\n'\nfi\n"
        )
        binary.chmod(0o700)
        environment = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "DS_HOME": str(root / "state"),
            "DS_BINARY": str(binary),
            "TMPDIR": str(root),
        }

        def run(arguments: tuple[str, ...], payload: dict | None = None) -> str:
            return subprocess.run(
                arguments,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
                env=environment,
                cwd=root,
            ).stdout

        assert "Switch saved Devin CLI accounts" in run((str(runtime), "--help"))
        bridge = (str(runtime), "--desktop-bridge")
        assert json.loads(run(bridge, {"action": "add", "account": "smoke"}))["ok"]
        credentials = root / "state/accounts/smoke/data/devin/credentials.toml"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text("valid-smoke-fixture")
        credentials.chmod(0o600)
        reply = json.loads(
            run(bridge, {"action": "start", "account": "smoke", "project": str(root)})
        )
        assert reply["ok"], reply
        assert "offline-native-smoke" in run((reply["launcher"],))
        assert os.stat(credentials).st_mode & 0o777 == 0o600
    print("Portable app passed: signing, relocated runtime, JSON bridge, and Terminal launcher.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Smoke-test a portable app without real credentials."
    )
    parser.add_argument("app", type=Path)
    verify(parser.parse_args().app.resolve())
