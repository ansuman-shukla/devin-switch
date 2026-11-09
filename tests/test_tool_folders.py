import os
import subprocess
import sys
from pathlib import Path

import pytest

from devin_switch import tool_folders
from devin_switch.native import Native
from devin_switch.store import Store
from devin_switch.tool_folders import Adopted

DAY = tool_folders.INTERVAL


def shared_tool(store: Store, area: str, name: str) -> Path:
    folder = store.tools(area) / name
    folder.mkdir(parents=True)
    return folder


def test_new_login_links_every_shared_tool_folder_and_keeps_devin_private(store: Store) -> None:
    for area, name in (("cache", "uv"), ("cache", "pnpm"), ("data", "uv")):
        (shared_tool(store, area, name) / "kept-once").write_text(f"{area}/{name}")
    # Devin's own folder is never shared, even if something put one in the shared place.
    for area in ("cache", "data"):
        (shared_tool(store, area, "devin") / "foreign").write_text("not this login's")
    (store.root / "shared/tools/adopting").mkdir()

    base = store.directory(store.add("fresh", None).name)

    for area, name in (("cache", "uv"), ("cache", "pnpm"), ("data", "uv")):
        link = base / area / name
        assert link.is_symlink()
        assert Path(os.readlink(link)) == (store.tools(area) / name).resolve()
        assert (link / "kept-once").read_text() == f"{area}/{name}"
    assert sorted(path.name for path in (base / "cache").iterdir()) == ["pnpm", "uv"]
    assert sorted(path.name for path in (base / "data").iterdir()) == ["devin", "uv"]
    assert (base / "data/devin").is_dir() and not (base / "data/devin").is_symlink()
    assert not (base / "data/devin/foreign").exists()
    # What one login's tool writes is what every other login's tool reads.
    (base / "cache/uv/wheel").write_text("built once")
    other = store.directory("ansuman-1")
    store.prepare(store.account("ansuman-1"))
    assert (other / "cache/uv/wheel").read_text() == "built once"


def test_prepare_never_replaces_what_a_login_already_has(store: Store, tmp_path: Path) -> None:
    account = store.account("ansuman-1")
    base = store.directory(account.name)
    for name in ("uv", "pip", "gh", "pnpm"):
        (shared_tool(store, "cache", name) / "shared").write_text(name)
    (base / "cache/uv").mkdir()
    (base / "cache/uv/own").write_text("still being adopted")
    (base / "cache/pip").write_text("a plain file")
    elsewhere = tmp_path / "elsewhere"
    (base / "cache/gh").symlink_to(elsewhere)

    store.prepare(account)
    first = os.readlink(base / "cache/pnpm")
    store.prepare(account)

    assert not (base / "cache/uv").is_symlink()
    assert [path.name for path in (base / "cache/uv").iterdir()] == ["own"]
    assert (base / "cache/pip").read_text() == "a plain file"
    assert os.readlink(base / "cache/gh") == str(elsewhere) and not elsewhere.exists()
    assert os.readlink(base / "cache/pnpm") == first
    assert (store.tools("cache") / "uv/shared").read_text() == "uv"


def test_prepare_never_links_through_a_linked_cache_folder(store: Store, tmp_path: Path) -> None:
    account = store.account("ansuman-1")
    base = store.directory(account.name)
    shared_tool(store, "cache", "uv")
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "cache").rmdir()
    (base / "cache").symlink_to(outside)

    store.prepare(account)

    assert list(outside.iterdir()) == []


def test_running_logins_are_read_from_commands_and_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A state folder whose path would mean something else as a regular expression.
    root = tmp_path / "my (state)+ folder"
    listing = (
        f"node server.js HOME=/Users/me XDG_CACHE_HOME={root}/accounts/ansuman-7/cache\n"
        f"python worker.py DS_LOGIN={root}/accounts/ansuman-8\n"
        f"cat {tmp_path}/other/accounts/ansuman-9/cache/file\n"
    )
    monkeypatch.setattr(tool_folders, "PROCESSES", ("/bin/echo", listing))

    assert tool_folders.running_logins(Store(root)) == {"ansuman-7", "ansuman-8"}


