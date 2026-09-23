import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from devin_switch import cli
from devin_switch.native import AUTH_ENVIRONMENT, Native
from devin_switch.store import Store, SwitchError, validate_name


@pytest.mark.parametrize("name", ["../escape", "", "A", "a/b", "a b", "a" * 49, "-flag"])
def test_alias_cannot_escape_storage(name: str) -> None:
    with pytest.raises(SwitchError):
        validate_name(name)


def test_credentials_separate_but_conversations_shared(store: Store) -> None:
    first, second = store.accounts()
    assert store.credentials(first) != store.credentials(second)
    first_history = store.directory(first.name) / "data/devin/cli"
    second_history = store.directory(second.name) / "data/devin/cli"
    (first_history / "marker").write_text("existing conversation")
    assert (second_history / "marker").read_text() == "existing conversation"
    assert first_history.resolve() == second_history.resolve()
    assert not store.credentials(first).is_symlink()


def test_accounts_and_selection_are_private_and_duplicate_add_preserves_login(store: Store) -> None:
    first = store.account("ansuman-1")
    store.credentials(first).write_text("keep this login")
    store.select(first)
    with pytest.raises(SwitchError, match="already exists"):
        store.add(first.name, "Profile 5")
    assert store.credentials(first).read_text() == "keep this login"
    assert store.selected() == first
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.directory(first.name).stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / "selected").stat().st_mode) == 0o600


def test_display_name_preserves_account_identity_and_active_sessions(store: Store) -> None:
    account = store.account("ansuman-1")
    store.select(account)
    store.credentials(account).write_text("test-login")
    history = store.directory(account.name) / "data/devin/cli"
    metadata = store.directory(account.name) / "account.json"
    original_metadata = metadata.read_bytes()
    with store.account_lock(account, shared=True), store.lock():
        renamed = store.set_display_name(account, "  Work Account  ")
    assert renamed.name == account.name
    assert renamed.display_name == "Work Account"
    assert store.selected() == renamed
    assert store.credentials(renamed).read_text() == "test-login"
    assert history.is_symlink()
    assert Store(store.root).account(account.name).display_name == "Work Account"
    assert metadata.read_bytes() == original_metadata
    assert set(json.loads(metadata.read_text())) == {"chrome_profile"}
    label_file = store.directory(account.name) / "display-name.json"
    assert stat.S_IMODE(label_file.stat().st_mode) == 0o600
    with store.lock():
        reset = store.set_display_name(renamed, "  ")
    assert reset.display_name is None
    assert json.loads((store.directory(account.name) / "account.json").read_text()) == {
        "chrome_profile": account.chrome_profile
    }


@pytest.mark.parametrize("label", ["a" * 81, "first\nsecond", "bad\x00label", "bad\x1blabel"])
def test_invalid_display_names_preserve_metadata(store: Store, label: str) -> None:
    account = store.account("ansuman-1")
    before = (store.directory(account.name) / "account.json").read_bytes()
    with store.lock(), pytest.raises(SwitchError, match="display name"):
        store.set_display_name(account, label)
    assert (store.directory(account.name) / "account.json").read_bytes() == before
    assert not (store.directory(account.name) / "display-name.json").exists()


def test_display_name_supports_unicode_and_survives_alias_rename(store: Store) -> None:
    account = store.add("custom", "Profile 5")
    with store.lock():
        renamed = store.set_display_name(account, "Équipe personnelle")
        moved = store.rename(renamed, "new-alias")
    assert moved.display_name == "Équipe personnelle"
    assert moved.chrome_profile == "Profile 5"


def test_other_commands_cannot_switch_during_running_session(store: Store) -> None:
    with store.lock(), pytest.raises(SwitchError, match="Another ds command"):
        with store.lock():
            pytest.fail("A second process must not be able to acquire the profile lock")


def test_existing_history_is_never_overwritten(store: Store) -> None:
    first = store.account("ansuman-1")
    history = store.directory(first.name) / "data/devin/cli"
    history.unlink()
    history.mkdir()
    (history / "marker").write_text("precious")
    with pytest.raises(SwitchError, match="Refusing to replace"):
        store.prepare(first)
    assert (history / "marker").read_text() == "precious"


