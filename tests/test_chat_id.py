import os
import shlex
import subprocess
import sys

from devin_switch.native import Native


def test_native_children_get_the_chat_id_command_unless_one_is_set(
    native: Native, monkeypatch
) -> None:
    account = native.store.accounts()[0]
    command = shlex.split(native.environment(account)["WHITEBOARD_CHAT_COMMAND"])
    assert command == [sys.executable, "-m", "devin_switch.cli", "chat", "id"]
    assert "WHITEBOARD_OPEN" not in native.environment(account)
    monkeypatch.setenv("WHITEBOARD_CHAT_COMMAND", "echo fixed")
    assert native.environment(account)["WHITEBOARD_CHAT_COMMAND"] == "echo fixed"


def test_chat_id_command_survives_paths_with_spaces(monkeypatch) -> None:
    from devin_switch.executable import ds_command

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Devin Switch.app/Contents/ds")
    assert shlex.split(ds_command("chat", "id")) == [
        "/Applications/Devin Switch.app/Contents/ds",
        "chat",
        "id",
    ]


def test_chat_id_outside_a_gui_chat_fails_without_guessing(store) -> None:
    done = subprocess.run(
        (sys.executable, "-m", "devin_switch.cli", "chat", "id"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode != 0 and done.stdout == ""
    assert "Not inside a Desktop chat launched through Switch" in done.stderr
    stale = subprocess.run(
        (sys.executable, "-m", "devin_switch.cli", "chat", "id"),
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "DS_ACP_RUN_ID": "not-a-live-run"},
    )
    assert stale.returncode != 0 and stale.stdout == ""