def test_launch_survives_broken_or_racing_tool_folders(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = store.account("ansuman-1")
    base = store.directory(account.name)
    store.tools("cache").parent.mkdir(parents=True)
    store.tools("cache").write_text("not a folder")
    shared_tool(store, "data", "uv")

    store.prepare(account)

    assert list((base / "cache").iterdir()) == []
    assert (base / "data/uv").is_symlink()

    # A tool creates one name between the listing and the link: the others still link.
    for name in ("deno", "pip", "pnpm"):
        shared_tool(store, "data", name)
    link = Path.symlink_to

    def racing(self, target, target_is_directory=False):
        if self.name == "deno":
            raise FileExistsError(str(self))
        link(self, target, target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", racing)
    store.prepare(account)
    monkeypatch.undo()
    assert not os.path.lexists(base / "data/deno")
    assert (base / "data/pip").is_symlink() and (base / "data/pnpm").is_symlink()
    store.prepare(account)
    assert (base / "data/deno").is_symlink()


def test_native_environment_keeps_per_login_folders(native: Native) -> None:
    store = native.store
    shared_tool(store, "cache", "uv")
    shared_tool(store, "data", "uv")
    for account in store.accounts():
        base = store.directory(account.name)
        environment = native.environment(account)
        assert {key: value for key, value in environment.items() if key.startswith("XDG_")} == {
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "XDG_STATE_HOME": str(base / "state"),
        }
        # Launching is what links a login to the shared tool folders.
        assert (base / "cache/uv").resolve() == (store.tools("cache") / "uv").resolve()
        assert (base / "data/uv").resolve() == (store.tools("data") / "uv").resolve()
        assert not (base / "data").is_symlink() and not (base / "cache").is_symlink()


def test_removing_a_login_keeps_shared_tool_folders(store: Store) -> None:
    account = store.account("ansuman-1")
    for area in ("cache", "data"):
        (shared_tool(store, area, "uv") / "wheel").write_text(area)
    store.prepare(account)
    assert (store.directory(account.name) / "cache/uv/wheel").read_text() == "cache"

    store.remove(account)

    assert not store.directory(account.name).exists()
    assert (store.tools("cache") / "uv/wheel").read_text() == "cache"
    assert (store.tools("data") / "uv/wheel").read_text() == "data"


def own_tool(store: Store, login: str, area: str, name: str, files: dict[str, str]) -> Path:
    """A real tool folder a login filled before its folders were shared."""
    folder = store.directory(login) / area / name
    for relative, text in files.items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(text)
    return folder


def staged(store: Store, area: str, name: str) -> list[Path]:
    return tool_folders.entries(store.tools("adopting") / area / name)


def age(path: Path, seconds: int) -> None:
    os.utime(path, (seconds, seconds), follow_symlinks=False)


def test_first_logins_tool_folders_become_the_shared_ones(store: Store) -> None:
    cache = own_tool(store, "ansuman-1", "cache", "uv", {"wheels/a.whl": "wheel"})
    data = own_tool(store, "ansuman-1", "data", "pnpm", {"store/index": "packages"})
    note = store.directory("ansuman-1") / "cache/note"
    note.write_text("a plain file")

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=3, duplicates=0, in_use=0)

    for link, area in ((cache, "cache"), (data, "data"), (note, "cache")):
        assert link.is_symlink()
        assert os.readlink(link) == str((store.tools(area) / link.name).resolve())
    assert not (store.tools("cache") / "uv").is_symlink()
    # The login's old paths still read the same files, now kept once.
    assert (cache / "wheels/a.whl").read_text() == "wheel"
    assert (store.tools("cache") / "uv/wheels/a.whl").read_text() == "wheel"
    assert (data / "store/index").read_text() == "packages"
    assert note.read_text() == "a plain file"
    # Every other login gets them at its next launch.
    store.prepare(store.account("ansuman-2"))
    assert (store.directory("ansuman-2") / "cache/uv/wheels/a.whl").read_text() == "wheel"


def test_duplicate_cache_is_dropped_for_the_shared_one(store: Store) -> None:
    first = own_tool(store, "ansuman-1", "cache", "uv", {"a.whl": "from the first login"})
    second = own_tool(store, "ansuman-2", "cache", "uv", {"b.whl": "rebuildable"})

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=1, duplicates=1, in_use=0)

    shared = store.tools("cache") / "uv"
    assert first.resolve() == second.resolve() == shared.resolve()
    assert second.is_symlink()
    assert [path.name for path in shared.iterdir()] == ["a.whl"]
    assert (second / "a.whl").read_text() == "from the first login"
    assert staged(store, "cache", "uv") == []


