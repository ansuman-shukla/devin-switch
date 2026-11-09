import os
from pathlib import Path

import pytest

from devin_switch.native import Native
from devin_switch.store import Store


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

    # A tool creates the same name between the listing and the link.
    shared_tool(store, "data", "pnpm")

    def taken(self, target, target_is_directory=False):
        raise FileExistsError(str(self))

    monkeypatch.setattr(Path, "symlink_to", taken)
    store.prepare(account)
    monkeypatch.undo()
    assert not os.path.lexists(base / "data/pnpm")
    store.prepare(account)
    assert (base / "data/pnpm").is_symlink()


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
