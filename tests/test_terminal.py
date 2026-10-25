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
from otlp import encode_logs

from devin_switch import desktop, handoff, sessions, terminal
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError, private_directory, write_json


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


def test_confirmed_exit_stops_keys_and_survives_slow_native_shutdown(store, monkeypatch):
    run_id = "d" * 32
    save_request(store, run_id)
    request = terminal.ExitRequest(store, run_id, timeout=2)
    monkeypatch.setattr(terminal, "process_exists", lambda _: False)
    assert request.advance(0) == (b"\x1b", "")
    write_json(handoff.state_path(store, run_id, "exit"), {"session_id": "exact-chat"})
    assert request.advance(1) == (b"", "")
    assert request.advance(3) == (b"", "")
    assert handoff.state_path(store, run_id, "request").exists()


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


@pytest.mark.parametrize("usable,background", [(True, False), (False, False), (True, True)])
def test_one_command_automatically_resumes_with_best_account_on_real_pty(
    signed_in: Native, tmp_path: Path, usable: bool, background: bool, monkeypatch
):
    monkeypatch.setattr(desktop.browser, "profiles", lambda: ())
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
        f"    used = (100 if account.name == 'ansuman-2' else 25) if {usable!r} else 100\n"
        "    return usage.Usage(status='ok', fetched_at=now, "
        "daily=usage.Window(used, now+3600, 'available'), "
        "weekly=usage.Window(used, now+86400, 'available'))\n"
        "usage.refresh_account = refresh\n"
        "raise SystemExit(cli.main())\n"
    )
    binary = tmp_path / "fake native"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys, termios, time, tty\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print('Logged in as offline@example.invalid')\n"
        "    sys.exit(0)\n"
        "account = pathlib.Path(os.environ['XDG_DATA_HOME']).parent.name\n"
        f"if {background!r} and account == 'ansuman-1':\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "close_fds=False, start_new_session=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    pathlib.Path('background.pid').write_text(str(child.pid))\n"
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
        f"if {background!r} and account == 'ansuman-1':\n"
        "    time.sleep(1.5)\n"
        "termios.tcsetattr(0, termios.TCSADRAIN, settings)\n"
    )
    binary.chmod(0o700)
    driver = tmp_path / "tty driver.py"
    driver.write_text(
        "import functools, termios\n"
        "from devin_switch import cli, terminal\n"
        f"if {background!r}:\n"
        "    terminal.ExitRequest = functools.partial(terminal.ExitRequest, timeout=1)\n"
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
        assert store.selected().name == "ansuman-2"
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
        expected_default = "ansuman-3" if usable else "ansuman-2"
        assert store.selected().name == expected_default
        assert desktop.snapshot(store)["selected"] == expected_default
        os.write(master, b"\x04")
        read_until(master, b"TERMINAL-RESTORED")
        assert process.wait(timeout=5) == 0
        assert store.selected().name == expected_default
        assert not any(run["active"] for run in sessions.runs(store))
        assert "private-initial-prompt" not in json.dumps(sessions.overview(store))
        if background:
            os.kill(int((project / "background.pid").read_text()), 0)
            first_run = sessions.runs(store)[0]
            assert store.locked(f"run-{first_run['id']}.lock")
    finally:
        pid_file = project / "background.pid"
        if pid_file.exists():
            import signal

            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        os.close(master)
        os.close(slave)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_automatic_request_sends_exit_keys_without_waiting_for_requester(store, monkeypatch):
    run_id = "e" * 32
    path = handoff.state_path(store, run_id, "request")
    private_directory(path.parent)
    write_json(
        path,
        {"account": "ansuman-2", "requester_pid": os.getpid(), "created_at": 0, "automatic": True},
    )
    monkeypatch.setattr(terminal, "process_exists", lambda _: True)
    request = terminal.ExitRequest(store, run_id)
    assert request.advance(0) == (b"\x1b", "")
    assert request.advance(0.3) == (b"\x04", "")


def test_quota_watch_matches_styled_split_alert_once_per_cooldown(monkeypatch):
    calls = []

    def queue_automatic(native, account, progress):
        progress("checking")
        calls.append(account)
        return "switching"

    monkeypatch.setattr(handoff, "queue_automatic", queue_automatic)
    watch = terminal.QuotaWatch("native", "ansuman-1", cooldown=10)
    watch.feed(b"The agent mentioned quota limits\r\n\x1b]0;title\x07", 0)
    watch.feed(b"\x1b[1;3", 1)
    watch.feed(b"1mQuota\x1b[0m\x1b[2C\x1b[1mexhau", 1)
    watch.feed(b"sted\x1b[0m", 1)
    watch.worker.join(5)
    assert calls == ["ansuman-1"]
    assert [watch.message(), watch.message(), watch.message()] == ["checking", "switching", ""]
    watch.feed(b"Quota exhausted", 5)
    assert calls == ["ansuman-1"]
    watch.feed(b"Usage limit reached", 12)
    watch.worker.join(5)
    assert calls == ["ansuman-1", "ansuman-1"]


def test_quota_watch_matches_alert_followed_by_large_redraw_in_one_read(monkeypatch):
    calls = []
    monkeypatch.setattr(
        handoff, "queue_automatic", lambda native, account, progress: calls.append(account)
    )
    watch = terminal.QuotaWatch("native", "ansuman-1")
    rule = "─".encode() * 180
    watch.feed(
        b"\x1b[33m \xe2\x96\xb2 Quota exhausted\x1b[0m\r\n   Your daily usage quota has been "
        b"exhausted.\r\n\x1b[2m" + rule + b"\x1b[0m\r\n\xe2\x9d\xad \r\n" + rule,
        0,
    )
    watch.worker.join(5)
    assert calls == ["ansuman-1"]


def test_typed_quota_error_bypasses_text_cooldown_once_per_incident(monkeypatch):
    calls = []
    monkeypatch.setattr(
        handoff, "queue_automatic", lambda native, account, progress: calls.append(account)
    )
    watch = terminal.QuotaWatch("native", "ansuman-1")
    quota = {"event.name": "api_error", "error_kind": "ResourceExhausted"}
    watch.feed(b"the agent printed: quota exhausted", 0)
    watch.worker.join(5)
    watch.record(quota)
    watch.poll(1)
    assert calls == ["ansuman-1"]
    watch.record({**quota, "retry_after_ms": 1000})
    watch.record({"event.name": "user_prompt"})
    watch.poll(20)
    assert calls == ["ansuman-1"]
    watch.record(quota)
    watch.record(quota)
    watch.poll(20)
    watch.worker.join(5)
    watch.poll(21)
    assert calls == ["ansuman-1", "ansuman-1"]


@pytest.mark.parametrize(
    "error",
    [
        OSError("sensitive-native-diagnostic"),
        subprocess.TimeoutExpired(("devin", "sensitive-native-diagnostic"), 30),
    ],
)
def test_quota_watch_reports_check_failures_without_raising(monkeypatch, error):
    def fail(native, account, progress):
        raise error

    monkeypatch.setattr(handoff, "queue_automatic", fail)
    watch = terminal.QuotaWatch("native", "ansuman-1")
    watch.feed(b"Quota exhausted", 0)
    watch.worker.join(5)
    message = watch.message()
    assert "unchanged" in message and "sensitive" not in message


def test_quota_watch_explains_switch_errors(monkeypatch):
    def fail(native, account, progress):
        raise SwitchError("Devin auth status timed out for 'ansuman-2' after 30 seconds.")

    monkeypatch.setattr(handoff, "queue_automatic", fail)
    watch = terminal.QuotaWatch("native", "ansuman-1")
    watch.feed(b"Quota exhausted", 0)
    watch.worker.join(5)
    message = watch.message()
    assert "timed out for 'ansuman-2'" in message and "unchanged" in message


@pytest.mark.parametrize(
    ("exhausted", "trigger"), [(True, b"q"), (False, b"q"), (True, b"t"), (False, b"t")]
)
def test_quota_alert_automatically_resumes_and_continues_on_real_pty(
    signed_in: Native, tmp_path: Path, monkeypatch, exhausted: bool, trigger: bytes
):
    """`q` renders the alert text; `t` only exports the CLI's typed telemetry error."""
    otlp = tmp_path / "quota.otlp"
    otlp.write_bytes(encode_logs([{"event.name": "api_error", "error_kind": "ResourceExhausted"}]))
    monkeypatch.setattr(desktop.browser, "profiles", lambda: ())
    store = signed_in.store
    project = tmp_path / "project"
    project.mkdir()
    with store.lock():
        third = store.add("ansuman-3", None)
    store.credentials(third).write_text("valid-third")
    with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT, title TEXT, working_directory TEXT, "
            "last_activity_at INTEGER, hidden INTEGER DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES ('exact-chat', 'Chat', ?, 1, 0)", (str(project),)
        )
    binary = tmp_path / "fake native"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys, termios, tty\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print('Logged in as offline@example.invalid')\n"
        "    sys.exit(0)\n"
        "account = pathlib.Path(os.environ['XDG_DATA_HOME']).parent.name\n"
        "settings = termios.tcgetattr(0)\n"
        "tty.setraw(0)\n"
        "print('START ' + json.dumps({'account': account, 'args': sys.argv[1:]}), flush=True)\n"
        "print('READY-' + account, flush=True)\n"
        "while True:\n"
        "    key = os.read(0, 1)\n"
        "    if key == b'\\x04':\n"
        "        break\n"
        "    if key == b'q':\n"
        "        sys.stdout.write('\\x1b[1;31mQuota\\x1b[0m\\x1b[1C'\n"
        "                         '\\x1b[1mexhausted\\x1b[0m\\r\\n')\n"
        "        sys.stdout.flush()\n"
        "    if key == b't':\n"
        "        import urllib.request\n"
        "        urllib.request.urlopen(urllib.request.Request(\n"
        "            os.environ['OTEL_EXPORTER_OTLP_LOGS_ENDPOINT'],\n"
        "            data=pathlib.Path(os.environ['FAKE_OTLP']).read_bytes(),\n"
        "            headers={'Content-Type': 'application/x-protobuf'}), timeout=5).read()\n"
        "config = pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'devin/config.json'\n"
        "event = {'hook_event_name':'SessionEnd','session_id':'exact-chat', "
        "'reason':'prompt_input_exit'}\n"
        "for entry in json.loads(config.read_text())['hooks']['SessionEnd']:\n"
        "    for hook in entry['hooks']:\n"
        "        subprocess.run(hook['command'], shell=True, input=json.dumps(event), text=True)\n"
        "termios.tcsetattr(0, termios.TCSADRAIN, settings)\n"
    )
    binary.chmod(0o700)
    driver = tmp_path / "tty driver.py"
    driver.write_text(
        "import time\n"
        "from devin_switch import cli, usage\n"
        "def refresh(store, account, *, force):\n"
        "    assert force\n"
        "    now = time.time()\n"
        f"    used = {{'ansuman-1': {100 if exhausted else 70}, 'ansuman-2': 100}}.get("
        "account.name, 30)\n"
        "    return usage.Usage(status='ok', fetched_at=now, checked_at=now, "
        "daily=usage.Window(used, now+3600, 'available'), "
        "weekly=usage.Window(10, now+86400, 'available'))\n"
        "usage.refresh_account = refresh\n"
        "raise SystemExit(cli.main())\n"
    )
    master, slave = pty.openpty()
    process = subprocess.Popen(
        (sys.executable, str(driver), "run", "--account", "ansuman-1", "--", "--sandbox"),
        env={**os.environ, "DS_BINARY": str(binary), "FAKE_OTLP": str(otlp)},
        cwd=project,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        preexec_fn=terminal.claim_terminal,
    )
    try:
        read_until(master, b"READY-ansuman-1")
        os.write(master, trigger)
        notice = b"ds: Quota alert on ansuman-1. Checking usage"
        if exhausted:
            output = read_until(master, b"READY-ansuman-3")
            assert -1 < output.find(notice) < output.find(b"ansuman-1 is out of usage")
            assert b'"args": ["--sandbox", "--resume", "exact-chat", "--", "continue"]' in output
            assert store.selected().name == "ansuman-3"
        else:
            output = read_until(master, b"this chat is unchanged")
            assert -1 < output.find(notice) < output.find(b"ansuman-1 still reports 30% remaining")
            assert not list((store.root / "handoffs").glob("*.request.json"))
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert not any(run["active"] for run in sessions.runs(store))
    finally:
        os.close(master)
        os.close(slave)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