def test_data_folders_merge_at_their_top_levels_so_every_logins_paths_resolve(
    store: Store,
) -> None:
    first = own_tool(
        store,
        "ansuman-1",
        "data",
        "uv",
        {
            "python/cpython-3.11/bin/python3.11": "3.11 of the first login",
            "python/.lock": "older lock",
            "tools/ruff/bin/ruff": "ruff 0.4 of the first login",
            "tools/ruff/lib/ruff-0.4.dist-info/RECORD": "0.4",
            "settings": "newer settings",
            "same-age": "shared copy",
        },
    )
    second = own_tool(
        store,
        "ansuman-2",
        "data",
        "uv",
        {
            "python/cpython-3.11/bin/python3.11": "3.11 of the second login",
            "python/cpython-3.11/lib/extra.py": "only in the second login's 3.11",
            "python/cpython-3.12/bin/python3.12": "only the second login had 3.12",
            "python/.lock": "newer lock",
            "tools/ruff/bin/ruff": "ruff 0.5 of the second login",
            "tools/ruff/lib/ruff-0.5.dist-info/RECORD": "0.5",
            "tools/yt-dlp/bin/yt-dlp": "only the second login had yt-dlp",
            "settings": "older settings",
            "same-age": "staged copy",
            "only-second": "moved in",
        },
    )
    for folder, lock, settings in ((first, 1000, 3000), (second, 2000, 1000)):
        age(folder / "python/.lock", lock)
        age(folder / "settings", settings)
        age(folder / "same-age", 5000)
    # Newer files inside a folder both logins have change nothing: the shared one stays whole.
    age(first / "tools/ruff/bin/ruff", 1000)
    age(second / "tools/ruff/bin/ruff", 9000)
    age(second / "python/cpython-3.11/bin/python3.11", 9000)

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=1, duplicates=1, in_use=0)

    shared = store.tools("data") / "uv"
    assert first.is_symlink() and second.is_symlink()
    assert first.resolve() == second.resolve() == shared.resolve()
    # A project virtualenv made under the second login points at this absolute path.
    assert second.parent == store.directory("ansuman-2") / "data"
    assert (second / "python/cpython-3.12/bin/python3.12").read_text() == (
        "only the second login had 3.12"
    )
    assert (second / "tools/yt-dlp/bin/yt-dlp").read_text() == "only the second login had yt-dlp"
    # Two logins' copies of one Python or one tool are never mixed file by file.
    assert sorted(
        str(path.relative_to(shared)) for path in shared.rglob("*") if not path.is_dir()
    ) == [
        "only-second",
        "python/.lock",
        "python/cpython-3.11/bin/python3.11",
        "python/cpython-3.12/bin/python3.12",
        "same-age",
        "settings",
        "tools/ruff/bin/ruff",
        "tools/ruff/lib/ruff-0.4.dist-info/RECORD",
        "tools/yt-dlp/bin/yt-dlp",
    ]
    assert (shared / "tools/ruff/bin/ruff").read_text() == "ruff 0.4 of the first login"
    assert (shared / "python/cpython-3.11/bin/python3.11").read_text() == "3.11 of the first login"
    # Plain files at the top levels: the newer one wins, the shared one on a tie.
    assert (shared / "python/.lock").read_text() == "newer lock"
    assert (shared / "settings").read_text() == "newer settings"
    assert (shared / "same-age").read_text() == "shared copy"
    assert (shared / "only-second").read_text() == "moved in"
    assert staged(store, "data", "uv") == []


