"""Move the tool folders logins filled privately (uv, pnpm, pip, ...) into the shared copy.

`Store.prepare` links only names a login lacks, cheaply, on every launch. This is the heavier
half, run in the background: a login's own real folders become, or are folded into, the one
shared folder, and the login keeps a link, so absolute paths into it still resolve.
"""

import os
import shutil
import stat
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from devin_switch.store import NATIVE_FOLDER, TOOL_AREAS, Store, SwitchError, private_directory

INTERVAL = 24 * 3600
MARKER = "tools-adopted.json"
LOCK = "tools.lock"
STAGING = "adopting"


@dataclass(frozen=True)
class Adopted:
    moved: int  # A login's folder became the shared one.
    duplicates: int  # The shared one existed: folded into it (data) or dropped (cache).
    in_use: int  # Logins left for a later run: a launch or its background process holds them.


def entries(folder: Path) -> list[Path]:
    if folder.is_symlink() or not folder.is_dir():
        return []
    return sorted(folder.iterdir())


def logins(store: Store) -> list[Path]:
    return [
        base
        for base in entries(store.root / "accounts")
        if base.is_dir() and not base.is_symlink() and (base / "account.json").is_file()
    ]


def private_tools(base: Path) -> list[tuple[str, Path]]:
    """A login's own tool folders and files: all but Devin's folder and existing links."""
    return [
        (area, entry)
        for area in TOOL_AREAS
        for entry in entries(base / area)
        if entry.name != NATIVE_FOLDER and not entry.is_symlink()
    ]


def fold(source: Path, target: Path) -> None:
    """Merge a staged data entry into the shared one, never following links.

    Project virtualenvs point at absolute paths inside a login's data folder (uv's Pythons),
    so the shared folder keeps the union of what every login had; a newer file wins.
    """
    incoming = source.lstat()
    try:
        existing = target.lstat()
    except FileNotFoundError:
        os.rename(source, target)
        return
    if stat.S_ISDIR(incoming.st_mode) and stat.S_ISDIR(existing.st_mode):
        for name in os.listdir(source):
            fold(source / name, target / name)
    elif (
        stat.S_IFMT(incoming.st_mode) == stat.S_IFMT(existing.st_mode)
        and incoming.st_mtime_ns > existing.st_mtime_ns
    ):
        os.replace(source, target)


def discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def finish(store: Store) -> None:
    """The slow part, also for copies an interrupted run left staged."""
    for area in TOOL_AREAS:
        for staged in entries(store.tools(STAGING) / area):
            for copy in entries(staged):
                try:
                    if area == "data":
                        fold(copy, store.tools(area) / staged.name)
                    # A cache is rebuildable, so the shared copy simply wins.
                    discard(copy)
                except OSError:
                    # E.g. a read-only tree: it stays staged for the next run, and the
                    # other copies are still finished.
                    continue


def adopt(store: Store, *, now: float | None = None) -> Adopted | None:
    """Share the tool folders of every idle login, at most once a day and in one process.

    Renames come first because they are instant: a login is without its folder only between
    a rename and the link that replaces it. Merging and deleting follow for all logins.
    """
    now = time.time() if now is None else now
    with ExitStack() as stack:
        try:
            stack.enter_context(store.lock(LOCK))
        except SwitchError:
            return None  # Another window's bridge is doing it.
        if not store.due(MARKER, INTERVAL, now):
            return None
        moved = duplicates = in_use = 0
        for base in logins(store):
            tools = private_tools(base)
            if not tools:
                continue
            # Probed, not held: a chat opening on this login must never find it locked.
            if store.in_use(base.name):
                in_use += 1
                continue
            for area, entry in tools:
                target = store.tools(area) / entry.name
                try:
                    if os.path.lexists(target):
                        parking = store.tools(STAGING) / area / entry.name
                        private_directory(parking)
                        os.rename(entry, parking / f"{base.name}-{time.time_ns()}")
                        duplicates += 1
                    else:
                        private_directory(target.parent)
                        os.rename(entry, target)
                        moved += 1
                    entry.symlink_to(target.resolve())
                except OSError:
                    # The tool removed or recreated its folder meanwhile, or it cannot be
                    # moved (read-only): a later run or the login's next launch links it,
                    # and the other folders are still adopted.
                    continue
        finish(store)
        return Adopted(moved, duplicates, in_use)
