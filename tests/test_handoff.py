import io
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from devin_switch import cli, handoff, sessions, usage
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


@pytest.fixture(autouse=True)
def reported_usage(monkeypatch):
    import time

    def refresh(store, account, *, force):
        assert force
        now = time.time()
        return usage.Usage(
            status="ok",
            daily=usage.Window(20, now + 3600, "available"),
            weekly=usage.Window(40, now + 86400, "available"),
            fetched_at=now,
            checked_at=now,
        )

    monkeypatch.setattr(usage, "refresh_account", refresh)


def setup_project(store: Store, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setattr(handoff, "controller_available", lambda *_: True)


def record_exit(store: Store, session_id="exact-chat", reason="prompt_input_exit") -> None:
    from devin_switch.handoff import record_exit

    record_exit(
        store,
        {"hook_event_name": "SessionEnd", "session_id": session_id, "reason": reason},
    )


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"theme_mode":"dark"}',
        '{"hooks":null,"theme_mode":"dark"}',
        '{"hooks":{"Stop":[]}}',
        '{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"old"}]}]}}',
        '{\n// keep this\n"permissions":{"deny":["Exec(secret)"],}, /* keep too */\n}',
        '{"hooks":{"SessionEnd":[/* keep array comment */],},"label":"http://x/*a*/,}",}',
    ],
)
def test_automatic_hook_preserves_profile_configuration(store, tmp_path, content):
    account = store.accounts()[0]
    path = store.directory(account.name) / "config/devin/config.json"
    path.write_text(content)
    handoff.enable(store, account)
    first = path.read_text()
    before = json.loads(handoff.json_source(content))
    installed = json.loads(handoff.json_source(first))
    assert handoff.HOOK in installed["hooks"]["SessionEnd"]
    installed["hooks"]["SessionEnd"].remove(handoff.HOOK)
    for key, value in before.items():
        if key != "hooks":
            assert installed[key] == value
    for event, hooks in (before.get("hooks") or {}).items():
        assert installed["hooks"][event] == hooks
    if "// keep this" in content:
        assert "// keep this" in first and "/* keep too */" in first
    if "/* keep array comment */" in content:
        assert "/* keep array comment */" in first
    handoff.enable(store, account)
    assert path.read_text() == first
    assert not (tmp_path / ".devin").exists()
    assert installed["otel"] == handoff.TELEMETRY
    assert handoff.telemetry_enabled(store, account)


@pytest.mark.parametrize(
    "otel",
    [
        {"endpoint": "https://collector.example.invalid"},
        {**handoff.TELEMETRY, "enabled": False},
        None,
    ],
)
def test_existing_telemetry_configuration_is_never_redirected(store, otel):
    account = store.accounts()[0]
    path = store.directory(account.name) / "config/devin/config.json"
    path.write_text(json.dumps({"theme_mode": "dark", "otel": otel}))
    handoff.enable(store, account)
    installed = json.loads(path.read_text())
    assert installed["otel"] == otel and handoff.HOOK in installed["hooks"]["SessionEnd"]
    assert not handoff.telemetry_enabled(store, account)


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"hooks":[]}', '{"hooks":{"SessionEnd":{}}}']
)
def test_automatic_hook_preserves_invalid_configuration(store, content):
    account = store.accounts()[0]
    path = store.directory(account.name) / "config/devin/config.json"
    path.write_text(content)
    with pytest.raises(SwitchError, match="config"):
        handoff.enable(store, account)
    assert path.read_text() == content


@pytest.mark.parametrize("linked_directory", [False, True])
def test_automatic_hook_refuses_symlinks(store, tmp_path, linked_directory):
    account = store.accounts()[0]
    external = tmp_path / "external"
    external.mkdir()
    target = external / "config.json"
    target.write_text("{}")
    directory = store.directory(account.name) / "config/devin"
    (directory / "config.json").unlink()
    if linked_directory:
        (directory / "mcp_config.json").unlink()
        directory.rmdir()
        directory.symlink_to(external, target_is_directory=True)
    else:
        (directory / "config.json").symlink_to(target)
    with pytest.raises(SwitchError, match="linked"):
        handoff.enable(store, account)
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
    assert "Ctrl+D" not in output.out
    assert "automatically" in output.out
    assert "default" in output.err
    assert not (tmp_path / ".devin").exists()


