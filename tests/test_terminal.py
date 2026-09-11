import errno
import json
import os
import pty
import select
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devin_switch import handoff, sessions, terminal
from devin_switch.native import Native
from devin_switch.store import Store, private_directory, write_json


def save_request(store: Store, run_id: str, pid: int = 12345) -> None:
    path = handoff.state_path(store, run_id, "request")
    private_directory(path.parent)
    write_json(path, {"account": "ansuman-2", "requester_pid": pid, "created_at": time.time()})


def test_exit_waits_for_command_then_sends_separate_escape_and_eof(store, monkeypatch):
    run_id = "a" * 32
    save_request(store, run_id)
    request = terminal.ExitRequest(store, run_id)
    monkeypatch.setattr(terminal, "process_exists", lambda _: True)
    assert request.advance(0) == (b"", "")
    monkeypatch.setattr(terminal, "process_exists", lambda _: False)
    assert request.advance(1) == (b"\x1b", "")
    assert request.advance(1.1) == (b"", "")
    assert request.advance(1.3) == (b"\x04", "")
    assert request.advance(1.4) == (b"", "")
    assert request.advance(1.9) == (b"\x04", "")
    handoff.cancel(store, {"id": run_id})
    assert request.advance(2) == (b"", "")


def test_exit_timeout_cancels_instead_of_killing_or_leaving_delayed_switch(store, monkeypatch):
    run_id = "b" * 32
    save_request(store, run_id)
    request = terminal.ExitRequest(store, run_id, timeout=2)
    monkeypatch.setattr(terminal, "process_exists", lambda _: True)
    assert request.advance(0) == (b"", "")
    keys, message = request.advance(3)
    assert keys == b"" and "canceled" in message
    assert not handoff.state_path(store, run_id, "request").exists()
    assert request.advance(4) == (b"", "")


@pytest.mark.parametrize("pid", [-1, 0, 1, None, "123", True])
def test_invalid_request_cannot_signal_a_process(store, monkeypatch, pid):
    run_id = "c" * 32
    save_request(store, run_id, pid)
    monkeypatch.setattr(terminal, "process_exists", lambda _: pytest.fail("Invalid PID"))
    assert terminal.ExitRequest(store, run_id).advance(0) == (b"", "")


