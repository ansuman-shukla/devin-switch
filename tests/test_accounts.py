import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from devin_switch import cli, handoff
from devin_switch import native as native_module
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


def test_new_accounts_disable_commit_attribution_privately(store: Store, native: Native) -> None:
    account = store.add("fresh", None)
    config = store.directory(account.name) / "config/devin/config.json"
    assert json.loads(config.read_text()) == {"attribution": False}
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert native.environment(account)["XDG_CONFIG_HOME"] == str(config.parent.parent)
    with store.lock():
        handoff.enable(store, account)
    assert json.loads(handoff.json_source(config.read_text()))["attribution"] is False
    config.write_text('{"attribution": true}')
    store.prepare(account)
    assert json.loads(config.read_text()) == {"attribution": True}


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


def unshare_mcp(store: Store, account) -> tuple[Path, Path]:
    tokens = store.directory(account.name) / "data/devin/mcp"
    config = store.directory(account.name) / "config/devin/mcp_config.json"
    tokens.unlink()
    config.unlink()
    (tokens / "oauth").mkdir(parents=True)
    return tokens, config


def write_dated(path: Path, content: str, mtime: int) -> None:
    path.write_text(content)
    os.utime(path, (mtime, mtime))


def test_mcp_servers_and_tokens_are_shared_but_logins_are_not(native: Native) -> None:
    store = native.store
    first, second = store.accounts()
    first_env, second_env = native.environment(first), native.environment(second)
    first_token = Path(first_env["XDG_DATA_HOME"]) / "devin/mcp/oauth/server.json"
    first_token.parent.mkdir(parents=True)
    first_token.write_text("token-from-first-login")
    second_token = Path(second_env["XDG_DATA_HOME"]) / "devin/mcp/oauth/server.json"
    assert second_token.read_text() == "token-from-first-login"
    first_config = Path(first_env["XDG_CONFIG_HOME"]) / "devin/mcp_config.json"
    second_config = Path(second_env["XDG_CONFIG_HOME"]) / "devin/mcp_config.json"
    assert json.loads(second_config.read_text()) == {"mcpServers": {}}
    first_config.write_text('{"mcpServers": {"github": {"url": "https://example.invalid"}}}')
    assert "github" in json.loads(second_config.read_text())["mcpServers"]
    assert store.credentials(first) != store.credentials(second)
    assert first_env["XDG_CONFIG_HOME"] != second_env["XDG_CONFIG_HOME"]
    assert stat.S_IMODE((store.root / "shared/mcp").stat().st_mode) == 0o700
    assert stat.S_IMODE(second_config.resolve().stat().st_mode) == 0o600
    store.remove(first)
    assert second_token.read_text() == "token-from-first-login"
    assert "github" in json.loads(second_config.read_text())["mcpServers"]


def test_existing_mcp_tokens_migrate_with_newest_copy_and_no_deletion(store: Store) -> None:
    first, second = store.accounts()
    first_tokens, _ = unshare_mcp(store, first)
    second_tokens, _ = unshare_mcp(store, second)
    write_dated(first_tokens / "oauth/logfire.json", "first-older", 100)
    write_dated(first_tokens / "oauth/sentry.json", "first-only", 100)
    write_dated(second_tokens / "oauth/logfire.json", "second-newer", 200)
    store.prepare(second)
    shared = store.root / "shared/mcp/oauth"
    assert (shared / "logfire.json").read_text() == "second-newer"
    assert (shared / "sentry.json").read_text() == "first-only"
    for account in (first, second):
        tokens = store.directory(account.name) / "data/devin/mcp"
        assert tokens.is_symlink() and tokens.resolve() == shared.parent.resolve()
    assert not list(first_tokens.parent.glob("mcp.superseded-*"))
    (retired,) = second_tokens.parent.glob("mcp.superseded-*")
    assert [path.name for path in retired.rglob("*") if path.is_file()] == ["logfire.json"]
    assert (retired / "oauth/logfire.json").read_text() == "first-older"
    store.prepare(first)
    assert len(list(second_tokens.parent.glob("mcp.superseded-*"))) == 1


