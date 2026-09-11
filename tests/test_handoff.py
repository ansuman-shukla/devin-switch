import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from devin_switch import cli, sessions
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError


def seed_history(store: Store, project: Path) -> None:
    with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT, title TEXT, working_directory TEXT, "
            "last_activity_at INTEGER, hidden INTEGER DEFAULT 0)"
        )
        connection.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, 0)",
            [("exact-chat", "Original", str(project), 1), ("newer-chat", "Other", str(project), 2)],
        )


def setup_project(store: Store, project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(project)
    assert cli.execute(cli.parser().parse_args(["switch", "--setup"]), store) == 0
    return project / ".devin/hooks.v1.json"


def record_exit(store: Store, session_id="exact-chat", reason="prompt_input_exit") -> None:
    from devin_switch.handoff import record_exit

    record_exit(
        store,
        {"hook_event_name": "SessionEnd", "session_id": session_id, "reason": reason},
    )


def test_setup_merges_hooks_idempotently(store: Store, tmp_path: Path, monkeypatch):
    from devin_switch.handoff import HOOK_COMMAND

    directory = tmp_path / ".devin"
    directory.mkdir()
    path = directory / "hooks.v1.json"
    existing = {"Stop": [{"hooks": [{"type": "command", "command": "check-tests"}]}]}
    existing["SessionEnd"] = [{"hooks": [{"type": "command", "command": "existing-cleanup"}]}]
    path.write_text(json.dumps(existing))
    setup_project(store, tmp_path, monkeypatch)
    first = path.read_bytes()
    setup_project(store, tmp_path, monkeypatch)
    assert path.read_bytes() == first
    installed = json.loads(first)
    assert installed["Stop"] == existing["Stop"]
    assert installed["SessionEnd"][0] == existing["SessionEnd"][0]
    assert installed["SessionEnd"][1]["hooks"][0]["command"] == HOOK_COMMAND
    assert str(tmp_path) not in HOOK_COMMAND
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("content", ["not json", "[]", '{"SessionEnd": {}}'])
def test_setup_preserves_invalid_configuration(store: Store, tmp_path, monkeypatch, content):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".devin"
    directory.mkdir()
    path = directory / "hooks.v1.json"
    path.write_text(content)
    with pytest.raises(SwitchError, match="hooks"):
        cli.execute(cli.parser().parse_args(["switch", "--setup"]), store)
    assert path.read_text() == content


@pytest.mark.parametrize("linked_directory", [False, True])
def test_setup_refuses_symlinks(store: Store, tmp_path, monkeypatch, linked_directory):
    monkeypatch.chdir(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    target = external / "hooks.v1.json"
    target.write_text("{}")
    directory = tmp_path / ".devin"
    if linked_directory:
        directory.symlink_to(external, target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "hooks.v1.json").symlink_to(target)
    with pytest.raises(SwitchError, match="linked"):
        cli.execute(cli.parser().parse_args(["switch", "--setup"]), store)
    assert target.read_text() == "{}"


def test_switch_requires_live_managed_chat(store: Store, monkeypatch):
    for value in (None, "../selected", "a" * 32):
        monkeypatch.delenv("DS_RUN_ID", raising=False)
        if value:
            monkeypatch.setenv("DS_RUN_ID", value)
        with pytest.raises(SwitchError, match="ds run"):
            cli.execute(cli.parser().parse_args(["switch"]), store)


def test_switch_resumes_exact_exit_chat_and_cycles_from_bound_account(
    signed_in: Native, tmp_path: Path, monkeypatch, capsys
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    store.select(store.account("ansuman-1"))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        launches.append((account.name, arguments))
        with monkeypatch.context() as child:
            for key, value in self.environment(account).items():
                if key.startswith("DS_"):
                    child.setenv(key, value)
            if len(launches) == 1:
                store.select(store.account("ansuman-2"))
                assert cli.execute(cli.parser().parse_args(["switch"]), store) == 0
                record_exit(store, "newer-chat", "clear")
                record_exit(store)
            else:
                assert len([run for run in sessions.runs(store) if run["active"]]) == 1
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert cli.execute(cli.parser().parse_args(["run"]), store) == 0
    assert launches == [("ansuman-1", ()), ("ansuman-2", ("--resume", "exact-chat"))]
    assert store.selected().name == "ansuman-2"
    assert not any(run["active"] for run in sessions.runs(store))
    output = capsys.readouterr()
    assert "Ctrl+D" in output.out
    assert "default" in output.err


@pytest.mark.parametrize(
    "action", ["cancel", "missing-hook", "crash", "clear-only", "missing-chat"]
)
def test_handoff_never_guesses_or_restarts_on_failure(
    signed_in: Native, tmp_path, monkeypatch, action
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    store.select(store.account("ansuman-1"))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        launches.append(account.name)
        assert len(launches) == 1
        monkeypatch.setenv("DS_RUN_ID", self.run_id)
        assert cli.execute(cli.parser().parse_args(["switch", "ansuman-2"]), store) == 0
        if action == "cancel":
            assert cli.execute(cli.parser().parse_args(["switch", "--cancel"]), store) == 0
            record_exit(store)
        elif action == "clear-only":
            record_exit(store, reason="clear")
        elif action == "missing-chat":
            record_exit(store, "not-saved")
        elif action == "crash":
            record_exit(store)
            return 7
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    if action == "cancel":
        assert cli.execute(cli.parser().parse_args(["run"]), store) == 0
    else:
        with pytest.raises(SwitchError):
            cli.execute(cli.parser().parse_args(["run"]), store)
    assert launches == ["ansuman-1"]
    assert store.selected().name == "ansuman-1"
    assert not any(run["active"] for run in sessions.runs(store))


@pytest.mark.parametrize("case", ["same-account", "signed-out", "no-setup", "print"])
def test_invalid_switch_does_not_queue(signed_in: Native, tmp_path, monkeypatch, case):
    store = signed_in.store
    monkeypatch.chdir(tmp_path)
    if case != "no-setup":
        setup_project(store, tmp_path, monkeypatch)
    store.select(store.account("ansuman-1"))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    if case == "signed-out":
        store.credentials(store.account("ansuman-2")).write_text("")

    def interactive(self, account, arguments):
        if self.run_id:
            monkeypatch.setenv("DS_RUN_ID", self.run_id)
        else:
            monkeypatch.delenv("DS_RUN_ID", raising=False)
        target = "ansuman-1" if case == "same-account" else "ansuman-2"
        with pytest.raises(SwitchError):
            cli.execute(cli.parser().parse_args(["switch", target]), store)
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    arguments = ["run", "--", "-p", "private-prompt"] if case == "print" else ["run"]
    assert cli.execute(cli.parser().parse_args(arguments), store) == 0
    assert not list((store.root / "handoffs").glob("*.request.json"))


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ((), ()),
        (("--", "initial private prompt", "--sandbox"), ()),
        (("--resume=old", "--model", "opus", "--", "do not replay"), ()),
        (("-rold", "--prompt-file=private.txt", "--sandbox"), ("--sandbox",)),
        (("--continue", "--config", "a path/config.json"), ("--config", "a path/config.json")),
        (("--permission-mode=accept-edits",), ("--permission-mode=accept-edits",)),
        (
            ("--export", "a file.json", "--respect-workspace-trust", "true"),
            ("--export", "a file.json", "--respect-workspace-trust", "true"),
        ),
        (("--export", "--sandbox"), ("--export", "--sandbox")),
        (("--print", "private-prompt-marker"), None),
        (("-pprivate",), None),
        (("--print=private",), None),
        (("auth", "logout"), None),
        (("acp",), None),
        (("--help",), None),
        (("--unknown-option",), None),
        (("--config",), None),
    ],
)
def test_restart_options_never_replay_prompts_or_override_saved_model(arguments, expected):
    from devin_switch.handoff import restart_options

    assert restart_options(arguments) == expected


def test_native_environment_pins_handoff_context(native: Native, monkeypatch):
    from dataclasses import replace

    monkeypatch.setenv("DS_RUN_ID", "old-run")
    monkeypatch.setenv("DS_HOME", "/wrong-state")
    monkeypatch.setenv("DS_BINARY", "/wrong-binary")
    monkeypatch.setenv("DS_EXECUTABLE", "/wrong-wrapper")
    monkeypatch.setenv("DS_FROZEN", "1")
    account = native.store.accounts()[0]
    clean = native.environment(account)
    assert "DS_RUN_ID" not in clean
    assert "DS_EXECUTABLE" not in clean
    assert "DS_FROZEN" not in clean
    assert clean["DS_HOME"] == str(native.store.root)
    assert clean["DS_BINARY"] == str(native.binary)
    bound = replace(native, run_id="a" * 32).environment(account)
    assert bound["DS_RUN_ID"] == "a" * 32
    assert bound["DS_EXECUTABLE"] == sys.executable
    assert bound["DS_FROZEN"] == "0"


def test_internal_hook_rejects_invalid_input_without_echoing_it(store, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ds", "_session-end"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("private invalid payload"))
    assert cli.main() == 1
    assert "private-prompt-marker" not in capsys.readouterr().err


@pytest.mark.parametrize("frozen", [False, True])
def test_hook_command_quotes_runtime_and_noops_outside_switch(tmp_path, frozen):
    from devin_switch.handoff import HOOK_COMMAND

    environment = {key: value for key, value in os.environ.items() if not key.startswith("DS_")}
    result = subprocess.run(
        HOOK_COMMAND, shell=True, env=environment, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
    executable = tmp_path / "runtime with ' quotes"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o700)
    environment.update(
        DS_RUN_ID="a" * 32, DS_EXECUTABLE=str(executable), DS_FROZEN="1" if frozen else "0"
    )
    result = subprocess.run(
        HOOK_COMMAND, shell=True, env=environment, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    expected = ["_session-end"] if frozen else ["-m", "devin_switch.cli", "_session-end"]
    assert result.stdout.splitlines() == expected


def test_real_process_handoff_keeps_terminal_directory_and_no_prompt_replay(
    signed_in: Native, tmp_path: Path, monkeypatch
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    store.select(store.account("ansuman-1"))
    binary = tmp_path / "fake native"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['auth', 'status']:\n"
        "    print('Logged in as offline@example.invalid')\n"
        "    sys.exit(0)\n"
        "account = pathlib.Path(os.environ['XDG_DATA_HOME']).parent.name\n"
        "print(json.dumps({'account': account, 'args': args, 'cwd': os.getcwd()}), flush=True)\n"
        "if account == 'ansuman-1':\n"
        f"    command = {shlex.join((sys.executable, '-m', 'devin_switch.cli', 'switch'))!r}\n"
        "    result = subprocess.run(command, shell=True, capture_output=True, text=True)\n"
        "    assert result.returncode == 0, result.stderr\n"
        "    hooks = json.loads(pathlib.Path('.devin/hooks.v1.json').read_text())\n"
        "    command = hooks['SessionEnd'][-1]['hooks'][0]['command']\n"
        "    event = {'hook_event_name': 'SessionEnd', 'session_id': 'exact-chat', "
        "'reason': 'prompt_input_exit'}\n"
        "    result = subprocess.run(command, shell=True, input=json.dumps(event), "
        "capture_output=True, text=True)\n"
        "    assert result.returncode == 0, result.stderr\n"
    )
    binary.chmod(0o700)
    environment = {**os.environ, "DS_BINARY": str(binary)}
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "devin_switch.cli",
            "run",
            "--",
            "--sandbox",
            "--",
            "private-prompt-marker",
        ),
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    launches = [json.loads(line) for line in result.stdout.splitlines()]
    assert launches == [
        {
            "account": "ansuman-1",
            "args": ["--sandbox", "--", "private-prompt-marker"],
            "cwd": str(tmp_path),
        },
        {
            "account": "ansuman-2",
            "args": ["--sandbox", "--resume", "exact-chat"],
            "cwd": str(tmp_path),
        },
    ]
    assert store.selected().name == "ansuman-1"
    assert not any(run["active"] for run in sessions.runs(store))
    assert "private-prompt-marker" not in json.dumps(sessions.overview(store))


def test_repeated_handoffs_follow_exit_id_not_original_resume(
    signed_in: Native, tmp_path, monkeypatch
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    store.select(store.account("ansuman-2"))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        launches.append((account.name, arguments))
        monkeypatch.setenv("DS_RUN_ID", self.run_id)
        if len(launches) < 3:
            assert cli.execute(cli.parser().parse_args(["switch"]), store) == 0
            record_exit(store, "exact-chat" if len(launches) == 1 else "newer-chat")
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    initial = [
        "--model",
        "opus",
        "--permission-mode",
        "normal",
        "--sandbox",
        "--prompt-file",
        "do-not-replay.txt",
        "--resume",
        "newer-chat",
    ]
    assert (
        cli.execute(
            cli.parser().parse_args(["run", "--account", "ansuman-1", "--", *initial]), store
        )
        == 0
    )
    assert launches == [
        ("ansuman-1", tuple(initial)),
        ("ansuman-2", ("--permission-mode", "normal", "--sandbox", "--resume", "exact-chat")),
        ("ansuman-1", ("--permission-mode", "normal", "--sandbox", "--resume", "newer-chat")),
    ]
    assert store.selected().name == "ansuman-2"


@pytest.mark.parametrize("same_project", [False, True])
def test_handoff_respects_other_live_chats(signed_in: Native, tmp_path, monkeypatch, same_project):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    other = tmp_path if same_project else tmp_path / "other-repo"
    other.mkdir(exist_ok=True)
    store.select(store.account("ansuman-1"))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        launches.append(account.name)
        if len(launches) == 1:
            monkeypatch.setenv("DS_RUN_ID", self.run_id)
            cli.execute(cli.parser().parse_args(["switch"]), store)
            record_exit(store)
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    monkeypatch.chdir(other)
    with sessions.managed(signed_in, "ansuman-2", ()):
        monkeypatch.chdir(tmp_path)
        if same_project:
            with pytest.raises(SwitchError, match="open"):
                cli.execute(cli.parser().parse_args(["run"]), store)
            assert launches == ["ansuman-1"]
        else:
            assert cli.execute(cli.parser().parse_args(["run"]), store) == 0
            assert launches == ["ansuman-1", "ansuman-2"]
    assert not any(run["active"] for run in sessions.runs(store))


@pytest.mark.parametrize("problem", ["signed-out", "removed", "wrong-project"])
def test_changed_destination_cannot_start_wrong_chat(
    signed_in: Native, tmp_path, monkeypatch, problem
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    store.select(store.account("ansuman-1"))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        launches.append(account.name)
        assert len(launches) == 1
        monkeypatch.setenv("DS_RUN_ID", self.run_id)
        cli.execute(cli.parser().parse_args(["switch"]), store)
        record_exit(store)
        target = store.account("ansuman-2")
        if problem == "signed-out":
            store.credentials(target).write_text("")
        elif problem == "removed":
            with store.lock():
                store.remove(target)
        else:
            other = tmp_path / "other"
            other.mkdir()
            with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
                connection.execute(
                    "UPDATE sessions SET working_directory = ? WHERE id = ?",
                    (str(other), "exact-chat"),
                )
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    with pytest.raises(SwitchError):
        cli.execute(cli.parser().parse_args(["run"]), store)
    assert launches == ["ansuman-1"]
    assert store.selected().name == "ansuman-1"
    assert not any(run["active"] for run in sessions.runs(store))


@pytest.mark.parametrize("session_id", [None, {}, "../selected", "bad\nID", "-r"])
def test_exit_id_must_be_valid(store, session_id):
    with pytest.raises(SwitchError, match="valid conversation ID"):
        record_exit(store, session_id)


def test_handoff_keeps_launch_metadata_readable_by_older_managers(signed_in: Native):
    store = signed_in.store
    with sessions.managed(signed_in, "ansuman-1", (), can_handoff=True) as (runner, _, _):
        path = store.root / "runs" / f"{runner.run_id}.json"
        assert set(json.loads(path.read_text())) == {
            "id",
            "account",
            "project",
            "kind",
            "started_at",
            "session_id",
            "ended_at",
        }


@pytest.mark.parametrize("flag", ["--setup", "--cancel"])
def test_setup_and_cancel_reject_account_argument(store, flag):
    with pytest.raises(SwitchError, match="not both"):
        cli.execute(cli.parser().parse_args(["switch", "ansuman-2", flag]), store)