def read_until(master: int, marker: bytes, timeout: float = 10) -> bytes:
    output = b""
    deadline = time.monotonic() + timeout
    while marker not in output and time.monotonic() < deadline:
        if select.select([master], [], [], 0.1)[0]:
            try:
                data = os.read(master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            output += data
    assert marker in output, output.decode(errors="replace")
    return output


@pytest.mark.parametrize("usable", [True, False])
def test_one_command_automatically_resumes_with_best_account_on_real_pty(
    signed_in: Native, tmp_path: Path, usable: bool
):
    store = signed_in.store
    project = tmp_path / "project with spaces"
    project.mkdir()
    with store.lock():
        third = store.add("ansuman-3", None)
        store.select(store.account("ansuman-2"))
    store.credentials(third).write_text("valid-third")
    with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT, title TEXT, working_directory TEXT, "
            "last_activity_at INTEGER, hidden INTEGER DEFAULT 0)"
        )
        connection.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, 0)",
            [("exact-chat", "Original", str(project), 1), ("newer-chat", "Other", str(project), 2)],
        )
    offline_command = tmp_path / "offline switch.py"
    offline_command.write_text(
        "import time\n"
        "from devin_switch import cli, usage\n"
        "def refresh(store, account, *, force):\n"
        "    assert force\n"
        "    now = time.time()\n"
        f"    used = (80 if account.name == 'ansuman-2' else 25) if {usable!r} else 100\n"
        "    return usage.Usage(status='ok', fetched_at=now, "
        "daily=usage.Window(used, now+3600, 'available'), "
        "weekly=usage.Window(used, now+86400, 'available'))\n"
        "usage.refresh_account = refresh\n"
        "raise SystemExit(cli.main())\n"
    )
    binary = tmp_path / "fake native"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys, termios, tty\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print('Logged in as offline@example.invalid')\n"
        "    sys.exit(0)\n"
        "account = pathlib.Path(os.environ['XDG_DATA_HOME']).parent.name\n"
        "assert all(os.isatty(fd) for fd in (0, 1, 2))\n"
        "assert os.tcgetpgrp(0) == os.getpgrp()\n"
        "settings = termios.tcgetattr(0)\n"
        "tty.setraw(0)\n"
        "print('START ' + json.dumps({'account': account, 'args': sys.argv[1:], "
        "'cwd': os.getcwd()}), flush=True)\n"
        "print('READY-' + account, flush=True)\n"
        "line = b''\n"
        "while True:\n"
        "    key = os.read(0, 1)\n"
        "    if key == b'\\x04':\n"
        "        break\n"
        "    if key == b'\\x1b':\n"
        "        line = b''\n"
        "    elif key in (b'\\r', b'\\n'):\n"
        "        assert line == b'!ds switch', line\n"
        f"        result = subprocess.run([sys.executable, {str(offline_command)!r}, 'switch'], "
        "stdin=subprocess.DEVNULL)\n"
        f"        assert result.returncode == {0 if usable else 1}\n"
        "        print('COMMAND-DONE', flush=True)\n"
        "        line = b''\n"
        "    else:\n"
        "        line += key\n"
        "config = pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'devin/config.json'\n"
        "hooks = json.loads(config.read_text())['hooks']['SessionEnd']\n"
        "event = {'hook_event_name':'SessionEnd','session_id':'exact-chat', "
        "'reason':'prompt_input_exit'}\n"
        "for entry in hooks:\n"
        "    for hook in entry['hooks']:\n"
        "        result = subprocess.run(hook['command'], shell=True, input=json.dumps(event), "
        "text=True, capture_output=True)\n"
        "        assert result.returncode == 0, result.stderr\n"
        "termios.tcsetattr(0, termios.TCSADRAIN, settings)\n"
    )
    binary.chmod(0o700)
    driver = tmp_path / "tty driver.py"
    driver.write_text(
        "import termios\n"
        "from devin_switch import cli\n"
        "original = termios.tcgetattr(0)\n"
        "code = cli.main()\n"
        "actual = termios.tcgetattr(0)\n"
        "pending_input_flag = getattr(termios, 'PENDIN', 0)\n"
        "original[3] &= ~pending_input_flag\n"
        "actual[3] &= ~pending_input_flag\n"
        "assert actual == original, (original, actual)\n"
        "print('TERMINAL-RESTORED', flush=True)\n"
        "raise SystemExit(code)\n"
    )
    master, slave = pty.openpty()
    process = subprocess.Popen(
        (
            sys.executable,
            str(driver),
            "run",
            "--account",
            "ansuman-1",
            "--",
            "--sandbox",
            "--resume",
            "newer-chat",
            "--",
            "private-initial-prompt",
        ),
        env={**os.environ, "DS_BINARY": str(binary)},
        cwd=project,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        preexec_fn=terminal.claim_terminal,
    )
    try:
        output = read_until(master, b"READY-ansuman-1")
        assert not (project / ".devin").exists()
        os.write(master, b"!ds switch\r")
        output += read_until(master, b"READY-ansuman-3" if usable else b"COMMAND-DONE")
        if usable:
            assert b"75% remaining" in output
            assert b'"args": ["--sandbox", "--resume", "exact-chat"]' in output
        else:
            assert b"No other saved login has confirmed remaining usage" in output
            assert b"READY-ansuman-3" not in output
            assert not list((store.root / "handoffs").glob("*.request.json"))
        assert b"READY-ansuman-2" not in output
        assert process.poll() is None
        running = [run for run in sessions.runs(store) if run["active"]]
        assert len(running) == 1
        assert running[0]["account"] == ("ansuman-3" if usable else "ansuman-1")
        assert running[0]["project"] == str(project)
        assert running[0]["session_id"] == ("exact-chat" if usable else "newer-chat")
        os.write(master, b"\x04")
        read_until(master, b"TERMINAL-RESTORED")
        assert process.wait(timeout=5) == 0
        assert store.selected().name == "ansuman-2"
        assert not any(run["active"] for run in sessions.runs(store))
        assert "private-initial-prompt" not in json.dumps(sessions.overview(store))
    finally:
        os.close(master)
        os.close(slave)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
