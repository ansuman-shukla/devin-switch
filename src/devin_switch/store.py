"""Private profile directories and an atomic active-account pointer."""

import fcntl
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class SwitchError(Exception):
    """An actionable failure that can be displayed without a traceback."""


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", name):
        raise SwitchError(
            "Use 1–48 lowercase letters, digits, underscores or hyphens for an alias."
        )
    return name


def validate_display_name(value: str) -> str:
    if not isinstance(value, str) or len(value.strip()) > 80 or (value and not value.isprintable()):
        raise SwitchError("Use a display name of up to 80 printable characters.")
    return value.strip()


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_json(path: Path, value: object) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Account:
    name: str
    chrome_profile: str | None
    display_name: str | None = None


@dataclass(frozen=True)
class Store:
    root: Path

    @contextmanager
    def lock(self, filename: str = "lock", *, shared: bool = False) -> Iterator[int]:
        private_directory(self.root)
        with open(self.root / filename, "a", opener=private_opener) as handle:
            try:
                fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                if filename.startswith("account-"):
                    message = (
                        "This profile is in use. Close its sessions or wait for usage refresh."
                    )
                elif filename == "usage.lock":
                    message = "Another usage refresh is active. Try again shortly."
                else:
                    message = "Another ds command is active. Finish it before switching."
                raise SwitchError(message) from exc
            yield handle.fileno()

    def account_lock(self, account: Account, *, shared: bool = False):
        return self.lock(f"account-{validate_name(account.name)}.lock", shared=shared)

    def locked(self, filename: str) -> bool:
        try:
            handle = (self.root / filename).open("r")
        except FileNotFoundError:
            return False
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def directory(self, name: str) -> Path:
        return self.root / "accounts" / validate_name(name)

    def accounts(self) -> tuple[Account, ...]:
        folder = self.root / "accounts"
        if not folder.exists():
            return ()
        return tuple(self.account(p.name) for p in sorted(folder.iterdir()) if p.is_dir())

    def account(self, name: str) -> Account:
        try:
            data = json.loads((self.directory(name) / "account.json").read_text())
        except FileNotFoundError as exc:
            raise SwitchError(f"Unknown account {name!r}. Run: ds add {name}") from exc
        except json.JSONDecodeError as exc:
            raise SwitchError(f"Account metadata is invalid for {name!r}.") from exc
        if not isinstance(data, dict) or set(data) != {"chrome_profile"}:
            raise SwitchError(f"Account metadata is invalid for {name!r}.")
        profile = data["chrome_profile"]
        if profile is not None and (not isinstance(profile, str) or not profile):
            raise SwitchError(f"Chrome profile is invalid for {name!r}.")
        try:
            display_name = json.loads((self.directory(name) / "display-name.json").read_text())
        except FileNotFoundError:
            display_name = None
        except json.JSONDecodeError as exc:
            raise SwitchError(f"Display name is invalid for {name!r}.") from exc
        if display_name is not None:
            display_name = validate_display_name(display_name) or None
        return Account(name, profile, display_name)

    def set_display_name(self, account: Account, display_name: str) -> Account:
        label = validate_display_name(display_name)
        current = self.account(account.name)
        write_json(self.directory(current.name) / "display-name.json", label or None)
        return self.account(current.name)

    def add(self, name: str, chrome_profile: str | None) -> Account:
        folder = self.directory(name)
        if folder.exists():
            raise SwitchError(f"Account {name!r} already exists; its login has been preserved.")
        private_directory(folder.parent)
        private_directory(folder)
        write_json(folder / "account.json", {"chrome_profile": chrome_profile})
        account = self.account(name)
        self.prepare(account)
        return account

    def rename(self, account: Account, name: str) -> Account:
        with self.account_lock(account):
            destination = self.directory(name)
            if destination.exists():
                raise SwitchError(f"Account {name!r} already exists; its login has been preserved.")
            selected = self.is_selected(account)
            self.directory(account.name).rename(destination)
            renamed = self.account(name)
            if selected:
                self.select(renamed)
            return renamed

    def is_selected(self, account: Account) -> bool:
        try:
            return (self.root / "selected").read_text().strip() == account.name
        except FileNotFoundError:
            return False

    def remove(self, account: Account) -> None:
        with self.account_lock(account):
            folder = self.directory(account.name)
            if folder.is_symlink():
                raise SwitchError("Refusing to remove a linked account directory.")
            selected = self.is_selected(account)
            shutil.rmtree(folder)
            if selected:
                (self.root / "selected").unlink(missing_ok=True)

    def prepare(self, account: Account) -> None:
        base = self.directory(account.name)
        for area in ("data/devin", "config", "cache", "state"):
            private_directory(base / area)
        # The native CLI keeps credentials above cli/. Share its session state only.
        for area in ("cli", "summaries"):
            shared = self.root / "shared" / area
            private_directory(shared)
            link = base / "data" / "devin" / area
            if link.is_symlink():
                if link.resolve() != shared.resolve():
                    raise SwitchError(f"Unexpected shared-session link for {account.name!r}.")
            elif link.exists():
                raise SwitchError(
                    f"Refusing to replace existing session data for {account.name!r}."
                )
            else:
                link.symlink_to(shared.resolve(), target_is_directory=True)

    def credentials(self, account: Account) -> Path:
        return self.directory(account.name) / "data" / "devin" / "credentials.toml"

    def selected(self) -> Account:
        try:
            name = (self.root / "selected").read_text().strip()
        except FileNotFoundError as exc:
            raise SwitchError("No account selected. Run: ds use <alias>") from exc
        return self.account(name)

    def select(self, account: Account) -> None:
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(account.name + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary.replace(self.root / "selected")
            finally:
                temporary.unlink(missing_ok=True)


def private_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)