def test_environment_cannot_select_another_accounts_credentials(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in AUTH_ENVIRONMENT:
        monkeypatch.setenv(name, "inherited-secret")
    monkeypatch.setenv("DEVIN_MODEL", "test-model")
    before_home = os.environ["HOME"]
    first, second = native.store.accounts()
    first_env, second_env = native.environment(first), native.environment(second)
    assert AUTH_ENVIRONMENT.isdisjoint(first_env)
    assert AUTH_ENVIRONMENT.isdisjoint(second_env)
    assert first_env["XDG_DATA_HOME"] != second_env["XDG_DATA_HOME"]
    assert first_env["XDG_CONFIG_HOME"] != second_env["XDG_CONFIG_HOME"]
    assert first_env["HOME"] == second_env["HOME"] == before_home
    assert first_env["DEVIN_MODEL"] == "test-model"


@pytest.mark.parametrize(
    ("gh_config", "xdg_config", "expected"),
    [
        (None, None, "home/.config/gh"),
        ("", "", "home/.config/gh"),
        (None, "custom-config", "custom-config/gh"),
        ("custom-gh", "custom-config", "custom-gh"),
        (None, "state/accounts/ansuman-1/config", "home/.config/gh"),
        (None, "old-state/accounts/ansuman-1/config", "home/.config/gh"),
        ("custom-gh", "state/accounts/ansuman-1/config", "custom-gh"),
    ],
)
def test_github_config_is_user_level_before_account_isolation(
    native: Native, tmp_path: Path, monkeypatch, gh_config, xdg_config, expected
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DS_HOME", str(tmp_path / "old-state"))
    for key, value in (("GH_CONFIG_DIR", gh_config), ("XDG_CONFIG_HOME", xdg_config)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(tmp_path / value) if value else "")
    before = dict(os.environ)
    first, second = native.store.accounts()
    first_env, second_env = native.environment(first), native.environment(second)
    assert first_env["GH_CONFIG_DIR"] == second_env["GH_CONFIG_DIR"] == str(tmp_path / expected)
    assert first_env["XDG_CONFIG_HOME"] != second_env["XDG_CONFIG_HOME"]
    assert dict(os.environ) == before
    assert not (tmp_path / expected).exists()
    assert not (native.store.directory(first.name) / "config/gh").exists()
    assert not (native.store.directory(second.name) / "config/gh").exists()


def test_nested_launch_keeps_original_github_config(native: Native, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    first, second = native.store.accounts()
    environment = native.environment(first)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert native.environment(second)["GH_CONFIG_DIR"] == str(tmp_path / "user-config/gh")


@pytest.mark.skipif(not shutil.which("gh"), reason="GitHub CLI is not installed")
def test_real_github_cli_reuses_config_across_accounts(native: Native, tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    subprocess.run(("gh", "config", "set", "editor", "switch-test-editor"), check=True)
    first, second = native.store.accounts()
    for account in (first, second, first):
        result = subprocess.run(
            ("gh", "config", "get", "editor"),
            env=native.environment(account),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "switch-test-editor"
        assert not (native.store.directory(account.name) / "config/gh").exists()


def test_zero_exit_status_is_not_proof_of_authentication(native: Native) -> None:
    first = native.store.account("ansuman-1")
    native.store.credentials(first).write_text("expired-or-invalid")
    assert native.capture(first, ("auth", "status")) == "Not logged in.\n"
    assert native.authenticated(first) is False


def test_linked_credentials_rejected(native: Native, tmp_path: Path) -> None:
    first = native.store.account("ansuman-1")
    shared = tmp_path / "shared-token"
    shared.write_text("valid-other-account")
    native.store.credentials(first).symlink_to(shared)
    with pytest.raises(SwitchError, match="separate regular file"):
        native.authenticated(first)


def test_failed_switch_preserves_previous_account(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = native.store.account("ansuman-1")
    native.store.select(first)
    monkeypatch.setattr(cli, "find_binary", lambda: native.binary)
    with pytest.raises(SwitchError, match="No saved login"):
        cli.execute(cli.parser().parse_args(["use", "ansuman-2"]), native.store)
    assert native.store.selected() == first


def test_next_skips_unsigned_accounts_and_does_not_reselect_current(signed_in: Native) -> None:
    store = signed_in.store
    first, second = store.accounts()
    store.select(first)
    assert cli.choose_next(store, signed_in) == second
    store.credentials(second).unlink()
    with pytest.raises(SwitchError, match="No other account"):
        cli.choose_next(store, signed_in)
    assert store.selected() == first


def test_verify_checks_a_b_a_and_shared_nonempty_history(
    signed_in: Native, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = signed_in.store.accounts()
    signed_in.store.select(second)
    history = signed_in.store.root / "shared/cli/test-sessions.json"
    history.write_text(json.dumps([{"id": "session-123", "title": "work in progress"}]))
    cli.verify(signed_in, first, second)
    output = capsys.readouterr().out
    assert output.index("ansuman-1") < output.index("ansuman-2") < output.rindex("ansuman-1")
    assert "No model request was made" in output
    assert "live prompt test" in output
    assert signed_in.store.selected() == second
    assert "valid-" not in output
    assert stat.S_IMODE(signed_in.store.credentials(first).stat().st_mode) == 0o600


def test_verify_rejects_same_credentials(signed_in: Native) -> None:
    first, second = signed_in.store.accounts()
    signed_in.store.credentials(second).write_bytes(signed_in.store.credentials(first).read_bytes())
    with pytest.raises(SwitchError, match="identical credentials"):
        cli.verify(signed_in, first, second)


def test_verify_catches_missing_history_in_second_account(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = signed_in.store.accounts()
    monkeypatch.setattr(
        Native, "sessions", lambda self, account: [{"id": "existing"}] if account == first else []
    )
    with pytest.raises(SwitchError, match="visibility differs"):
        cli.verify(signed_in, first, second)
