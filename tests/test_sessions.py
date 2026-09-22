import json
import os
import select
import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from devin_switch import cli, desktop
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError


def seed_history(store: Store, project: Path) -> None:
    with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT, title TEXT, working_directory TEXT, "
            "created_at INTEGER, last_activity_at INTEGER, hidden INTEGER DEFAULT 0)"
        )
        connection.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 0)",
            [
                ("first-chat", "First task", str(project), 1, 2),
                ("second-chat", "Second task", str(project), 3, 4),
            ],
        )


def test_running_chat_does_not_lock_default_or_other_launches(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = signed_in.store
    store.select(store.accounts()[0])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    calls = []

    def interactive(self, account, arguments):
        calls.append(account.name)
        state = desktop.snapshot(store)
        running = [run for run in state["runs"] if run["active"]]
        assert len(running) == len(calls)
        assert running[0]["account"] == "ansuman-1"
        if len(calls) == 1:
            desktop.action(store, {"action": "select", "account": "ansuman-2"})
            desktop.action(store, {"action": "add", "account": "unused"})
            assert cli.execute(cli.parser().parse_args(["run"]), store) == 0
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert cli.execute(cli.parser().parse_args(["run"]), store) == 0
    assert calls == ["ansuman-1", "ansuman-2"]
    assert store.selected().name == "ansuman-2"
    assert not any(run["active"] for run in desktop.snapshot(store)["runs"])


def test_same_account_can_run_two_repositories(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    project = tmp_path / "other repo"
    project.mkdir()
    calls = []

    def interactive(self, account, arguments):
        calls.append(str(Path.cwd()))
        if len(calls) == 1:
            with monkeypatch.context() as context:
                context.chdir(project)
                assert (
                    cli.execute(
                        cli.parser().parse_args(["run", "--account", account.name]), self.store
                    )
                    == 0
                )
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert (
        cli.execute(cli.parser().parse_args(["run", "--account", "ansuman-1"]), signed_in.store)
        == 0
    )
    assert len(calls) == 2 and calls[0] != calls[1]


def test_remove_requires_confirmation_and_preserves_shared_history(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = signed_in.store
    seed_history(store, tmp_path)
    first, second = store.accounts()
    store.select(first)
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    with pytest.raises(SwitchError, match="Confirm"):
        desktop.action(store, {"action": "remove", "account": first.name})
    assert store.credentials(first).exists()
    desktop.action(store, {"action": "remove", "account": first.name, "confirmed": "true"})
    assert not store.directory(first.name).exists()
    assert store.credentials(second).exists()
    assert not (store.root / "selected").exists()
    assert len(desktop.snapshot(store)["sessions"]) == 2


def test_removal_and_rename_refuse_account_in_use(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)

    def interactive(self, account, arguments):
        with pytest.raises(SwitchError, match="in use"):
            desktop.action(
                self.store, {"action": "remove", "account": account.name, "confirmed": "true"}
            )
        with self.store.lock(), pytest.raises(SwitchError, match="in use"):
            self.store.rename(account, "renamed")
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert (
        cli.execute(cli.parser().parse_args(["run", "--account", "ansuman-1"]), signed_in.store)
        == 0
    )
    assert signed_in.store.credentials(signed_in.store.accounts()[0]).exists()


def test_exact_resume_uses_history_directory_and_rejects_duplicate(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = signed_in.store
    seed_history(store, tmp_path)
    store.select(store.accounts()[0])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    request = {"action": "resume_session", "account": "ansuman-2", "session": "first-chat"}
    reply = desktop.action(store, request)
    launcher = Path(reply["launcher"]).read_text()
    assert "--resume first-chat" in launcher
    assert "--continue" not in launcher
    assert store.selected().name == "ansuman-1"

    def interactive(self, account, arguments):
        assert arguments == ("--resume", "first-chat")
        state = desktop.snapshot(store)
        chat = next(chat for chat in state["sessions"] if chat["id"] == "first-chat")
        assert chat["active"] and chat["account"] == "ansuman-2"
        with pytest.raises(SwitchError, match="open"):
            desktop.action(store, request)
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert (
        cli.execute(
            cli.parser().parse_args(
                ["run", "--account", "ansuman-2", "--", "--resume", "first-chat"]
            ),
            store,
        )
        == 0
    )
    assert not any(run["active"] for run in desktop.snapshot(store)["runs"])


def test_new_chat_allows_distinct_resume_and_new_launch(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seed_history(signed_in.store, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)

    def interactive(self, account, arguments):
        assert desktop.action(
            self.store,
            {"action": "resume_session", "account": account.name, "session": "first-chat"},
        )["launcher"]
        assert desktop.action(
            self.store, {"action": "start", "account": account.name, "project": str(tmp_path)}
        )["launcher"]
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert (
        cli.execute(cli.parser().parse_args(["run", "--account", "ansuman-1"]), signed_in.store)
        == 0
    )


def test_failed_run_releases_locks_and_hides_arguments(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)

    def fail(*args):
        raise OSError("test failure")

    monkeypatch.setattr(Native, "interactive", fail)
    with pytest.raises(OSError):
        cli.execute(
            cli.parser().parse_args(["run", "--account", "ansuman-1", "--", "-p", "secret-prompt"]),
            signed_in.store,
        )
    state = desktop.snapshot(signed_in.store)
    assert not any(run["active"] for run in state["runs"])
    assert "secret-prompt" not in json.dumps(state)
    with signed_in.store.lock():
        signed_in.store.remove(signed_in.store.accounts()[0])


def test_inherited_session_database_cannot_escape_switch(native: Native, monkeypatch):
    monkeypatch.setenv("CHISEL_SESSION_DB", "/other/history.db")
    environment = native.environment(native.store.accounts()[0])
    assert environment["CHISEL_SESSION_DB"] == str(native.store.root / "shared/cli/sessions.db")


def test_separate_processes_share_account_without_blocking_manager(
    signed_in: Native, tmp_path: Path
):
    binary = tmp_path / "holding-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print('Logged in as test@example.invalid')\n"
        "else:\n"
        "    print('ready', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    binary.chmod(0o700)
    environment = {**os.environ, "DS_BINARY": str(binary)}
    store = signed_in.store
    with ExitStack() as stack:
        processes = []
        for _ in range(2):
            process = stack.enter_context(
                subprocess.Popen(
                    (sys.executable, "-m", "devin_switch.cli", "run", "--account", "ansuman-1"),
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
            processes.append(process)
            assert select.select([process.stdout], [], [], 5)[0], "Runner did not become ready"
            assert process.stdout.readline() == b"ready\n"
        state = desktop.snapshot(store)
        assert len([run for run in state["runs"] if run["active"]]) == 2
        with store.lock():
            store.select(store.account("ansuman-2"))
            with pytest.raises(SwitchError, match="in use"):
                store.remove(store.account("ansuman-1"))
        for process in processes:
            process.communicate(timeout=5)
            assert process.returncode == 0
    assert not any(run["active"] for run in desktop.snapshot(store)["runs"])


def test_child_retains_lease_after_parent_closes_its_copy(store: Store):
    account = store.accounts()[0]
    with store.account_lock(account, shared=True) as descriptor:
        child = subprocess.Popen(
            (sys.executable, "-c", "import sys; sys.stdin.read()"),
            stdin=subprocess.PIPE,
            pass_fds=(descriptor,),
        )
    try:
        with pytest.raises(SwitchError, match="in use"), store.account_lock(account):
            pytest.fail("The child still owns its copy of the account lease")
    finally:
        child.communicate(timeout=5)
    with store.account_lock(account):
        pass


def test_history_read_failure_keeps_open_run_visible(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)

    def interactive(self, account, arguments):
        (self.store.root / "shared/cli/sessions.db").write_text("not sqlite")
        state = desktop.snapshot(self.store)
        assert len([run for run in state["runs"] if run["active"]]) == 1
        assert state["history_error"] and not state["sessions"]
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert (
        cli.execute(cli.parser().parse_args(["run", "--account", "ansuman-1"]), signed_in.store)
        == 0
    )


@pytest.mark.parametrize(
    "arguments", [("--continue",), ("-c",), ("--resume=second-chat",), ("-rsecond-chat",)]
)
def test_resume_flags_choose_exact_chat(signed_in: Native, tmp_path: Path, arguments):
    from devin_switch.sessions import resume_arguments

    seed_history(signed_in.store, tmp_path)
    forwarded, selected = resume_arguments(signed_in.store, arguments, tmp_path)
    assert selected == "second-chat"
    assert "--continue" not in forwarded and "-c" not in forwarded


def test_prompt_text_is_not_interpreted_as_resume_flag(signed_in: Native, tmp_path: Path):
    from devin_switch.sessions import resume_arguments

    for arguments in (
        ("--", "--continue"),
        ("-p", "explain --resume"),
        ("--print=--resume",),
        ("--model", "--continue"),
    ):
        assert resume_arguments(signed_in.store, arguments, tmp_path) == (arguments, None)


def test_print_mode_does_not_bypass_resume_guard(
    signed_in: Native, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from devin_switch.sessions import managed, resume_arguments

    seed_history(signed_in.store, tmp_path)
    monkeypatch.chdir(tmp_path)
    for arguments in (
        ("-p", "continue working", "--resume", "first-chat"),
        ("--print=hello", "-rfirst-chat"),
    ):
        assert resume_arguments(signed_in.store, arguments, tmp_path)[1] == "first-chat"
        with managed(signed_in, "ansuman-1", ("--resume", "first-chat"), kind="chat"):
            with (
                pytest.raises(SwitchError, match="open"),
                managed(signed_in, "ansuman-2", arguments),
            ):
                pytest.fail("A second process must not resume an open chat")


def test_profile_removal_never_follows_shared_or_external_links(store: Store, tmp_path: Path):
    account = store.accounts()[0]
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep"
    marker.write_text("keep")
    (store.directory(account.name) / "linked").symlink_to(external, target_is_directory=True)
    shared_marker = store.root / "shared/cli/keep"
    shared_marker.write_text("keep")
    with store.lock():
        store.remove(account)
    assert marker.read_text() == shared_marker.read_text() == "keep"


def test_ended_legacy_launch_is_not_active_with_inherited_lock(store, tmp_path):
    from dataclasses import asdict

    from devin_switch import sessions
    from devin_switch.store import private_directory, write_json

    run = sessions.Run("a" * 32, "ansuman-1", str(tmp_path), "chat", 1, "first-chat", 2)
    private_directory(store.root / "runs")
    write_json(store.root / "runs" / f"{run.id}.json", asdict(run))
    with store.lock(f"run-{run.id}.lock"):
        assert not sessions.runs(store)[0]["active"]


@pytest.mark.parametrize("crash_wrapper", [False, True])
def test_cli_liveness_ignores_inherited_background_leases(signed_in, tmp_path, crash_wrapper):
    import signal
    import time

    from devin_switch import sessions

    binary = tmp_path / "background-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print('Logged in as offline@example.invalid')\n"
        "    sys.exit(0)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "print(json.dumps({'native': os.getpid(), 'background': child.pid}), flush=True)\n"
        "sys.stdin.read()\n"
    )
    binary.chmod(0o700)
    process = subprocess.Popen(
        (sys.executable, "-m", "devin_switch.cli", "run", "--account", "ansuman-1"),
        env={**os.environ, "DS_BINARY": str(binary)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pids = {}
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        pids = json.loads(process.stdout.readline())
        run = sessions.runs(signed_in.store)[0]
        assert run["active"]
        if crash_wrapper:
            process.kill()
            process.wait(timeout=5)
            assert sessions.runs(signed_in.store)[0]["active"]
        process.stdin.close()
        deadline = time.monotonic() + 5
        while sessions.runs(signed_in.store)[0]["active"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not sessions.runs(signed_in.store)[0]["active"]
        assert signed_in.store.locked(f"run-{run['id']}.lock")
        os.kill(pids["background"], 0)
        if not crash_wrapper:
            assert process.wait(timeout=5) == 0
    finally:
        for pid in pids.values():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_distinct_saved_chats_can_resume_in_same_repository(signed_in, tmp_path, monkeypatch):
    from devin_switch import sessions

    seed_history(signed_in.store, tmp_path)
    monkeypatch.chdir(tmp_path)
    with sessions.managed(signed_in, "ansuman-1", ("--resume", "first-chat")):
        with sessions.managed(signed_in, "ansuman-2", ("--resume", "second-chat")):
            assert len([run for run in sessions.runs(signed_in.store) if run["active"]]) == 2
        with pytest.raises(SwitchError, match="open"):
            with sessions.managed(signed_in, "ansuman-2", ("--resume", "first-chat")):
                pytest.fail("Duplicate launch must be rejected")


@pytest.mark.parametrize(
    "stamp,expected", [("original-start", True), ("reused-pid", False), (None, False)]
)
def test_native_identity_not_inherited_lock_determines_liveness(
    store, tmp_path, monkeypatch, stamp, expected
):
    from devin_switch import sessions
    from devin_switch.store import write_json

    run = sessions.Run("b" * 32, "ansuman-1", str(tmp_path), "chat", 1, ended_at=2)
    monkeypatch.setattr(sessions, "process_started", lambda _: "original-start")
    sessions.record_process(store, run.id, os.getpid(), role="native")
    monkeypatch.setattr(sessions, "process_started", lambda _: stamp)
    with store.lock(f"run-{run.id}.lock"):
        assert sessions.active(store, run) is expected
        write_json(
            store.root / "runtime" / f"{run.id}.json", {"pid": os.getpid(), "role": "wrapper"}
        )
        assert not sessions.active(store, run)


def test_process_identity_is_independent_of_timezone(monkeypatch):
    from devin_switch.sessions import process_started

    original = process_started(os.getpid())
    assert original
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    assert process_started(os.getpid()) == original


def test_process_probe_does_not_count_zombies(monkeypatch):
    from devin_switch import sessions

    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, "Thu Sep 17 00:00:00 2026 Z+\n"
        ),
    )
    assert sessions.process_started(12345) is None
