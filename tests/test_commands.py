import json
import os
import subprocess
from pathlib import Path

import pytest

from devin_switch import browser, cli
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError


def test_run_forwards_arguments_directory_and_exit_code_without_switching(
    signed_in: Native,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = signed_in.store
    first, second = store.accounts()
    store.select(first)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_EXIT", "7")
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    args = cli.parser().parse_args(
        [
            "run",
            "--account",
            second.name,
            "--",
            "--resume",
            "existing-session",
            "a prompt with spaces",
        ]
    )
    assert cli.execute(args, store) == 7
    output = json.loads(capfd.readouterr().out)
    assert output == {
        "account": "ansuman-2",
        "cwd": str(tmp_path),
        "args": ["--resume", "existing-session", "a prompt with spaces"],
    }
    assert store.selected() == first


def test_login_uses_selected_chrome_profile_and_manual_flow(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = native.store
    with store.lock():
        account = store.add("browser-account", "Profile 11")
    opened: list[str] = []
    monkeypatch.setattr(browser, "open_login", opened.append)
    monkeypatch.setattr(cli, "find_binary", lambda: native.binary)

    def login(self: Native, selected: object, arguments: tuple[str, ...]) -> int:
        assert selected == account
        assert arguments == ("auth", "login", "--force-manual-token-flow")
        self.store.credentials(account).write_text("valid-new-account")
        return 0

    monkeypatch.setattr(Native, "interactive", login)
    assert cli.execute(cli.parser().parse_args(["login", account.name]), store) == 0
    assert opened == ["Profile 11"]
    assert native.authenticated(account)


def test_existing_login_never_opens_browser(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "find_binary", lambda: signed_in.binary)
    monkeypatch.setattr(browser, "open_login", lambda _: pytest.fail("Unexpected browser login"))
    monkeypatch.setattr(Native, "interactive", lambda *_: pytest.fail("Unexpected CLI login"))
    assert cli.execute(cli.parser().parse_args(["login", "ansuman-1"]), signed_in.store) == 0


def test_native_login_zero_exit_without_credentials_is_a_failure(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "find_binary", lambda: native.binary)
    monkeypatch.setattr(Native, "interactive", lambda *_: 0)
    with pytest.raises(SwitchError, match="No saved login"):
        cli.execute(cli.parser().parse_args(["login", "ansuman-1"]), native.store)
    assert not (native.store.root / "selected").exists()


def test_profiles_only_read_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "Local State").write_text(
        json.dumps(
            {"profile": {"info_cache": {"Profile 11": {"name": "Ansuman", "user_name": "a@test"}}}}
        )
    )
    monkeypatch.setattr(browser, "CHROME_DATA", tmp_path)
    assert browser.profiles() == (browser.ChromeProfile("Profile 11", "Ansuman", "a@test"),)


def test_browser_command_preserves_profile_as_single_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        browser, "profiles", lambda: (browser.ChromeProfile("Profile 11", "A", ""),)
    )
    monkeypatch.setattr(browser, "CHROME_APP", tmp_path)

    def run(arguments: tuple[str, ...], **kwargs: object) -> None:
        assert arguments == (
            "/usr/bin/open",
            "-na",
            str(tmp_path),
            "--args",
            "--profile-directory=Profile 11",
            "https://app.devin.ai/auth/cli/token",
        )
        assert kwargs == {"check": True, "timeout": 15}

    monkeypatch.setattr(subprocess, "run", run)
    browser.open_login("Profile 11")


def test_native_failure_does_not_echo_secret_output(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "secret", "secret"),
    )
    with pytest.raises(SwitchError) as error:
        native.capture(native.store.account("ansuman-1"), ("auth", "status"))
    assert "secret" not in str(error.value)


@pytest.mark.skipif(
    not os.environ.get("DS_TEST_NATIVE"), reason="Set DS_TEST_NATIVE for installed CLI"
)
def test_installed_cli_isolation_and_existing_session_visibility(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    monkeypatch.chdir(tmp_path)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    first, second = store.accounts()
    for account in (first, second, first):
        status = native.capture(account, ("auth", "status"))
        assert "Not logged in" in status
        assert str(store.credentials(account)) in status
    assert native.sessions(first) == []
    database = store.root / "shared/cli/sessions.db"
    # Only seed the disposable test database created by this installed CLI.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sessions "
            "(id, working_directory, backend_type, model, agent_mode, "
            "created_at, last_activity_at, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "switch-test-session",
                str(tmp_path),
                "local",
                "test-model",
                "normal",
                1,
                1,
                "continuity marker",
            ),
        )
    for account in (first, second, first):
        sessions = native.sessions(account)
        assert len(sessions) == 1
        assert "switch-test-session" in json.dumps(sessions)
    assert not store.credentials(first).exists()
    assert not store.credentials(second).exists()
