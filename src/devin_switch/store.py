"""Private profile directories and an atomic active-account pointer."""

import fcntl
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

NEW_ACCOUNT_CONFIG = {"attribution": False}
EMPTY_MCP_CONFIG: dict = {"mcpServers": {}}
# The native CLI has no setting for its own folders, so XDG stays per login and every tool an
# agent runs (uv, pnpm, pip, ...) inherits it. Their folders in these areas are kept once.
TOOL_AREAS = ("cache", "data")
NATIVE_FOLDER = "devin"


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


def json_source(text: str) -> str:
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/',
        lambda match: match[0] if match[0].startswith('"') else re.sub(r"[^\r\n]", " ", match[0]),
        text,
    )
    return re.sub(
        r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
        lambda match: match[0] if match[0].startswith('"') else " ",
        text,
    )


def retire(path: Path) -> None:
    path.rename(path.with_name(f"{path.name}.superseded-{time.time_ns()}"))


def adopt_mcp_tokens(source: Path, destination: Path) -> None:
    for folder, _, names in os.walk(source):
        for name in names:
            item = Path(folder) / name
            target = destination / item.relative_to(source)
            if item.is_symlink() or target.is_symlink():
                continue
            private_directory(target.parent)
            if not target.exists():
                os.replace(item, target)
            elif item.stat().st_mtime > target.stat().st_mtime:
                # Swap without a gap, so running agents never see a missing token.
                previous = item.with_name(f".{item.name}.previous")
                shutil.copy2(target, previous)
                os.replace(item, target)
                previous.rename(item)
    if any(names for _, _, names in os.walk(source)):
        retire(source)
    else:
        shutil.rmtree(source)


def adopt_mcp_config(source: Path, destination: Path) -> None:
    try:
        incoming = json.loads(json_source(source.read_text()))
        current = (
            json.loads(json_source(destination.read_text()))
            if destination.exists()
            else EMPTY_MCP_CONFIG
        )
        if current == EMPTY_MCP_CONFIG:
            os.replace(source, destination)
            return
        added, existing = incoming.get("mcpServers", {}), current.get("mcpServers", {})
        if not all(isinstance(value, dict) for value in (added, existing)):
            raise ValueError("Invalid server map")
    except (ValueError, AttributeError) as exc:
        raise SwitchError(
            f"Cannot share MCP servers from {source}; fix or move that file and retry."
        ) from exc
    if source.stat().st_mtime > destination.stat().st_mtime:
        servers = {**existing, **added}
    else:
        servers = {**added, **existing}
    merged = {**incoming, **current, "mcpServers": servers}
    if merged != current:
        write_json(destination, merged)
    retire(source)


def share_link(name: str, link: Path, target: Path, adopt) -> None:
    private_directory(link.parent)
    private_directory(target.parent)
    for _ in range(3):
        if link.is_symlink():
            if link.resolve() != target.resolve():
                raise SwitchError(f"Unexpected shared MCP link for {name!r}.")
        elif link.exists():
            adopt(link, target)
        if adopt is adopt_mcp_tokens:
            private_directory(target)
        elif not target.exists():
            write_json(target, EMPTY_MCP_CONFIG)
        if link.is_symlink():
            return
        try:
            link.symlink_to(target.resolve(), target_is_directory=target.is_dir())
            return
        except FileExistsError:
            continue
    raise SwitchError("MCP settings changed during launch; retry.")


@dataclass(frozen=True)
class Account:
    name: str
    chrome_profile: str | None
    display_name: str | None = None


@dataclass(frozen=True)
class Store:
    root: Path

    @contextmanager
    def lock(
        self, filename: str = "lock", *, shared: bool = False, timeout: float = 0
    ) -> Iterator[int]:
        private_directory(self.root)
        deadline = time.monotonic() + timeout
        with open(self.root / filename, "a", opener=private_opener) as handle:
            while True:
                try:
                    fcntl.flock(
                        handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
                    )
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.05, remaining))
                        continue
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
        return self.lock(account_lock_name(validate_name(account.name)), shared=shared)

    def in_use(self, name: str) -> bool:
        """Whether a launch, or a background process it started, still holds this login."""
        return self.locked(account_lock_name(name))

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

    def due(self, marker: str, interval: float, now: float) -> bool:
        """Whether background upkeep last ran over `interval` ago; a due run is recorded."""
        path = self.root / marker
        try:
            if now - json.loads(path.read_text()) < interval:
                return False
        except (OSError, ValueError, TypeError):
            pass
        write_json(path, now)
        return True

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
        private_directory(folder / "config/devin")
        write_json(folder / "config/devin/config.json", NEW_ACCOUNT_CONFIG)
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
        for area in ("data/devin", "config/devin", "cache", "state"):
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
        self.share_mcp(account)
        self.share_tools(account)

    def tools(self, area: str) -> Path:
        return self.root / "shared" / "tools" / area

    def share_tools(self, account: Account) -> None:
        """Link the tool folders other logins already share; a launch never fails over them.

        Only names this login lacks are linked. Its own real folders wait for
        `tool_folders.adopt`, which runs in the background while the login is idle.
        """
        base = self.directory(account.name)
        for area in TOOL_AREAS:
            shared = self.tools(area)
            try:
                missing = set(os.listdir(shared)) - set(os.listdir(base / area)) - {NATIVE_FOLDER}
                for name in missing:
                    (base / area / name).symlink_to(shared.resolve() / name)
            except OSError:
                # No shared folder yet, or a tool made this name meanwhile: nothing to repair.
                continue

    def share_mcp(self, account: Account) -> None:
        # MCP servers and their OAuth tokens belong to the user, not to one Devin login.
        # Adopt every profile at once so a token saved under another login is never stranded.
        links = tuple(
            (base.name, link, self.root / "shared" / target, adopt)
            for base in sorted((self.root / "accounts").iterdir())
            if base.is_dir() and not base.is_symlink() and (base / "account.json").is_file()
            for link, target, adopt in (
                (base / "data/devin/mcp", "mcp", adopt_mcp_tokens),
                (base / "config/devin/mcp_config.json", "config/mcp_config.json", adopt_mcp_config),
            )
        )
        if all(
            link.is_symlink() and target.exists() and link.resolve() == target.resolve()
            for _, link, target, _ in links
        ):
            return
        with self.lock("mcp.lock", timeout=5):
            for name, link, target, adopt in links:
                try:
                    share_link(name, link, target, adopt)
                except SwitchError:
                    # Another profile's unreadable file must not block this launch.
                    if name == account.name:
                        raise

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


def account_lock_name(name: str) -> str:
    return f"account-{name}.lock"


def private_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)
