import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from devin_switch import cli, desktop, native
from devin_switch.store import Store, SwitchError


def test_frozen_terminal_launcher_invokes_bundled_runtime(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "Devin Switch.app/Contents/Resources/ds-runtime/ds-runtime"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(desktop, "find_binary", lambda: tmp_path / "devin")
    launcher = desktop.terminal_launcher(store, ("login", "work"), tmp_path)
    command = shlex.split(launcher.read_text().splitlines()[2])
    assert command[-3:] == [str(executable), "login", "work"]
    assert "devin_switch.cli" not in command


def test_gui_finds_system_app(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    app = Path("/Applications/Devin Switch.app")
    monkeypatch.setattr(Path, "is_dir", lambda self: self == app)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda arguments, **_: calls.append(arguments))
    assert cli.execute(cli.parser().parse_args(["gui"]), store) == 0
    assert calls == [("/usr/bin/open", str(app))]


def test_native_discovery_without_shell_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / ".local/bin/devin"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.delenv("DS_BINARY", raising=False)
    monkeypatch.setattr(native.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(native, "BUNDLED_CLI", tmp_path / "missing")
    assert native.find_binary() == binary


def test_invalid_explicit_native_path_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_BINARY", str(tmp_path / "missing"))
    with pytest.raises(SwitchError, match="not found"):
        native.find_binary()


def test_module_entrypoint_supports_cli_and_private_bridge(tmp_path: Path) -> None:
    import os

    environment = {**os.environ, "DS_HOME": str(tmp_path / "isolated-state")}
    help_result = subprocess.run(
        (sys.executable, "-m", "devin_switch", "--help"),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Switch saved Devin CLI accounts" in help_result.stdout
    bridge = subprocess.run(
        (sys.executable, "-m", "devin_switch", "--desktop-bridge"),
        input='{"action": "add"}',
        env=environment,
        capture_output=True,
        text=True,
    )
    assert bridge.returncode == 1
    assert json.loads(bridge.stdout) == {"ok": False, "message": "Account is required."}


def test_installed_gui_bridge_modules_and_registry_are_isolated(native, tmp_path):
    import os

    root = Path(__file__).resolve().parents[1]
    excluded = {"PYTHONPATH", "DS_RUN_ID", "DS_ACP_RUN_ID", "DS_EXECUTABLE", "DS_FROZEN"}
    environment = {
        **{
            key: value
            for key, value in os.environ.items()
            if key not in excluded and not key.startswith("XDG_")
        },
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "DS_HOME": str(native.store.root),
        "DS_BINARY": str(native.binary),
    }
    installed = subprocess.run(
        (
            "uv",
            "tool",
            "install",
            "--offline",
            "--force",
            "--reinstall-package",
            "devin-switch",
            "--python",
            sys.executable,
            str(root),
        ),
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stderr
    python = tmp_path / "tools/devin-switch/bin/python"
    imported = subprocess.run(
        (
            str(python),
            "-c",
            "import devin_switch.acp, devin_switch.acp_state; "
            "assert callable(devin_switch.acp_state.check_handoff); "
            "print(devin_switch.acp.__file__)",
        ),
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert imported.returncode == 0, imported.stderr
    assert str(tmp_path / "tools") in imported.stdout
    registry = subprocess.run(
        (str(tmp_path / "bin/ds"), "acp", "--sandbox", "--print-registry"),
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert registry.returncode == 0, registry.stderr
    agent = json.loads(registry.stdout)["agents"][0]
    binary = next(iter(agent["distribution"]["binary"].values()))
    assert binary["cmd"] == str(python)
    assert binary["args"] == ["-m", "devin_switch.cli", "acp", "--sandbox"]
    assert set(binary["env"]) == {"DS_HOME", "DS_BINARY"}
    assert not (native.store.root / "acp").exists()
