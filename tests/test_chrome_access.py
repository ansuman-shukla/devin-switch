import json
import shutil
import subprocess
from pathlib import Path

import pytest

from devin_switch import browser, chrome_access, cli
from devin_switch.store import SwitchError


@pytest.fixture
def chrome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "Chrome"
    (data / "Profile 1/Local Storage/leveldb").mkdir(parents=True)
    (data / "Profile 1/Cookies").write_text("cookies")
    (data / "Profile 1/Local Storage/leveldb/000003.log").write_text("storage")
    (data / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
    info = {"Profile 1": {"name": "Work", "user_name": "work@example.invalid"}}
    (data / "Local State").write_text(json.dumps({"profile": {"info_cache": info}}))
    monkeypatch.setattr(browser, "CHROME_DATA", data)
    monkeypatch.setattr(chrome_access, "HELPER_DATA", data)
    return data


@pytest.fixture
def blocked(chrome: Path, chrome_helper, protect):
    chrome_helper.protection = protect(chrome)
    return chrome_helper


def test_readable_data_is_copied_directly_without_a_popup(
    chrome: Path, chrome_helper, tmp_path: Path
) -> None:
    destination = tmp_path / "copy"
    (destination / "Local Storage/stale").mkdir(parents=True)
    (destination / "Cookies").write_text("old")
    (destination / "Missing").write_text("kept")
    items = ["Profile 1/Cookies", "Profile 1/Local Storage", "Profile 1/Missing"]
    chrome_access.copy(chrome, items, destination, "test")
    assert (destination / "Cookies").read_text() == "cookies"
    assert (destination / "Local Storage/leveldb/000003.log").read_text() == "storage"
    assert not (destination / "Local Storage/stale").exists()
    assert (destination / "Missing").read_text() == "kept"
    shutil.rmtree(destination / "Local Storage")
    (destination / "Local Storage").write_text("a file where Chrome has a folder")
    chrome_access.copy(chrome, ["Profile 1/Local Storage"], destination, "test")
    assert (destination / "Local Storage/leveldb/000003.log").read_text() == "storage"
    assert chrome_helper.requests == []


@pytest.mark.parametrize("item", ["/etc/passwd", "../Messages", "Profile 1/../x", "a//b", ".", ""])
def test_items_must_stay_inside_chromes_folder(
    blocked, chrome: Path, tmp_path: Path, item: str
) -> None:
    with pytest.raises(SwitchError, match="inside Chrome's data folder"):
        chrome_access.copy(chrome, [item], tmp_path, "test")
    with pytest.raises(SwitchError, match="at least one"):
        chrome_access.copy(chrome, [], tmp_path, "test")
    assert blocked.requests == []


def test_blocked_data_is_copied_by_the_helper_after_allow(
    blocked, chrome: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    destination = tmp_path / "copy"
    destination.mkdir()
    items = ["Profile 1/Cookies", "Profile 1/Local Storage"]
    chrome_access.copy(chrome, items, destination, "Copy the logins")
    assert blocked.requests == [
        {
            "items": items,
            "destination": str(destination.resolve()),
            "reason": "Copy the logins",
            "requester": str(project.resolve()),
        }
    ]
    assert (destination / "Cookies").read_text() == "cookies"
    assert (destination / "Local Storage/leveldb/000003.log").read_text() == "storage"
    assert chrome_access.read_text(chrome, "DevToolsActivePort", "connect").split() == [
        "9222",
        "/devtools/browser/abc",
    ]
    assert blocked.requests[-1]["items"] == ["DevToolsActivePort"]
    assert blocked.requests[-1]["reason"] == "connect"


@pytest.mark.parametrize(
    ("decision", "error", "message"),
    [
        ("deny", chrome_access.ChromeBlocked, "denied in the popup"),
        ("timeout", chrome_access.ChromeBlocked, "within 2 minutes"),
        ("error", SwitchError, "helper failed: broken request"),
        ("silent", SwitchError, "helper failed: it gave no answer"),
    ],
)
def test_anything_but_allow_copies_nothing(
    blocked, chrome: Path, tmp_path: Path, decision: str, error: type, message: str
) -> None:
    blocked.decision = decision
    with pytest.raises(error, match=message):
        chrome_access.copy(chrome, ["Profile 1/Cookies"], tmp_path, "test")
    assert not (tmp_path / "Cookies").exists()
    assert len(blocked.requests) == 1


def test_without_the_helper_blocked_data_explains_the_one_time_setup(
    chrome: Path, chrome_helper, protect, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protect(chrome)
    monkeypatch.setenv("DS_CHROME_ACCESS_APP", str(tmp_path / "missing.app"))
    with pytest.raises(chrome_access.ChromeBlocked, match="make chrome-access.*Full Disk Access"):
        chrome_access.copy(chrome, ["Profile 1/Cookies"], tmp_path, "test")
    assert chrome_helper.requests == []


def test_the_popup_only_covers_chromes_own_folder(
    blocked, chrome: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome_access, "HELPER_DATA", tmp_path / "elsewhere")
    with pytest.raises(chrome_access.ChromeBlocked, match="popup only covers"):
        chrome_access.copy(chrome, ["Profile 1/Cookies"], tmp_path, "test")
    assert blocked.requests == []


def test_helper_finds_installed_app_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DS_CHROME_ACCESS_APP")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert chrome_access.helper() is None
    installed = tmp_path / "Applications" / chrome_access.APP_NAME
    installed.mkdir(parents=True)
    assert chrome_access.helper() == installed


def test_launch_starts_the_helper_through_launch_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    chrome_access.launch(tmp_path / "Helper.app", tmp_path / "in.json", tmp_path / "out.json")
    assert calls == [
        (
            (
                (
                    "/usr/bin/open",
                    "-W",
                    "-n",
                    str(tmp_path / "Helper.app"),
                    "--args",
                    "--request",
                    str(tmp_path / "in.json"),
                    "--result",
                    str(tmp_path / "out.json"),
                    "--timeout",
                    "120",
                ),
            ),
            {"check": False, "capture_output": True, "timeout": 180},
        )
    ]


def test_profile_index_asks_only_when_a_reason_is_given(blocked) -> None:
    with pytest.raises(browser.ChromeBlocked, match="Full Disk Access"):
        browser.profiles()
    assert blocked.requests == []
    expected = (browser.ChromeProfile("Profile 1", "Work", "work@example.invalid"),)
    assert browser.profiles(reason="List profiles") == expected
    assert [request["items"] for request in blocked.requests] == [["Local State"]]


def test_has_profile_sees_folders_when_blocked_without_a_popup(blocked, chrome: Path) -> None:
    assert browser.has_profile("Profile 1")
    for name in ("Profile 2", "..", ".", "", "Profile 1/Local Storage"):
        assert not browser.has_profile(name)
    assert blocked.requests == []


def test_has_profile_uses_the_index_when_readable(chrome: Path) -> None:
    (chrome / "Stray").mkdir()
    assert browser.has_profile("Profile 1") and not browser.has_profile("Stray")


def test_chrome_copy_command_reports_each_item(
    blocked, tmp_path: Path, capsys: pytest.CaptureFixture[str], store
) -> None:
    destination = tmp_path / "new/folder"
    arguments = ["chrome", "copy", "Local State", "Nope", "--to", str(destination)]
    assert cli.execute(cli.parser().parse_args([*arguments, "--reason", "Read it"]), store) == 0
    assert (destination / "Local State").exists()
    output = capsys.readouterr().out
    assert f"Local State: copied to {destination / 'Local State'}" in output
    assert "Nope: not found" in output
    assert blocked.requests[0]["reason"] == "Read it"


def test_profiles_command_asks_with_a_popup(
    blocked, capsys: pytest.CaptureFixture[str], store
) -> None:
    assert cli.execute(cli.parser().parse_args(["profiles"]), store) == 0
    assert "Profile 1" in capsys.readouterr().out
    assert "ds profiles" in blocked.requests[0]["reason"]
