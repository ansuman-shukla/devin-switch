import os
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


@pytest.fixture(autouse=True)
def isolated_cli_state(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_HOME", str(store.root))
    for name in ("DS_RUN_ID", "DS_EXECUTABLE", "DS_FROZEN"):
        monkeypatch.delenv(name, raising=False)
    # No inherited authentication should reach any fixture subprocess.
    for name in tuple(os.environ):
        if name.startswith(("WINDSURF_", "CODEIUM_")):
            monkeypatch.delenv(name)