def test_handoff_with_inherited_background_lease(signed_in, tmp_path, monkeypatch):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches, children = [], []

    def interactive(self, account, arguments):
        launches.append((account.name, arguments))
        if len(launches) == 1:
            children.append(
                subprocess.Popen(
                    (sys.executable, "-c", "import sys; sys.stdin.read()"),
                    stdin=subprocess.PIPE,
                    pass_fds=self.lock_fds,
                )
            )
            monkeypatch.setenv("DS_RUN_ID", self.run_id)
            cli.execute(cli.parser().parse_args(["switch", "ansuman-2"]), store)
            record_exit(store)
        else:
            assert children[0].poll() is None
            assert len([run for run in sessions.runs(store) if run["active"]]) == 1
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    try:
        assert cli.execute(cli.parser().parse_args(["run", "--account", "ansuman-1"]), store) == 0
        assert launches == [("ansuman-1", ()), ("ansuman-2", ("--resume", "exact-chat"))]
    finally:
        for child in children:
            child.communicate(timeout=5)


def test_handoff_waits_for_brief_store_contention(signed_in, tmp_path, monkeypatch):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    with sessions.managed(signed_in, "ansuman-1", (), can_handoff=True) as (runner, _, _):
        monkeypatch.setenv("DS_RUN_ID", runner.run_id)
        handoff.queue(store, handoff.current(store), store.account("ansuman-2"))
        record_exit(store)
    with subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import os, time; from pathlib import Path; from devin_switch.store import Store; "
            "store = Store(Path(os.environ['DS_HOME'])); "
            "lease = store.lock(); lease.__enter__(); print('locked', flush=True); time.sleep(0.3)",
        ),
        stdout=subprocess.PIPE,
    ) as holder:
        assert holder.stdout.readline() == b"locked\n"
        assert handoff.finish(runner, 0, ()) == ("ansuman-2", ("--resume", "exact-chat"))
        assert holder.wait(timeout=5) == 0


