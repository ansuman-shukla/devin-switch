import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from devin_switch.native import Native
from devin_switch.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "state")
    with result.lock():
        result.add("ansuman-1", None)
        result.add("ansuman-2", None)
    return result


@pytest.fixture
def native(store: Store, tmp_path: Path) -> Native:
    executable = tmp_path / "fake-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "base = pathlib.Path(os.environ['XDG_DATA_HOME']) / 'devin'\n"
        "credential = base / 'credentials.toml'\n"
        "args = sys.argv[1:]\n"
        "if args == ['auth', 'status']:\n"
        "    content = credential.read_text() if credential.exists() else ''\n"
        "    print('Logged in as test@example.invalid' if content.startswith('valid-') "
        "else 'Not logged in.')\n"
        "elif args == ['list', '--format', 'json']:\n"
        "    history = base / 'cli' / 'test-sessions.json'\n"
        "    print(history.read_text() if history.exists() else '[]')\n"
        "else:\n"
        "    print(json.dumps({'args': args, 'cwd': os.getcwd(), "
        "'account': base.parent.parent.name}))\n"
        "    sys.exit(int(os.environ.get('FAKE_EXIT', '0')))\n"
    )
    executable.chmod(0o700)
    return Native(store, executable)


@pytest.fixture
def signed_in(native: Native) -> Native:
    for account in native.store.accounts():
        native.store.credentials(account).write_text(f"valid-{account.name}")
    return native


@pytest.fixture
def save_subagent_transcript() -> Callable[[Store, str, str], None]:
    def save(store: Store, session_id: str, agent: str) -> None:
        """Write a subagent chain and head the way the native saves them in shared history."""
        database = store.root / "shared/cli/sessions.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS subagent_heads (session_id TEXT, agent_id TEXT, "
                "chain_node_id INTEGER, updated_at INTEGER, PRIMARY KEY (session_id, agent_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS message_nodes (row_id INTEGER PRIMARY KEY, "
                "session_id TEXT, node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, "
                "created_at INTEGER, metadata TEXT)"
            )
            steps = [
                {"role": "system", "content": "You are a subagent"},
                {"role": "user", "content": f"Task for {agent}"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"name": "exec", "arguments": {"command": "pytest -q"}}],
                },
                {"role": "tool", "content": f"{agent} ran the tests: 3 passed"},
            ]
            for node, step in enumerate(steps, 1):
                connection.execute(
                    "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        session_id,
                        100 * len(agent) + node,
                        (100 * len(agent) + node - 1) if node > 1 else None,
                        json.dumps(step),
                    ),
                )
            connection.execute(
                "INSERT OR REPLACE INTO subagent_heads VALUES (?, ?, ?, 0)",
                (session_id, agent, 100 * len(agent) + len(steps)),
            )

    return save


@pytest.fixture(autouse=True)
def isolated_cli_state(store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DS_HOME", str(store.root))
    monkeypatch.setenv("DS_DESKTOP_EXTENSIONS", str(tmp_path / "desktop-extensions"))
    for name in (
        "DS_RUN_ID",
        "DS_ACP_RUN_ID",
        "DS_EXECUTABLE",
        "DS_FROZEN",
        "WHITEBOARD_CHAT_COMMAND",
        "WHITEBOARD_OPEN",
    ):
        monkeypatch.delenv(name, raising=False)
    # No inherited authentication should reach any fixture subprocess.
    for name in tuple(os.environ):
        if name.startswith(("WINDSURF_", "CODEIUM_", "OTEL_")):
            monkeypatch.delenv(name)