def test_links_inside_tool_folders_are_moved_not_followed(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("not a tool's file")
    first = own_tool(
        store,
        "ansuman-1",
        "data",
        "uv",
        {"tools/kept/receipt": "real folder", "python/cpython-3.11": "real folder"},
    )
    (first / "alias").symlink_to(outside)
    own_tool(store, "ansuman-1", "cache", "uv", {"a.whl": "wheel"})
    second = own_tool(store, "ansuman-2", "data", "uv", {"alias/intruder": "must not land outside"})
    (second / "link").symlink_to(outside)
    (second / "tools").mkdir()
    (second / "tools/kept").symlink_to(outside)
    (second / "python").symlink_to(outside)
    (own_tool(store, "ansuman-2", "cache", "uv", {"b.whl": "wheel"}) / "link").symlink_to(outside)
    whole = store.directory("ansuman-2") / "cache/gh"
    whole.symlink_to(outside)

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=2, duplicates=2, in_use=0)

    shared = store.tools("data") / "uv"
    assert (shared / "link").is_symlink() and os.readlink(shared / "link") == str(outside)
    # A link never replaces a real folder, and a real folder never fills a link's target.
    assert not (shared / "tools/kept").is_symlink()
    assert (shared / "tools/kept/receipt").read_text() == "real folder"
    assert [path.name for path in (shared / "python").iterdir()] == ["cpython-3.11"]
    assert os.readlink(shared / "alias") == str(outside)
    assert os.readlink(whole) == str(outside) and not (store.tools("cache") / "gh").exists()
    assert [path.name for path in outside.iterdir()] == ["precious"]
    assert (outside / "precious").read_text() == "not a tool's file"
    assert staged(store, "data", "uv") == [] and staged(store, "cache", "uv") == []


def test_login_in_use_is_left_alone_until_released(store: Store) -> None:
    account = store.account("ansuman-1")
    folder = own_tool(store, account.name, "cache", "uv", {"blob": "being written"})
    # A launch holds its login shared and hands the descriptor to the native CLI, whose
    # background tools (uv, Blender, a dev server) inherit it and can outlive the chat.
    with store.account_lock(account, shared=True) as lease:
        # A busy login with nothing of its own to adopt is not even probed.
        with store.account_lock(store.account("ansuman-2"), shared=True):
            assert tool_folders.adopt(store, now=0.0) == Adopted(moved=0, duplicates=0, in_use=1)
        background = subprocess.Popen(("/bin/sleep", "60"), pass_fds=(lease,))
    try:
        assert tool_folders.adopt(store, now=DAY) == Adopted(moved=0, duplicates=0, in_use=1)
        assert not folder.is_symlink() and (folder / "blob").read_text() == "being written"
        assert not store.tools("cache").exists()
    finally:
        background.kill()
        background.wait()

    assert tool_folders.adopt(store, now=2 * DAY) == Adopted(moved=1, duplicates=0, in_use=0)
    assert folder.is_symlink() and (folder / "blob").read_text() == "being written"


@pytest.mark.parametrize("named", ["as given", "resolved"])
def test_login_a_running_process_points_into_is_left_alone(
    store: Store, tmp_path: Path, named: str
) -> None:
    # The same state folder reached through a link, as DS_HOME may be.
    (tmp_path / "linked-state").symlink_to(store.root)
    linked = Store(tmp_path / "linked-state")
    root = linked.root if named == "as given" else linked.root.resolve()
    assert linked.root != linked.root.resolve()
    idle = own_tool(store, "ansuman-1", "cache", "uv", {"a.whl": "wheel"})
    used = own_tool(store, "ansuman-2", "cache", "pip", {"http": "being read"})
    # A server that outlived its launch: it holds no lock, only the login's environment.
    # Not /bin/sleep: macOS hides the environment of Apple's own binaries from ps.
    survivor = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(60)"),
        env={**os.environ, "XDG_CACHE_HOME": str(root / "accounts/ansuman-2/cache")},
    )
    try:
        assert not store.in_use("ansuman-2")
        assert tool_folders.adopt(linked, now=0.0) == Adopted(moved=1, duplicates=0, in_use=1)
        assert idle.is_symlink()
        assert not used.is_symlink() and (used / "http").read_text() == "being read"
        assert not (store.tools("cache") / "pip").exists()
    finally:
        survivor.kill()
        survivor.wait()

    assert tool_folders.adopt(linked, now=DAY) == Adopted(moved=1, duplicates=0, in_use=0)
    assert used.is_symlink() and (used / "http").read_text() == "being read"


