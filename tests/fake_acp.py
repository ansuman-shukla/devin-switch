import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

account = Path(os.environ["XDG_DATA_HOME"]).parent.name
if sys.argv[1:] == ["auth", "status"]:
    print("Logged in as test@example.invalid")
    sys.exit(0)

assert sys.argv[-1] == "acp"
assert not any(key in os.environ for key in ("WINDSURF_API_KEY", "DEVIN_AUTH_TOKEN", "DS_RUN_ID"))
database = Path(os.environ["CHISEL_SESSION_DB"])
connection = sqlite3.connect(database)
connection.execute(
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT, "
    "working_directory TEXT, last_activity_at INTEGER, hidden INTEGER DEFAULT 0)"
)
connection.commit()
settings = {"mode": "ask", "model": "test-model"}
replies = {}
loaded = None
project = None
canceled = asyncio.Event()


def emit(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def configs():
    return [
        {
            "id": key,
            "name": key,
            "category": key,
            "type": "select",
            "currentValue": value,
            "options": [{"value": value, "name": value}],
        }
        for key, value in settings.items()
    ]


def update(session_id, change):
    emit({"method": "session/update", "params": {"sessionId": session_id, "update": change}})


async def handle(message):
    global loaded, project
    method, params = message["method"], message.get("params", {})
    if path := os.environ.get("FAKE_ACP_LOG"):
        with open(path, "a") as handle:
            handle.write(
                json.dumps(
                    {
                        "method": method,
                        "account": account,
                        "pid": os.getpid(),
                        "sessionId": params.get("sessionId"),
                        "args": sys.argv[1:],
                    }
                )
                + "\n"
            )
    result = {}
    if method == "initialize":
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": not bool(os.environ.get("FAKE_NO_LOAD")),
                "sessionCapabilities": {"list": {}, "fork": {}},
            },
            "authMethods": [{"id": "api-key", "name": "API key"}],
            "agentInfo": {"name": "fake", "version": "1"},
        }
    elif method in {"session/new", "session/load"}:
        if expected := os.environ.get("FAKE_EXPECT_CONTEXT"):
            for key, value in json.loads(expected).items():
                assert params[key] == value
        session_id = params.get("sessionId", "chat-" + uuid.uuid4().hex)
        if method == "session/load":
            if account in os.environ.get("FAKE_FAIL_LOAD_ACCOUNT", "").split(","):
                emit(
                    {
                        "id": message["id"],
                        "error": {"code": -32603, "message": "sensitive-native-diagnostic"},
                    }
                )
                return
            assert connection.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "historical replay"},
                },
            )
        elif not os.environ.get("FAKE_SAVE_ON_PROMPT"):
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, 0)",
                (session_id, "Test chat", params["cwd"], 1),
            )
            connection.commit()
        loaded = session_id
        project = params["cwd"]
        update(
            session_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "help", "description": "Help"},
                    {"name": "login", "description": "Login"},
                    {"name": "logout", "description": "Logout"},
                ],
            },
        )
        result = {"configOptions": configs()}
        if method == "session/new":
            result["sessionId"] = session_id
    elif method == "session/prompt":
        assert params["sessionId"] == loaded
        if os.environ.get("FAKE_SAVE_ON_PROMPT"):
            connection.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, 0)",
                (loaded, "Test chat", project, 1),
            )
            connection.commit()
        text = params["prompt"][0]["text"]
        if text == "slow":
            await asyncio.sleep(0.5)
        if text == "wait":
            await canceled.wait()
        if text == "crash":
            sys.stderr.write("sensitive-native-diagnostic\n")
            os._exit(7)
        if text == "background":
            subprocess.Popen(
                (sys.executable, "-c", "import time; time.sleep(2)"),
                stdin=subprocess.DEVNULL,
                close_fds=False,
            )
        decision = {}
        if text in {"permission", "question"}:
            future = asyncio.get_running_loop().create_future()
            replies[100] = future
            request = {
                "id": 100,
                "method": "session/request_permission",
                "params": {
                    "sessionId": loaded,
                    "toolCall": {"toolCallId": "tool", "title": "Test", "kind": "execute"},
                    "options": [
                        {"optionId": "allow", "name": "Yes", "kind": "allow_once"},
                        {"optionId": "reject", "name": "No", "kind": "reject_once"},
                    ],
                },
            }
            if text == "question":
                request["method"] = "elicitation/create"
                request["params"] = {
                    "sessionId": loaded,
                    "mode": "form",
                    "message": "Choose an approach",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"approach": {"type": "string", "enum": ["one", "two"]}},
                    },
                }
            emit(request)
            response = await future
            assert response["id"] == 100
            decision = {"decision": response}
        update(
            loaded,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {
                    "type": "text",
                    "text": json.dumps(
                        {"account": account, "session": loaded, "settings": settings, **decision}
                    ),
                },
            },
        )
        result = {"stopReason": "cancelled" if text == "wait" else "end_turn"}
    elif method == "session/cancel":
        canceled.set()
    elif method == "session/set_config_option":
        if os.environ.get("FAKE_IGNORE_CONFIG_ACCOUNT") != account:
            settings[params["configId"]] = params["value"]
        result = {"configOptions": configs()}
    elif method == "session/set_mode":
        settings["mode"] = params["modeId"]
    elif method == "session/set_model":
        settings["model"] = params["modelId"]
    elif method == "_cognition.ai/command/revise":
        result = {"command": params["command"] + " --revised"}
    elif method == "session/list":
        result = {
            "sessions": [
                {"sessionId": row[0], "cwd": row[2], "title": row[1]}
                for row in connection.execute("SELECT * FROM sessions")
            ]
        }
    if "id" in message:
        emit({"id": message["id"], "result": result})


async def main():
    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    tasks = set()
    while line := await reader.readline():
        message = json.loads(line)
        if "method" not in message:
            replies[message["id"]].set_result(message)
            continue
        task = asyncio.create_task(handle(message))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(float(os.environ.get("FAKE_SHUTDOWN_DELAY", "0")))


asyncio.run(main())
sys.exit(int(os.environ.get("FAKE_EXIT_ON_EOF", "0")))
