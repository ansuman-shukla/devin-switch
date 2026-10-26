"""Invoke the installed CLI using an account-specific environment."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from devin_switch.executable import ds_command
from devin_switch.store import Account, Store, SwitchError

BUNDLED_CLI = Path(
    "/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin"
)
AUTH_ENVIRONMENT = frozenset(
    {
        "WINDSURF_API_KEY",
        "CODEIUM_API_KEY",
        "DEVIN_API_KEY",
        "DEVIN_AUTH_TOKEN",
        "DEVIN_API_URL",
        "DEVIN_REMOTE_AUTH_TOKEN",
        "DEVIN_REMOTE_SESSION_TOKEN",
        "DEVIN_OUTPOSTS_TOKEN",
        "DEVIN_OUTPOST_CONNECT_TOKEN",
    }
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# `auth status` is a network round trip (about 1.6 s), paid on every GUI chat open. A
# confirmed login is trusted for this long unless its credentials file changes.
LOGIN_CHECK_TTL = 300
verified_logins: dict[tuple, float] = {}
CAPTURE_TIMEOUT = 30
TELEMETRY_VARIABLE = "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
RECEIVER_ENDPOINT = re.compile(r"http://127\.0\.0\.1:\d+/[0-9a-f]{32}/v1/logs")


def find_binary() -> Path:
    candidate = os.environ.get("DS_BINARY") or shutil.which("devin")
    candidates = (
        (Path(candidate),)
        if candidate
        else (
            BUNDLED_CLI,
            Path.home() / ".local/bin/devin",
            Path("/opt/homebrew/bin/devin"),
            Path("/usr/local/bin/devin"),
        )
    )
    for binary in candidates:
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary.resolve()
    raise SwitchError("Devin CLI was not found. Install it or set DS_BINARY to its executable.")


def user_config_home(store: Store) -> Path:
    """The incoming user-level XDG config directory, ignoring a Switch account's isolated one."""
    if configured := os.environ.get("XDG_CONFIG_HOME"):
        config = Path(configured).resolve()
        roots = {store.root.resolve()}
        if inherited := os.environ.get("DS_HOME"):
            roots.add(Path(inherited).expanduser().resolve())
        isolated = config.name == "config" and any(
            config.parent.parent == root / "accounts" for root in roots
        )
        if not isolated:
            return config
    return Path.home() / ".config"


def github_config_directory(store: Store) -> str:
    return os.environ.get("GH_CONFIG_DIR") or str(user_config_home(store) / "gh")


@dataclass(frozen=True)
class Native:
    store: Store
    binary: Path
    lock_fds: tuple[int, ...] = ()
    run_id: str | None = None
    select_on_start: bool = False
    launch_id: str | None = None

    def track_process(self, pid: int) -> None:
        if self.launch_id:
            from devin_switch.sessions import record_process

            record_process(self.store, self.launch_id, pid, role="native")

    @contextmanager
    def launch_selection(self, account: Account) -> Iterator[None]:
        with self.store.lock(timeout=5) if self.select_on_start else nullcontext():
            yield
            if self.select_on_start:
                self.store.select(account)

    def environment(self, account: Account) -> dict[str, str]:
        self.store.prepare(account)
        base = self.store.directory(account.name)
        excluded = AUTH_ENVIRONMENT | {"DS_RUN_ID", "DS_ACP_RUN_ID", "DS_EXECUTABLE", "DS_FROZEN"}
        if RECEIVER_ENDPOINT.fullmatch(os.environ.get(TELEMETRY_VARIABLE, "")):
            excluded |= {TELEMETRY_VARIABLE}
        environment = {
            **{key: value for key, value in os.environ.items() if key not in excluded},
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "XDG_STATE_HOME": str(base / "state"),
            "GH_CONFIG_DIR": github_config_directory(self.store),
            "CHISEL_SESSION_DB": str(self.store.root / "shared/cli/sessions.db"),
            "DS_HOME": str(self.store.root),
            "DS_BINARY": str(self.binary),
        }
        # Lets the whiteboard MCP keep one board per conversation across account handoffs.
        environment.setdefault("WHITEBOARD_CHAT_COMMAND", ds_command("chat", "id"))
        if self.run_id:
            environment.update(
                DS_RUN_ID=self.run_id,
                DS_EXECUTABLE=sys.executable,
                DS_FROZEN="1" if getattr(sys, "frozen", False) else "0",
            )
        return environment

    def capture(
        self, account: Account, arguments: tuple[str, ...], *, cwd: Path | None = None
    ) -> str:
        try:
            result = subprocess.run(
                (str(self.binary), *arguments),
                env=self.environment(account),
                capture_output=True,
                text=True,
                timeout=CAPTURE_TIMEOUT,
                check=False,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise SwitchError(
                f"Devin {' '.join(arguments)} timed out for {account.name!r} "
                f"after {CAPTURE_TIMEOUT:g} seconds."
            ) from exc
        if result.returncode:
            # Native authentication errors can include credential material.
            raise SwitchError(
                f"Devin {' '.join(arguments)} failed for {account.name!r} "
                f"(exit {result.returncode}); retry with ds login {account.name}."
            )
        return result.stdout

    def saved_login(self, account: Account) -> bool:
        """A saved credentials file, checked locally without asking Devin's server."""
        credentials = self.store.credentials(account)
        if credentials.is_symlink():
            raise SwitchError(f"Credentials must be a separate regular file for {account.name!r}.")
        if not credentials.is_file() or credentials.stat().st_size == 0:
            return False
        credentials.chmod(0o600)
        return True

    def authenticated(self, account: Account) -> bool:
        if not self.saved_login(account):
            return False
        credentials = self.store.credentials(account)
        details = credentials.stat()
        key = (str(credentials), details.st_ino, details.st_mtime_ns, details.st_size)
        if verified_logins.get(key, 0) > time.monotonic():
            return True
        output = ANSI_ESCAPE.sub("", self.capture(account, ("auth", "status")))
        if not re.search(r"^Logged in(?:[ .]|$)", output, re.MULTILINE):
            return False
        verified_logins[key] = time.monotonic() + LOGIN_CHECK_TTL
        return True

    def require_login(self, account: Account, *, network: bool = True) -> None:
        if not (self.authenticated(account) if network else self.saved_login(account)):
            raise SwitchError(f"No saved login for {account.name!r}. Run: ds login {account.name}")

    def interactive(self, account: Account, arguments: tuple[str, ...]) -> int:
        previous_umask = os.umask(0o077)
        try:
            if self.run_id and sys.stdin.isatty() and sys.stdout.isatty():
                from devin_switch import terminal

                return terminal.run(self, account, arguments)
            with ExitStack() as stack:
                with self.launch_selection(account):
                    process = stack.enter_context(
                        subprocess.Popen(
                            (str(self.binary), *arguments),
                            env=self.environment(account),
                            pass_fds=self.lock_fds,
                        )
                    )
                    self.track_process(process.pid)
                code = process.wait()
            return code if code >= 0 else 128 - code
        finally:
            os.umask(previous_umask)

    def sessions(self, account: Account, *, cwd: Path | None = None) -> list[object]:
        output = self.capture(account, ("list", "--format", "json"), cwd=cwd)
        try:
            sessions = json.loads(output)
        except json.JSONDecodeError as exc:
            raise SwitchError("Devin returned an unreadable session list.") from exc
        if not isinstance(sessions, list):
            raise SwitchError("Devin returned an unexpected session list format.")
        return sessions