@pytest.mark.parametrize("failure", ["exit status", "missing", "timeout"])
def test_unknown_running_processes_adopt_nothing(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    asked = tmp_path / "asked"
    listing = tmp_path / "ps"
    listing.write_text(f"#!/bin/sh\necho asked >> {asked}\nexec /bin/sleep 5\n")
    listing.chmod(0o700)
    commands = {
        "exit status": ("/bin/sh", "-c", f"echo asked >> {asked}; exit 1"),
        "missing": (str(tmp_path / "no-ps"),),
        "timeout": (str(listing),),
    }
    monkeypatch.setattr(tool_folders, "PROCESSES", commands[failure])
    monkeypatch.setattr(tool_folders, "PROCESSES_TIMEOUT", 0.3)
    # Nothing to adopt: the processes are not even listed, and leftovers are still finished.
    leftover = store.tools("adopting") / "cache/uv/ansuman-2-1"
    leftover.mkdir(parents=True)
    (store.tools("cache") / "uv").mkdir(parents=True)
    assert tool_folders.adopt(store, now=0.0) == Adopted(moved=0, duplicates=0, in_use=0)
    assert not asked.exists() and not leftover.exists()

    folders = [
        own_tool(store, login, "cache", "pip", {"http": "cached"})
        for login in ("ansuman-1", "ansuman-2")
    ]
    leftover.mkdir()
    assert tool_folders.adopt(store, now=DAY) == Adopted(moved=0, duplicates=0, in_use=2)
    assert not any(folder.is_symlink() for folder in folders)
    assert not (store.tools("cache") / "pip").exists() and not leftover.exists()
    if failure != "missing":
        assert asked.read_text() == "asked\n"

    monkeypatch.undo()
    assert tool_folders.adopt(store, now=2 * DAY) == Adopted(moved=1, duplicates=1, in_use=0)
    assert all(folder.is_symlink() for folder in folders)


def test_adoption_never_locks_a_login_a_chat_may_open(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    for login in ("ansuman-1", "ansuman-2"):
        own_tool(store, login, "data", "uv", {f"python/{login}": login})
    opened: list[str] = []
    rename, finish = os.rename, tool_folders.finish

    def open_chat(step: str) -> None:
        for account in store.accounts():
            with store.account_lock(account, shared=True):
                opened.append(step)

    def renaming(source, target):
        open_chat("rename")
        rename(source, target)

    def finishing(current: Store) -> None:
        open_chat("finish")
        finish(current)

    monkeypatch.setattr(tool_folders.os, "rename", renaming)
    monkeypatch.setattr(tool_folders, "finish", finishing)

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=1, duplicates=1, in_use=0)
    assert opened.count("finish") == 2 and opened.count("rename") >= 4


def test_devins_own_folders_are_never_moved(store: Store) -> None:
    base = store.directory("ansuman-1")
    store.credentials(store.account("ansuman-1")).write_text("valid-login")
    own_tool(store, "ansuman-1", "cache", "devin", {"model-cache": "per login"})
    own_tool(store, "ansuman-1", "cache", "uv", {"a.whl": "wheel"})
    own_tool(store, "ansuman-1", "data", "uv", {"python/cpython-3.11": "python"})

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=2, duplicates=0, in_use=0)

    for area in ("cache", "data"):
        assert (base / area / "devin").is_dir() and not (base / area / "devin").is_symlink()
        assert [path.name for path in store.tools(area).iterdir()] == ["uv"]
    assert (base / "data/devin/credentials.toml").read_text() == "valid-login"
    assert (base / "cache/devin/model-cache").read_text() == "per login"
    assert not store.tools("adopting").exists()


def test_adoption_runs_once_a_day_and_in_one_process(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DAY == 24 * 3600
    first = own_tool(store, "ansuman-1", "cache", "uv", {"a.whl": "wheel"})
    # Another window's bridge is adopting: skip, without counting it as today's run.
    with store.lock(tool_folders.LOCK):
        assert tool_folders.adopt(store, now=1000.0) is None
    assert not first.is_symlink()
    others: list[Adopted | None] = []
    finish = tool_folders.finish

    def finishing(current: Store) -> None:
        # A second window starting while this run works, even days later by the clock.
        others.append(tool_folders.adopt(current, now=1000.0 + 5 * DAY))
        finish(current)

    with monkeypatch.context() as patch:
        patch.setattr(tool_folders, "finish", finishing)
        assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=1, duplicates=0, in_use=0)
    assert others == [None]

    second = own_tool(store, "ansuman-2", "cache", "pip", {"http": "cached"})
    assert tool_folders.adopt(store, now=1000.0 + DAY - 1) is None
    assert not second.is_symlink()
    assert tool_folders.adopt(store, now=1000.0 + DAY) == Adopted(moved=1, duplicates=0, in_use=0)
    assert second.is_symlink()
    # The lock is free again for the next window.
    with store.lock(tool_folders.LOCK):
        pass


