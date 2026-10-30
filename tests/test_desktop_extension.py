import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import pytest
from test_acp import acp_native as acp_native  # noqa: F401 (a fixture)

from devin_switch import side
from devin_switch.store import Store

TESTS = sorted((Path(__file__).parent / "desktop_extension").glob("*.test.js"))


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_desktop_extension_node_suite() -> None:
    """The Desktop panels extension is plain JavaScript; its tests run under node:test."""
    assert TESTS
    result = subprocess.run(
        ("node", "--test", *map(str, TESTS)), capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


DESKTOP = Path("/Applications/Devin.app/Contents/MacOS/Devin")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
WHITEBOARD = shutil.which("whiteboard-mcp")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_in_desktop(tmp_path: Path, short: str, environment: dict) -> dict:
    """Run integration/index.js in a real, isolated Desktop window with the packaged panels."""
    from devin_switch import panels

    extension = tmp_path / "extension"
    with zipfile.ZipFile(panels.build_vsix(tmp_path)) as archive:
        for name in archive.namelist():
            if name.startswith("extension/"):
                target = extension / name.removeprefix("extension/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result_file = tmp_path / "result.json"
    inherited = {k: v for k, v in os.environ.items() if not k.startswith(("VSCODE_", "ELECTRON_"))}
    launched = subprocess.run(
        (
            str(DESKTOP),
            f"--extensionDevelopmentPath={extension}",
            f"--extensionTestsPath={Path(__file__).parent / 'desktop_extension/integration'}",
            f"--user-data-dir={short}/u",
            f"--extensions-dir={short}/e",
            "--disable-workspace-trust",
            "--skip-welcome",
            "--new-window",
            str(workspace),
        ),
        env={**inherited, **environment, "PANELS_RESULT": str(result_file)},
        timeout=150,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result_file.exists(), (launched.stdout + launched.stderr)[-3000:]
    return json.loads(result_file.read_text())


@pytest.mark.skipif(
    os.environ.get("DS_TEST_DESKTOP") != "1"
    or not DESKTOP.exists()
    or not CHROME.exists()
    or not WHITEBOARD,
    reason="Opens a real, isolated Desktop window with whiteboard-mcp; set DS_TEST_DESKTOP=1",
)
def test_real_desktop_shows_each_board_in_its_own_tab_and_watches_the_agent_browser(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devin_switch import agent_browser, browser

    # Electron's IPC socket path must stay under 104 characters; pytest's tmp_path is longer.
    short = tempfile.mkdtemp(prefix="dsp-", dir="/tmp")
    home = Path(short) / "wb"
    whiteboard_env = {
        **os.environ,
        "WHITEBOARD_HOME": str(home),
        "WHITEBOARD_PORT": str(free_port()),
        "WHITEBOARD_IDLE_MS": "0",
    }
    daemon = subprocess.Popen((WHITEBOARD, "serve"), env=whiteboard_env, stdin=subprocess.DEVNULL)
    chat = agent_browser.Lease(store)
    try:
        deadline = time.monotonic() + 20
        while not (home / "daemon.json").exists():
            assert time.monotonic() < deadline, "whiteboard daemon did not start"
            time.sleep(0.1)
        port = json.loads((home / "daemon.json").read_text())["port"]
        token = (home / "token").read_text().strip()

        chrome_data = tmp_path / "chrome"
        (chrome_data / "Profile 1").mkdir(parents=True)
        profiles = {"Profile 1": {"name": "Test", "user_name": "panel@example.invalid"}}
        (chrome_data / "Local State").write_text(json.dumps({"profile": {"info_cache": profiles}}))
        monkeypatch.setattr(browser, "CHROME_DATA", chrome_data)
        monkeypatch.setenv("DS_CHROME_DATA", str(chrome_data))
        monkeypatch.setenv("DS_BROWSER_CHROME", str(CHROME))
        chat.acquire(browser.profiles()[0])
        chrome_port = agent_browser.running(store, "Profile 1")["port"]

        result = run_in_desktop(
            tmp_path,
            short,
            {
                "DS_HOME": str(store.root),
                "WHITEBOARD_HOME": str(home),
                "PANELS_WB_URL": f"http://127.0.0.1:{port}",
                "PANELS_WB_TOKEN": token,
                "PANELS_CHROME_PORT": str(chrome_port),
            },
        )
        assert "error" not in result, result["error"]
        assert result["shown"] and result["canvasTab"]
        # The window id is the extension host's pid: the process that also starts the window's
        # chats with WINDSURF_EXT_HOST_PID=<its pid>.
        assert result["hostId"].isdigit() and int(result["hostId"]) != os.getpid()
        assert "--utility-sub-type=node.mojom.NodeService" in result["hostProcess"]
        # Never in an unrelated window; the window's extension-host pid routes a board here.
        assert result["foreign"] == {"noHint": 409, "otherWindow": 409}
        assert result["shownByHint"] == 200 and result["secondTab"]
        # One tab per board, both live; showing the first board again reveals its tab.
        assert result["viewers"]["panel-test"] >= 1 and result["viewers"]["panel-test-2"] >= 1
        assert result["reshown"] == 200 and result["active"] == "How MCP works"
        assert sorted(result["boardTabs"]) == ["Auth flow", "How MCP works"]
        assert min(result["viewersAfter"].values()) >= 1
        assert result["browserTab"] and result["browser"]["attached"] >= 1
    finally:
        chat.release()
        daemon.terminate()
        daemon.wait(timeout=15)
        shutil.rmtree(short, ignore_errors=True)
        if current := agent_browser.state(store, "Profile 1"):
            agent_browser.stop(current)


@pytest.mark.skipif(
    os.environ.get("DS_TEST_DESKTOP") != "1" or not DESKTOP.exists(),
    reason="Opens a real, isolated Desktop window; set DS_TEST_DESKTOP=1",
)
def test_real_desktop_btw_opens_the_side_panel_which_answers_and_follows_up(
    tmp_path: Path, acp_native
) -> None:
    from devin_switch.executable import command_prefix

    short = tempfile.mkdtemp(prefix="dsp-", dir="/tmp")
    project = tmp_path / "project"
    project.mkdir()
    try:
        result = run_in_desktop(
            tmp_path,
            short,
            {
                "PANELS_SIDE": "1",
                "PANELS_DS": json.dumps(command_prefix()),
                "PANELS_PROJECT": str(project),
                "DS_HOME": str(acp_native.store.root),
                "DS_BINARY": str(acp_native.binary),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert "error" not in result, json.dumps(result, indent=1)
    assert result["commands"] == ["switchPanels.openSideChat", "switchPanels.askOnSide"]
    assert result["first"] == result["second"] == {"stopReason": "end_turn"}
    assert result["sideTab"] and result["askedOnSide"] == 1
    first, second = result["prompts"]
    assert first == "what is a cache?"
    # The panel streamed the first answer into its thread and sent it back as history.
    answer = json.dumps({"side": first, "account": "ansuman-1", "session": result["sessionId"]})
    assert second == side.prompt_text(
        [{"user": "what is a cache?", "assistant": answer}], "is it shared?"
    )