def test_existing_mcp_configs_merge_and_keep_comments(store: Store) -> None:
    first, second = store.accounts()
    _, first_config = unshare_mcp(store, first)
    _, second_config = unshare_mcp(store, second)
    original = (
        '{\n  // keep this note\n  "mcpServers": {\n'
        '    "chrome": {"command": "old"},\n    "linear": {"url": "https://l.invalid"},\n  },\n}\n'
    )
    write_dated(first_config, original, 100)
    write_dated(
        second_config,
        '{"mcpServers": {"chrome": {"command": "new"}, "github": {"url": "https://g.invalid"}}}',
        200,
    )
    store.prepare(second)
    shared = store.root / "shared/config/mcp_config.json"
    assert json.loads(shared.read_text())["mcpServers"] == {
        "chrome": {"command": "new"},
        "linear": {"url": "https://l.invalid"},
        "github": {"url": "https://g.invalid"},
    }
    (retired,) = second_config.parent.glob("mcp_config.json.superseded-*")
    assert "github" in retired.read_text()
    assert not list(first_config.parent.glob("mcp_config.json.superseded-*"))
    for config in (first_config, second_config):
        assert config.is_symlink() and config.resolve() == shared.resolve()


def test_single_existing_mcp_config_moves_with_comments(store: Store) -> None:
    first = store.accounts()[0]
    _, config = unshare_mcp(store, first)
    original = '{\n  // keep this note\n  "mcpServers": {"chrome": {"command": "x"},},\n}\n'
    config.write_text(original)
    store.prepare(store.accounts()[1])
    assert (store.root / "shared/config/mcp_config.json").read_text() == original
    assert config.is_symlink()


def test_replaced_mcp_config_link_is_merged_back(store: Store) -> None:
    first, second = store.accounts()
    config = store.directory(first.name) / "config/devin/mcp_config.json"
    config.unlink()
    config.write_text('{"mcpServers": {"written-directly": {"command": "x"}}}')
    store.prepare(first)
    assert config.is_symlink()
    other = store.directory(second.name) / "config/devin/mcp_config.json"
    assert "written-directly" in json.loads(other.read_text())["mcpServers"]


def test_unreadable_mcp_config_is_preserved_and_reported(store: Store) -> None:
    first = store.accounts()[0]
    store.root.joinpath("shared/config/mcp_config.json").write_text('{"mcpServers": {}}')
    _, config = unshare_mcp(store, first)
    config.write_text("{not json")
    with pytest.raises(SwitchError, match="Cannot share MCP servers"):
        store.prepare(first)
    store.prepare(store.accounts()[1])
    assert config.read_text() == "{not json"
    assert not config.is_symlink()


def test_foreign_mcp_link_is_rejected(store: Store, tmp_path: Path) -> None:
    first = store.accounts()[0]
    tokens = store.directory(first.name) / "data/devin/mcp"
    tokens.unlink()
    (tmp_path / "elsewhere").mkdir()
    tokens.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(SwitchError, match="Unexpected shared MCP link"):
        store.prepare(first)


def test_zero_exit_status_is_not_proof_of_authentication(native: Native) -> None:
    first = native.store.account("ansuman-1")
    native.store.credentials(first).write_text("expired-or-invalid")
    assert native.capture(first, ("auth", "status")) == "Not logged in.\n"
    assert native.authenticated(first) is False


def test_confirmed_login_is_reused_until_credentials_change(native: Native, monkeypatch) -> None:
    first = native.store.account("ansuman-1")
    credentials = native.store.credentials(first)
    credentials.write_text("valid-first")
    checks = []
    capture = Native.capture
    monkeypatch.setattr(
        Native,
        "capture",
        lambda self, *args, **kw: checks.append(args) or capture(self, *args, **kw),
    )
    assert native.authenticated(first) and native.authenticated(first)
    assert len(checks) == 1
    credentials.write_text("expired-first")
    assert native.authenticated(first) is False
    assert native.authenticated(first) is False
    assert len(checks) == 3
    credentials.write_text("valid-again")
    monkeypatch.setattr(native_module, "LOGIN_CHECK_TTL", 0)
    assert native.authenticated(first) and native.authenticated(first)
    assert len(checks) == 5


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