@pytest.mark.parametrize("initial_default", [None, "ansuman-1"])
def test_explicit_switch_sets_default_only_when_resumed_launch_starts(
    signed_in, tmp_path, monkeypatch, initial_default
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    if initial_default:
        store.select(store.account(initial_default))
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        assert cli.selected_name(store) == initial_default
        with self.launch_selection(account):
            assert cli.selected_name(store) == initial_default
        launches.append(account.name)
        if len(launches) == 1:
            assert not self.select_on_start
            monkeypatch.setenv("DS_RUN_ID", self.run_id)
            cli.execute(cli.parser().parse_args(["switch", "ansuman-2"]), store)
            assert cli.selected_name(store) == initial_default
            record_exit(store)
            assert cli.selected_name(store) == initial_default
        else:
            assert self.select_on_start
            assert store.selected().name == "ansuman-2"
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    assert cli.execute(cli.parser().parse_args(["run", "--account", "ansuman-1"]), store) == 0
    assert launches == ["ansuman-1", "ansuman-2"]
    assert store.selected().name == "ansuman-2"


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


@pytest.mark.parametrize("case", ["same-account", "signed-out", "no-controller", "print"])
def test_invalid_switch_does_not_queue(signed_in: Native, tmp_path, monkeypatch, case):
    store = signed_in.store
    monkeypatch.chdir(tmp_path)
    if case != "no-controller":
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
        (("--continue", "--config", "a path/config.json"), None),
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


def test_next_account_with_credits_is_chosen_without_checking_the_rest(signed_in, monkeypatch):
    import time

    store = signed_in.store
    with store.lock():
        extra = [store.add(f"ansuman-{number}", None) for number in range(3, 12)]
    for account in extra:
        store.credentials(account).write_text(f"valid-{account.name}")
    now = time.time()

    def saved(account, used):
        reading = usage.Usage(
            status="ok",
            daily=usage.Window(used, now + 1000, "available"),
            weekly=usage.Window(used, now + 1000, "available"),
            fetched_at=now - 3600,
            checked_at=now - 3600,
        )
        (store.directory(account.name) / "usage.json").write_text(
            json.dumps(
                {
                    "credential_fingerprint": usage.fingerprint(
                        usage.credential_bytes(store, account)
                    ),
                    "usage": asdict(reading),
                }
            )
        )

    # Saved (stale) readings only order the checks: exhausted and unknown logins go last.
    saved(store.account("ansuman-2"), 100)
    saved(store.account("ansuman-7"), 10)
    saved(store.account("ansuman-9"), 50)
    checked = []

    def refresh(store, account, *, force):
        assert force
        checked.append(account.name)
        used = {"ansuman-9": 100, "ansuman-7": 60}.get(account.name, 0)
        return usage.Usage(
            status="ok",
            daily=usage.Window(used, time.time() + 1000, "available"),
            weekly=usage.Window(used, time.time() + 1000, "available"),
            fetched_at=time.time(),
        )

    monkeypatch.setattr(usage, "refresh_account", refresh)
    account, remaining = handoff.choose_best(signed_in, "ansuman-1")
    assert (account.name, remaining) == ("ansuman-7", 40)
    assert sorted(checked) == ["ansuman-10", "ansuman-11", "ansuman-7", "ansuman-9"]


@pytest.mark.parametrize("status", ["stale", "unavailable", "sign_in"])
def test_no_known_quota_never_selects_unknown_as_unlimited(signed_in, monkeypatch, status):
    monkeypatch.setattr(usage, "refresh_account", lambda *_a, **_k: usage.Usage(status=status))
    with pytest.raises(SwitchError, match="confirmed remaining usage"):
        handoff.choose_best(signed_in, "ansuman-1")


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
        with self.launch_selection(account):
            pass
        launches.append((account.name, arguments))
        assert store.selected().name == ("ansuman-2" if len(launches) == 1 else account.name)
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
    assert store.selected().name == "ansuman-1"


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
        with self.launch_selection(account):
            pass
        launches.append(account.name)
        if len(launches) == 1:
            monkeypatch.setenv("DS_RUN_ID", self.run_id)
            cli.execute(cli.parser().parse_args(["switch"]), store)
            record_exit(store)
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    monkeypatch.chdir(other)
    with sessions.managed(signed_in, "ansuman-1", ()):
        monkeypatch.chdir(tmp_path)
        assert cli.execute(cli.parser().parse_args(["run"]), store) == 0
        assert launches == ["ansuman-1", "ansuman-2"]
        assert store.selected().name == "ansuman-2"
        active = [run for run in sessions.runs(store) if run["active"]]
        assert len(active) == 1 and active[0]["account"] == "ansuman-1"
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


@pytest.mark.parametrize("flag", ["--cancel"])
def test_setup_and_cancel_reject_account_argument(store, flag):
    with pytest.raises(SwitchError, match="not both"):
        cli.execute(cli.parser().parse_args(["switch", "ansuman-2", flag]), store)


def test_switch_continue_resumes_exact_chat_with_one_continue_prompt(
    signed_in: Native, tmp_path: Path, monkeypatch, capsys
):
    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    seed_history(store, tmp_path)
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    launches = []

    def interactive(self, account, arguments):
        launches.append((account.name, arguments))
        if len(launches) < 3:
            monkeypatch.setenv("DS_RUN_ID", self.run_id)
            command = ["switch", "--continue"] if len(launches) == 1 else ["switch"]
            assert cli.execute(cli.parser().parse_args(command), store) == 0
            record_exit(store)
        return 0

    monkeypatch.setattr(Native, "interactive", interactive)
    arguments = ["run", "--account", "ansuman-1", "--", "--sandbox", "--", "private-initial"]
    assert cli.execute(cli.parser().parse_args(arguments), store) == 0
    assert launches == [
        ("ansuman-1", ("--sandbox", "--", "private-initial")),
        ("ansuman-2", ("--sandbox", "--resume", "exact-chat", "--", "continue")),
        ("ansuman-1", ("--sandbox", "--resume", "exact-chat")),
    ]
    output = capsys.readouterr()
    assert "Then sending “continue”" in output.out
    assert "Sending “continue”" in output.err
    assert "ds run --account ansuman-2 -- --sandbox --resume exact-chat -- continue" in output.err


def test_auto_switch_setting_defaults_on_and_fails_closed(store, capsys):
    assert handoff.auto_switch_enabled(store)
    handoff.settings_path(store).write_text('{"other": 1}')
    assert handoff.auto_switch_enabled(store)
    assert cli.execute(cli.parser().parse_args(["auto-switch", "off"]), store) == 0
    assert not handoff.auto_switch_enabled(store)
    assert json.loads(handoff.settings_path(store).read_text()) == {
        "other": 1,
        "auto_switch": False,
    }
    assert "off" in capsys.readouterr().out
    assert cli.execute(cli.parser().parse_args(["auto-switch", "on"]), store) == 0
    assert handoff.auto_switch_enabled(store)
    for invalid in ("not json", "[]", '{"auto_switch": "yes"}'):
        handoff.settings_path(store).write_text(invalid)
        assert not handoff.auto_switch_enabled(store)
    assert cli.execute(cli.parser().parse_args(["auto-switch"]), store) == 0
    assert "off" in capsys.readouterr().out


def test_continue_is_rejected_for_queued_gui_handoffs(store):
    for command in (
        ["switch", "--continue", "--session", "chat"],
        ["switch", "--continue", "--cancel"],
    ):
        with pytest.raises(SwitchError):
            cli.execute(cli.parser().parse_args(command), store)


def test_best_account_skips_logins_already_tried(signed_in, monkeypatch):
    with signed_in.store.lock():
        third = signed_in.store.add("ansuman-3", None)
    signed_in.store.credentials(third).write_text("valid-third")
    assert handoff.choose_best(signed_in, "ansuman-1", frozenset({"ansuman-2"}))[0] == third
    with pytest.raises(SwitchError, match="confirmed remaining usage"):
        handoff.choose_best(signed_in, "ansuman-1", frozenset({"ansuman-2", "ansuman-3"}))


@pytest.mark.parametrize(
    "case", ["exhausted", "remaining", "unknown", "disabled", "no-controller", "pending"]
)
def test_automatic_terminal_handoff_requires_confirmed_exhaustion(
    signed_in: Native, tmp_path, monkeypatch, case
):
    import time

    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    monkeypatch.setattr(handoff, "controller_available", lambda *_: case != "no-controller")

    def refresh(store, account, *, force):
        assert force
        now = time.time()
        if account.name == "ansuman-1" and case == "unknown":
            return usage.Usage(status="unavailable")
        used = 100 if account.name == "ansuman-1" and case != "remaining" else 20
        return usage.Usage(
            status="ok",
            daily=usage.Window(used, now + 3600, "available"),
            weekly=usage.Window(40, now + 86400, "available"),
            fetched_at=now,
            checked_at=now,
        )

    monkeypatch.setattr(usage, "refresh_account", refresh)
    if case == "disabled":
        handoff.set_auto_switch(store, False)
    account = store.account("ansuman-1")
    with sessions.managed(signed_in, account.name, (), can_handoff=True) as (runner, _, _):
        path = handoff.state_path(store, runner.run_id, "request")
        if case == "pending":
            handoff.queue(
                store, {"id": runner.run_id, "account": account.name}, store.accounts()[1]
            )
        notices = []
        message = handoff.queue_automatic(runner, account, notices.append)
        assert len(notices) == (case != "disabled")
        assert all("keep this chat open" in notice for notice in notices)
        if case == "exhausted":
            assert "ansuman-2" in message and "continue" in message
            request = json.loads(path.read_text())
            assert request["account"] == "ansuman-2"
            assert request["continue"] is True and request["automatic"] is True
        else:
            if case == "remaining":
                assert "60% remaining" in message and "unchanged" in message
            elif case == "unknown":
                assert "Could not confirm" in message and "unchanged" in message
            else:
                assert message is None
            assert path.exists() == (case == "pending")
            if case == "pending":
                assert json.loads(path.read_text())["automatic"] is False


def test_automatic_handoff_skips_a_login_whose_auth_check_hangs(
    signed_in: Native, tmp_path, monkeypatch
):
    from dataclasses import replace

    from devin_switch import native as native_module

    store = signed_in.store
    setup_project(store, tmp_path, monkeypatch)
    add_logins(signed_in, 1)
    store.credentials(store.account("ansuman-2")).write_text("valid-slow")
    slow = tmp_path / "slow-cli"
    slow.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "auth status" ] && '
        '[ "$(cat "$XDG_DATA_HOME/devin/credentials.toml")" = valid-slow ]; then exec sleep 5; fi\n'
        f'exec "{signed_in.binary}" "$@"\n'
    )
    slow.chmod(0o700)
    monkeypatch.setattr(native_module, "CAPTURE_TIMEOUT", 0.5)
    monkeypatch.setattr(handoff, "controller_available", lambda *_: True)
    monkeypatch.setattr(
        usage,
        "refresh_account",
        lambda store, account, *, force: full_usage(100 if account.name == "ansuman-1" else 20),
    )
    native = replace(signed_in, binary=slow)
    with pytest.raises(SwitchError, match="timed out for 'ansuman-2'"):
        native.require_login(store.account("ansuman-2"))
    account = store.account("ansuman-1")
    with sessions.managed(native, account.name, (), can_handoff=True) as (runner, _, _):
        message = handoff.queue_automatic(runner, account)
        assert "Switching to ansuman-3" in message
        request = json.loads(handoff.state_path(store, runner.run_id, "request").read_text())
        assert request["account"] == "ansuman-3" and request["automatic"] is True


