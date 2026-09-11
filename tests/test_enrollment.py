import pytest

from devin_switch import browser, desktop, enrollment
from devin_switch.store import Store, SwitchError, write_json


def test_import_all_profiles_nayanshi_names_and_repeat_without_duplicates(store: Store) -> None:
    profiles = (
        browser.ChromeProfile("Profile 1", "Ansuman", "a@test"),
        browser.ChromeProfile("Profile 15", "Nayanshi", "n@test"),
        browser.ChromeProfile("Profile 20", "School", "nayanshi@school.test"),
        browser.ChromeProfile("Profile 12", "Ansuman", "b@test"),
    )
    write_json(store.directory("ansuman-1") / "account.json", {"chrome_profile": "Profile 1"})
    credentials = store.credentials(store.account("ansuman-1"))
    credentials.write_text("preserve-existing-login")
    store.select(store.account("ansuman-1"))
    assert enrollment.import_profiles(store, profiles) == (3, 0)
    assert store.account("nayanshi-1").chrome_profile == "Profile 15"
    assert store.account("nayanshi-2").chrome_profile == "Profile 20"
    assert store.account("ansuman-3").chrome_profile == "Profile 12"
    assert credentials.read_text() == "preserve-existing-login"
    assert enrollment.import_profiles(store, profiles) == (0, 0)
    assert store.selected().name == "ansuman-1"
    assert not store.credentials(store.account("nayanshi-1")).exists()


def test_renaming_nayanshi_preserves_login_history_and_selection(store: Store) -> None:
    account = store.account("ansuman-2")
    write_json(store.directory(account.name) / "account.json", {"chrome_profile": "Profile 15"})
    store.credentials(account).write_text("preserve-me")
    store.select(account)
    profiles = (browser.ChromeProfile("Profile 15", "Nayanshi", "n@test"),)
    assert enrollment.import_profiles(store, profiles) == (0, 1)
    renamed = store.account("nayanshi-1")
    assert store.credentials(renamed).read_text() == "preserve-me"
    assert store.selected() == renamed
    assert (store.directory(renamed.name) / "data/devin/cli").resolve() == store.root / "shared/cli"
    assert not store.directory("ansuman-2").exists()


def test_rename_rejects_collisions_and_invalid_paths(store: Store) -> None:
    account = store.account("ansuman-1")
    with pytest.raises(SwitchError, match="already exists"):
        store.rename(account, "ansuman-2")
    with pytest.raises(SwitchError, match="lowercase"):
        store.rename(account, "../outside")
    assert store.account("ansuman-1") == account


def test_import_action_respects_active_cli_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "profiles", lambda: ())
    with store.lock(), pytest.raises(SwitchError, match="Another ds"):
        desktop.action(store, {"action": "import"})
    assert "Added 0" in desktop.action(store, {"action": "import"})["message"]
