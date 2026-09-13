import io
import json
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from devin_switch import browser, desktop
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError


def test_snapshot_shows_accounts_while_cli_is_busy_without_exposing_tokens(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = signed_in.store
    store.select(store.account("ansuman-2"))
    monkeypatch.setattr(browser, "profiles", lambda: ())
    with store.lock():
        state = desktop.snapshot(store)
        assert state["busy"] is True
        assert state["selected"] == "ansuman-2"
        assert tuple({k: v for k, v in a.items() if k != "usage"} for a in state["accounts"]) == (
            {
                "name": "ansuman-1",
                "display_name": None,
                "chrome_profile": None,
                "saved_login": True,
            },
            {
                "name": "ansuman-2",
                "display_name": None,
                "chrome_profile": None,
                "saved_login": True,
            },
        )
    assert desktop.is_busy(store) is False
    assert "valid-" not in json.dumps(state)


def test_add_validates_chrome_profile_and_keeps_existing_account(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        browser, "profiles", lambda: (browser.ChromeProfile("Profile 12", "Ansuman", "a@test"),)
    )
    result = desktop.action(
        store, {"action": "add", "account": "ansuman-3", "chrome_profile": "Profile 12"}
    )
    assert "Added ansuman-3" in result["message"]
    assert store.account("ansuman-3").chrome_profile == "Profile 12"
    with pytest.raises(SwitchError, match="existing Chrome profile"):
        desktop.action(
            store, {"action": "add", "account": "ansuman-4", "chrome_profile": "Missing"}
        )
    assert len(store.accounts()) == 3


def test_display_rename_needs_no_cli_and_returns_fresh_state(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "profiles", lambda: ())
    monkeypatch.setattr(desktop, "find_binary", lambda: pytest.fail("Rename must work offline"))
    account = store.account("ansuman-1")
    store.select(account)
    with store.account_lock(account, shared=True):
        result = desktop.action(
            store, {"action": "rename_display", "account": account.name, "display_name": "Personal"}
        )
    assert result["state"]["selected"] == account.name
    assert result["state"]["busy"] is False
    assert result["state"]["accounts"][0]["display_name"] == "Personal"
    assert "Personal" in result["message"]
    reset = desktop.action(
        store, {"action": "rename_display", "account": account.name, "display_name": ""}
    )
    assert reset["state"]["accounts"][0]["display_name"] is None


@pytest.mark.parametrize("value", [None, 42, [], {}, "line\nbreak", "x" * 81])
def test_display_rename_rejects_invalid_input(
    store: Store, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "profiles", lambda: ())
    with pytest.raises(SwitchError, match="[Dd]isplay name"):
        desktop.action(
            store, {"action": "rename_display", "account": "ansuman-1", "display_name": value}
        )
    assert store.account("ansuman-1").display_name is None


def test_display_rename_respects_store_lock(store: Store) -> None:
    with store.lock(), pytest.raises(SwitchError, match="Another ds command"):
        desktop.action(
            store, {"action": "rename_display", "account": "ansuman-1", "display_name": "Personal"}
        )
    assert store.account("ansuman-1").display_name is None


def test_select_next_and_failed_select_share_cli_rules(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = signed_in.store
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    desktop.action(store, {"action": "select", "account": "ansuman-1"})
    assert store.selected().name == "ansuman-1"
    result = desktop.action(store, {"action": "next"})
    assert result["focus"] == "ansuman-2"
    assert store.selected().name == "ansuman-2"
    store.credentials(store.account("ansuman-1")).unlink()
    with pytest.raises(SwitchError, match="No saved login"):
        desktop.action(store, {"action": "select", "account": "ansuman-1"})
    assert store.selected().name == "ansuman-2"


def test_resume_missing_session_does_not_create_launcher(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    with pytest.raises(SwitchError, match="Choose Start new"):
        desktop.action(
            signed_in.store,
            {"action": "resume", "account": "ansuman-1", "project": str(tmp_path)},
        )
    assert not (signed_in.store.root / "launchers").exists()


def test_resume_latest_resolves_exact_session_in_chosen_folder(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    monkeypatch.setattr(
        desktop.sessions,
        "history",
        lambda _: [
            {"id": "other-project", "project": str(tmp_path / "other")},
            {"id": "existing-session", "project": str(tmp_path)},
        ],
    )
    result = desktop.action(
        signed_in.store,
        {"action": "resume", "account": "ansuman-2", "project": str(tmp_path)},
    )
    launcher = Path(result["launcher"])
    command = shlex.split(launcher.read_text().splitlines()[2])
    assert command[-6:] == ["run", "--account", "ansuman-2", "--", "--resume", "existing-session"]
    assert "valid-" not in launcher.read_text()


def test_launcher_uses_quoted_paths_and_private_file_without_opening_terminal(
    native: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(desktop, "find_binary", lambda: native.binary)
    project = tmp_path / "quotes ' and $(touch injected)"
    project.mkdir()
    fake_python = tmp_path / "python with spaces"
    fake_python.write_text('#!/bin/sh\nprintf \'%s\\n\' "$PWD" "$DS_HOME" "$DS_BINARY" "$@"\n')
    fake_python.chmod(0o700)
    monkeypatch.setattr(desktop.sys, "executable", str(fake_python))
    launcher = desktop.terminal_launcher(native.store, ("run", "--account", "ansuman-1"), project)
    result = subprocess.run((str(launcher),), capture_output=True, text=True, check=True)
    assert str(project) in result.stdout
    assert str(native.store.root) in result.stdout
    assert str(native.binary) in result.stdout
    assert "run\n--account\nansuman-1" in result.stdout
    assert not (project / "injected").exists()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o700


def test_login_launcher_only_when_account_needs_login(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    request = {"action": "login", "account": "ansuman-1"}
    assert "launcher" not in desktop.action(signed_in.store, request)
    signed_in.store.credentials(signed_in.store.account("ansuman-1")).unlink()
    result = desktop.action(signed_in.store, request)
    assert " login ansuman-1" in Path(result["launcher"]).read_text()


def test_invalid_project_fails_without_changing_selection(
    signed_in: Native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(desktop, "find_binary", lambda: signed_in.binary)
    signed_in.store.select(signed_in.store.account("ansuman-2"))
    with pytest.raises(SwitchError, match="existing project folder"):
        desktop.action(
            signed_in.store,
            {"action": "start", "account": "ansuman-1", "project": str(tmp_path / "missing")},
        )
    assert signed_in.store.selected().name == "ansuman-2"


def test_bridge_returns_clean_json_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(desktop.sys, "stdin", io.StringIO('{"action": "add"}'))
    assert desktop.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": False, "message": "Account is required."}