def full_usage(used: float = 0) -> usage.Usage:
    import time

    now = time.time()
    return usage.Usage(
        status="ok",
        daily=usage.Window(used, now + 3600, "available"),
        weekly=usage.Window(used, now + 86400, "available"),
        fetched_at=now,
        checked_at=now,
    )


def add_logins(native: Native, count: int) -> None:
    with native.store.lock():
        for index in range(3, 3 + count):
            account = native.store.add(f"ansuman-{index}", None)
            native.store.credentials(account).write_text(f"valid-{account.name}")


def test_best_account_reuses_fresh_readings_and_refreshes_stale_ones(signed_in, monkeypatch):
    from dataclasses import asdict, replace

    add_logins(signed_in, 2)
    store = signed_in.store
    for name, used, age in (("ansuman-2", 30, 0), ("ansuman-3", 10, usage.STALE_SECONDS + 5)):
        account = store.account(name)
        reading = full_usage(used)
        reading = replace(reading, fetched_at=reading.fetched_at - age)
        (store.directory(name) / "usage.json").write_text(
            json.dumps(
                {
                    "credential_fingerprint": usage.fingerprint(
                        usage.credential_bytes(store, account)
                    ),
                    "usage": asdict(reading),
                }
            )
        )
    refreshed = []

    def refresh(store, account, *, force):
        assert force
        refreshed.append(account.name)
        return full_usage(50)

    monkeypatch.setattr(usage, "refresh_account", refresh)
    # The stale 90% reading ranks ansuman-3 first, and its fresh check still confirms credits.
    assert handoff.choose_best(signed_in, "ansuman-1") == (store.account("ansuman-3"), 50)
    # ansuman-2's fresh saved reading is reused instead of refreshed.
    assert sorted(refreshed) == ["ansuman-3", "ansuman-4"]


def test_next_login_search_stops_after_the_first_batch_with_credits(signed_in, monkeypatch):
    add_logins(signed_in, 8)
    refreshed = []

    def refresh(store, account, *, force):
        refreshed.append(account.name)
        return full_usage(0 if account.name == "ansuman-2" else 10)

    monkeypatch.setattr(usage, "refresh_account", refresh)
    ordered = sorted(a.name for a in signed_in.store.accounts() if a.name != "ansuman-1")
    account, remaining = handoff.choose_best(signed_in, "ansuman-1")
    assert (account.name, remaining) == (ordered[0], 100 if ordered[0] == "ansuman-2" else 90)
    assert sorted(refreshed) == sorted(ordered[: handoff.CHECK_BATCH])


def test_best_account_keeps_searching_when_full_login_is_signed_out(signed_in, monkeypatch):
    add_logins(signed_in, 1)
    store = signed_in.store
    store.credentials(store.account("ansuman-2")).write_text("expired")
    monkeypatch.setattr(
        usage,
        "refresh_account",
        lambda store, account, *, force: full_usage(0 if account.name == "ansuman-2" else 25),
    )
    assert handoff.choose_best(signed_in, "ansuman-1") == (store.account("ansuman-3"), 75)