def test_interrupted_adoption_is_finished_by_the_next_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_tool(store, "ansuman-1", "data", "uv", {"python/cpython-3.11": "first"})
    own_tool(store, "ansuman-1", "cache", "uv", {"a.whl": "wheel"})
    second = own_tool(store, "ansuman-2", "data", "uv", {"python/cpython-3.12": "second"})
    own_tool(store, "ansuman-2", "cache", "uv", {"b.whl": "duplicate"})

    def killed(current: Store) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(tool_folders, "finish", killed)
        with pytest.raises(KeyboardInterrupt):
            tool_folders.adopt(store, now=1000.0)
    # The renames are done: the login is linked, its own copy waits in staging.
    assert second.is_symlink() and not (second / "python/cpython-3.12").exists()
    assert len(staged(store, "data", "uv")) == len(staged(store, "cache", "uv")) == 1

    assert tool_folders.adopt(store, now=1000.0 + DAY) == Adopted(moved=0, duplicates=0, in_use=0)

    assert (second / "python/cpython-3.12").read_text() == "second"
    assert (second / "python/cpython-3.11").read_text() == "first"
    assert [path.name for path in (store.tools("cache") / "uv").iterdir()] == ["a.whl"]
    assert staged(store, "data", "uv") == [] and staged(store, "cache", "uv") == []


def test_folders_that_cannot_be_moved_do_not_stop_the_others(store: Store) -> None:
    store.add("ansuman-3", None)
    stuck = own_tool(store, "ansuman-1", "cache", "pip", {"http": "cached"})
    own_tool(store, "ansuman-1", "data", "uv", {"python/cpython-3.11": "first"})
    second = own_tool(store, "ansuman-2", "data", "uv", {"python/cpython-3.12": "second"})
    third = own_tool(store, "ansuman-3", "data", "uv", {"python/cpython-3.13": "third"})
    # Moving a folder to another parent, or an entry out of a folder, needs write access.
    stuck.chmod(0o500)
    (second / "python").chmod(0o500)
    try:
        assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=1, duplicates=2, in_use=0)
        assert not stuck.is_symlink() and (stuck / "http").read_text() == "cached"
        assert (third / "python/cpython-3.13").read_text() == "third"
        (waiting,) = staged(store, "data", "uv")
        assert (waiting / "python/cpython-3.12").read_text() == "second"
        assert not (second / "python/cpython-3.12").exists()
    finally:
        stuck.chmod(0o700)
        for path in store.tools("adopting").rglob("python"):
            path.chmod(0o700)

    assert tool_folders.adopt(store, now=1000.0 + DAY) == Adopted(moved=1, duplicates=0, in_use=0)
    assert stuck.is_symlink() and (stuck / "http").read_text() == "cached"
    assert (second / "python/cpython-3.12").read_text() == "second"
    assert staged(store, "data", "uv") == []


def test_linked_area_folder_is_not_adopted(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "user-cache"
    (outside / "uv").mkdir(parents=True)
    (outside / "uv/a.whl").write_text("the user's own cache")
    cache = store.directory("ansuman-1") / "cache"
    cache.rmdir()
    cache.symlink_to(outside)
    # Not a saved login: a folder left under accounts/ without its metadata.
    leftover = store.root / "accounts/leftover/cache/uv"
    leftover.mkdir(parents=True)

    assert tool_folders.adopt(store, now=1000.0) == Adopted(moved=0, duplicates=0, in_use=0)

    assert not leftover.is_symlink()
    assert not (outside / "uv").is_symlink()
    assert (outside / "uv/a.whl").read_text() == "the user's own cache"
    assert not store.tools("cache").exists()
