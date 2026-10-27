import json
import os
from pathlib import Path, PurePosixPath

import pytest

from devin_switch import chrome_access
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


class Protection:
    """Like macOS 27 App Data Protection: entries can be seen to exist, not listed or read."""

    def __init__(self, folder: Path) -> None:
        entries = [folder, *folder.rglob("*")]
        self.folders = [path for path in entries if path.is_dir()]
        self.files = [path for path in entries if not path.is_dir()]

    def block(self) -> None:
        for path in self.files:
            path.chmod(0)
        for path in reversed(self.folders):
            path.chmod(0o100)

    def lift(self) -> None:
        for path in self.folders:
            path.chmod(0o700)
        for path in self.files:
            path.chmod(0o600)


@pytest.fixture
def protect():
    made: list[Protection] = []

    def apply(folder: Path) -> Protection:
        made.append(Protection(folder))
        made[-1].block()
        return made[-1]

    yield apply
    for protection in made:
        protection.lift()


class FakeHelper:
    """Stands in for the helper app: records each request and answers like the dialog would."""

    def __init__(self, app: Path) -> None:
        self.app = app
        self.decision = "allow"
        self.requests: list[dict] = []
        self.protection = None

    def launch(self, app: Path, request: Path, result: Path) -> None:
        assert app == self.app
        payload = json.loads(request.read_text())
        self.requests.append(payload)
        if self.decision == "silent":
            return
        if self.decision == "allow":
            self.protection.lift()
            for item in payload["items"]:
                target = Path(payload["destination"]) / PurePosixPath(item).name
                chrome_access.copy_item(chrome_access.HELPER_DATA / item, target)
            self.protection.block()
        result.write_text(json.dumps({"decision": self.decision, "message": "broken request"}))


@pytest.fixture
def chrome_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHelper:
    app = tmp_path / "Devin Switch Chrome Access.app"
    app.mkdir()
    monkeypatch.setenv("DS_CHROME_ACCESS_APP", str(app))
    fake = FakeHelper(app)
    monkeypatch.setattr(chrome_access, "launch", fake.launch)
    return fake


@pytest.fixture(autouse=True)
def isolated_cli_state(store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DS_HOME", str(store.root))
    monkeypatch.setenv("DS_DESKTOP_EXTENSIONS", str(tmp_path / "desktop-extensions"))
    # Never show the real Chrome access popup from a test.
    monkeypatch.setenv("DS_CHROME_ACCESS_APP", str(tmp_path / "no-chrome-access.app"))
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
