import asyncio
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import urllib.request
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
cold = False
if account == os.environ.get("FAKE_COLD_CATALOG_ACCOUNT"):
    counter = Path(os.environ["FAKE_COLD_CATALOG_STARTS"])
    remaining = int(counter.read_text())
    cold = remaining != 0
    if remaining > 0:
        counter.write_text(str(remaining - 1))
replies = {}
loaded = None
project = None
host = None
canceled = asyncio.Event()
side_canceled = asyncio.Event()
chains = False
# Running "busy" turns by request ID; like the native, a session/load ends them.
turns = {}
INFERENCE = "/exa.api_server_pb.ApiServerService/GetChatMessage"
STATUS = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"


class QuotaExhausted(Exception):
    pass


def call_api(path):
    # Like the native, send the host-issued key in both the header and the protobuf body.
    request = urllib.request.Request(
        host["url"] + path,
        data=b"\x0a" + bytes([len(host["key"])]) + host["key"].encode() + b"\x12\x02hi",
        headers={"Authorization": f"Basic {host['key']}-sk", "Content-Type": "application/proto"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request) as response:
        return response.read()


def infer():
    data = call_api(INFERENCE)
    if data[0] & 2:
        error = json.loads(data[5:]).get("error", {})
        if "usage quota has been exhausted" in error.get("message", ""):
            raise QuotaExhausted(error["message"])
        raise RuntimeError(error.get("code"))
    return data[5 : 5 + int.from_bytes(data[1:5], "big")].decode()


def subagent_update(session_id, key, value):
    update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "spawn-" + value["agentId"],
            "status": "completed" if value.get("success", True) else "failed",
            "_meta": {key: value},
        },
    )


async def subagent(session_id, agent_id):
    subagent_update(
        session_id,
        "cognition.ai/subagent_started",
        {"agentId": agent_id, "title": f"Title {agent_id}", "task": f"Task for {agent_id}"},
    )
    gate = Path(os.environ["FAKE_SUBAGENT_GATE"])
    while not gate.exists():
        await asyncio.sleep(0.02)
    try:
        summary, success = await asyncio.to_thread(infer), True
    except QuotaExhausted as exc:
        summary, success = f"[Error] {exc}", False
    subagent_update(
        session_id,
        "cognition.ai/subagent_completed",
        {"agentId": agent_id, "success": success, "summary": summary},
    )
    return agent_id, summary


def emit(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def configs():
    return [
        {
            "id": key,
            "name": key,
            "category": key,
            "type": "select",
            **(
                {"currentValue": "fallback-model", "options": []}
                if cold and key == "model"
                else {"currentValue": value, "options": [{"value": value, "name": value}]}
            ),
        }
        for key, value in settings.items()
    ]


def update(session_id, change, meta=None):
    params = {"sessionId": session_id, "update": change}
    if meta:
        params["_meta"] = meta
    emit({"method": "session/update", "params": params})


def chain(params):
    meta = params.get("_meta")
    return meta.get("cognition.ai/chain") if isinstance(meta, dict) else None


async def side_prompt(message, params):
    """Like the native: a read-only fork of the chat answers, tagging each update side."""
    side = {"cognition.ai/chain": "side"}
    text = params["prompt"][0]["text"]
    update(
        loaded,
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "…"}},
        side,
    )
    update(loaded, {"sessionUpdate": "usage_update", "used": 1, "size": 10}, side)
    reply = {"side": text, "account": account, "session": loaded}
    update(
        loaded,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps(reply)},
        },
        side,
    )
    stop = "end_turn"
    if text.endswith("side-wait"):
        side_canceled.clear()
        await side_canceled.wait()
        stop = "cancelled"
    emit({"id": message["id"], "result": {"stopReason": stop}})


def process_cwd():
    try:
        return os.getcwd()
    except FileNotFoundError:  # the folder was deleted after this process started in it
        return None


def whiteboard_chat(params):
    """Runs the chat-id command an MCP server would get, only when a test asks for it."""
    if (params.get("prompt") or [{}])[0].get("text") != "whiteboard-chat":
        return None
    command = shlex.split(os.environ["WHITEBOARD_CHAT_COMMAND"])
    done = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return done.stdout.strip() if done.returncode == 0 else f"error: {done.stderr.strip()}"


async def handle(message):
    global loaded, project, host, chains
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
                        "gh_config_dir": os.environ.get("GH_CONFIG_DIR"),
                        "text": (params.get("prompt") or [{}])[0].get("text"),
                        "uri": params.get("uri"),
                        "process_cwd": process_cwd(),
                        "chat_id": whiteboard_chat(params),
                        "chain": chain(params),
                        "chains": (params.get("clientCapabilities") or {})
                        .get("_meta", {})
                        .get("cognition.ai/chains"),
                    }
                )
                + "\n"
            )
    result = {}
    if method == "initialize":
        meta = (params.get("clientCapabilities") or {}).get("_meta") or {}
        chains = meta.get("cognition.ai/chains") is True
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": not bool(os.environ.get("FAKE_NO_LOAD")),
                "sessionCapabilities": {
                    "list": {},
                    "fork": {},
                    "delete": {},
                    "close": {"_meta": {"cognition.ai/force": True}},
                    **(
                        {}
                        if os.environ.get("FAKE_NO_EXTRA_DIRS")
                        else {"additionalDirectories": {}}
                    ),
                },
            },
            "authMethods": [{"id": "api-key", "name": "API key"}],
            "agentInfo": {"name": "fake", "version": "1"},
        }
    elif method == "authenticate" and params.get("methodId") == "windsurf-api-key":
        previous, host = (
            host,
            {
                "key": params["_meta"]["api_key"],
                "url": params["_meta"]["api_server_url"],
            },
        )
        try:
            await asyncio.to_thread(call_api, STATUS)
        except OSError:
            host = previous
            emit({"id": message["id"], "error": {"code": -32603, "message": "Auth failed"}})
            return
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
            for ended in turns.values():
                ended.set()
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
                    # The native offers /btw only to clients that support side chains.
                    *([{"name": "btw", "description": "Side question"}] if chains else []),
                ],
            },
        )
        result = {"configOptions": configs()}
        if method == "session/new":
            result["sessionId"] = session_id
    elif method == "session/prompt":
        assert params["sessionId"] == loaded
        if chain(params) == "side":
            await side_prompt(message, params)
            return
        if os.environ.get("FAKE_SAVE_ON_PROMPT"):
            connection.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, 0)",
                (loaded, "Test chat", project, 1),
            )
            connection.commit()
        text = params["prompt"][0]["text"]
        if account in os.environ.get("FAKE_QUOTA_ACCOUNTS", "").split(","):
            style = os.environ.get("FAKE_QUOTA_STYLE", "error")
            if style == "stopped":
                emit(
                    {
                        "method": "_cognition.ai/agent_stopped",
                        "params": {"sessionId": loaded, "cause": "quota_exhausted"},
                    }
                )
                emit({"id": message["id"], "result": {"stopReason": "end_turn"}})
                return
            data = {"cognition.ai/errorKind": "resource_exhausted", "cognition.ai/retryable": True}
            if style == "rate":
                data["cognition.ai/retryAfterSeconds"] = 30
            emit(
                {
                    "id": message["id"],
                    "error": {"code": -32011, "message": "Quota exhausted.", "data": data},
                }
            )
            return
        if text in {"infer", "subagents"}:
            try:
                upstream = await asyncio.to_thread(infer)
                spawned = (
                    dict(await asyncio.gather(subagent(loaded, "s1"), subagent(loaded, "s2")))
                    if text == "subagents"
                    else None
                )
                if spawned:
                    # The parent reads its subagents and answers in a later inference.
                    upstream = await asyncio.to_thread(infer)
            except (TypeError, OSError, RuntimeError):
                emit({"id": message["id"], "error": {"code": -32603, "message": "No API login"}})
                return
            except QuotaExhausted as exc:
                data = {
                    "cognition.ai/errorKind": "resource_exhausted",
                    "cognition.ai/retryable": True,
                }
                emit(
                    {
                        "id": message["id"],
                        "error": {"code": -32011, "message": str(exc), "data": data},
                    }
                )
                return
            reply = {"account": account, "session": loaded, "upstream": upstream}
            if spawned:
                reply["subagents"] = spawned
            update(
                loaded,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": json.dumps(reply)},
                },
            )
            emit({"id": message["id"], "result": {"stopReason": "end_turn"}})
            return
        if text == "side-noise":
            # A side answer streaming while this main turn runs.
            update(
                loaded,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "aside"},
                },
                {"cognition.ai/chain": "side"},
            )
        if text == "slow":
            await asyncio.sleep(0.5)
        if text == "wait":
            await canceled.wait()
        if text == "busy":
            update(
                loaded,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "live progress"},
                },
            )
            ended = turns[message["id"]] = asyncio.Event()
            waits = [asyncio.create_task(event.wait()) for event in (canceled, ended)]
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in waits:
                task.cancel()
            turns.pop(message["id"])
            if ended.is_set():
                emit(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32603,
                            "message": "Agent communication channel closed",
                            "data": {"cognition.ai/errorKind": "agent_channel_closed"},
                        },
                    }
                )
                return
            emit({"id": message["id"], "result": {"stopReason": "cancelled"}})
            return
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
        if text == "withdrawn":
            # The native cancels a request it no longer needs (e.g. after Stop) with its own id.
            emit({"id": 101, "method": "elicitation/create", "params": {"sessionId": loaded}})
            await asyncio.sleep(0.2)
            emit({"method": "$/cancel_request", "params": {"requestId": 101}})
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
                # The native's card event, as captured from CLI 3000.11.216: labels in _meta,
                # descriptions only in rawInput, and camelCase multiSelect only when true.
                asked = [
                    ("Pick any", True, [("One", "First"), ("Two", "Second")]),
                    ("Pick one", False, [("Fast", "Quick but rough"), ("Slow", None)]),
                    ("Plain", False, [("Yes", None), ("No", None)]),
                ]
                update(
                    loaded,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "ask_user_question:0#test",
                        "title": "Asked user Pick any",
                        "rawInput": {
                            "questions": [
                                {
                                    "question": question,
                                    "header": "T",
                                    "options": [
                                        {"label": label} | ({"description": about} if about else {})
                                        for label, about in options
                                    ],
                                }
                                | ({"multi_select": True} if multiple else {})
                                for question, multiple, options in asked
                            ]
                        },
                        "_meta": {
                            "cognition.ai/inferenceToolName": "ask_user_question",
                            "cognition.ai/eventType": "ask_user_question",
                            "cognition.ai/questions": [
                                {
                                    "question": question,
                                    "header": "T",
                                    "options": [label for label, _ in options],
                                }
                                | ({"multiSelect": True} if multiple else {})
                                for question, multiple, options in asked
                            ],
                        },
                    },
                )
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
        # A cancel tagged side stops only the side answer; the main turn keeps running.
        (side_canceled if chain(params) == "side" else canceled).set()
    elif method == "session/set_config_option":
        if cold and params["configId"] == "model":
            emit(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32002,
                        "message": "Resource not found",
                        "data": {"uri": f"Model not found: {params['value']}. Available models: "},
                    },
                }
            )
            return
        if os.environ.get("FAKE_IGNORE_CONFIG_ACCOUNT") != account:
            settings[params["configId"]] = params["value"]
        result = {"configOptions": configs()}
    elif method == "session/set_mode":
        settings["mode"] = params["modeId"]
    elif method == "session/set_model":
        settings["model"] = params["modelId"]
    elif method == "_cognition.ai/command/revise":
        result = {"command": params["command"] + " --revised"}
    elif method.startswith("_cognition.ai/mcp/") or method == "_cognition.ai/scope_search":
        # Like the native, user-scope servers live in this login's config directory.
        path = Path(os.environ["XDG_CONFIG_HOME"]) / "devin/mcp_config.json"
        config = json.loads(path.read_text())
        if method == "_cognition.ai/scope_search":
            # The composer's @mcpServer: search; the native lists connected servers.
            result = {
                "items": [
                    {"type": "resource_link", "name": name, "uri": f"mcp://{name}"}
                    for name in config["mcpServers"]
                    if params["scope_type"] == "mcpServer" and params["query"] in name
                ]
            }
        elif method == "_cognition.ai/mcp/connectServer":
            # Stands in for an OAuth sign-in that waits for the user's browser.
            while not Path(os.environ["FAKE_MCP_CONNECT_GATE"]).exists():
                await asyncio.sleep(0.01)
        elif method == "_cognition.ai/mcp/listServers":
            result = {
                "servers": [
                    {
                        "serverId": name,
                        "sourcePath": str(path),
                        "connectionStatus": "not_started",
                        "disabled": bool(server.get("disabled")),
                    }
                    for name, server in config["mcpServers"].items()
                ]
            }
        else:
            if method == "_cognition.ai/mcp/installServer":
                config["mcpServers"][params["serverId"]] = params["config"]
            elif method == "_cognition.ai/mcp/removeServer":
                config["mcpServers"].pop(params["serverId"])
            elif method == "_cognition.ai/mcp/toggleServer":
                server = config["mcpServers"][params["serverId"]]
                server["disabled"] = not server.get("disabled")
            path.write_text(json.dumps(config))
            emit({"method": "_cognition.ai/mcp/serversChanged", "params": {}})
    elif method == "session/list":
        result = {
            "sessions": [
                {"sessionId": row[0], "cwd": row[2], "title": row[1], "_meta": {"fake/row": True}}
                for row in connection.execute("SELECT * FROM sessions")
                if not params.get("cwd") or row[2] == params["cwd"]
            ],
            **({"nextCursor": "fake-next"} if params.get("limit") else {}),
        }
    if "id" in message:
        emit({"id": message["id"], "result": result})


async def periodic(kind):
    while True:
        await asyncio.sleep(0.05)
        if loaded is None:
            continue
        if kind == "work":
            update(
                loaded,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "."}},
            )
            continue
        emit(
            {
                "method": "_cognition.ai/processMemory",
                "params": {"phase": "periodic", "rssBytes": 1, "loadedSessionCount": 1},
            }
        )
        update(loaded, {"sessionUpdate": "available_commands_update", "availableCommands": []})


async def main():
    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    tasks = set()
    kind = os.environ.get("FAKE_PERIODIC")
    emitter = asyncio.create_task(periodic(kind)) if kind else None
    while line := await reader.readline():
        message = json.loads(line)
        if "method" not in message:
            replies[message["id"]].set_result(message)
            continue
        task = asyncio.create_task(handle(message))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    if emitter:
        emitter.cancel()
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(float(os.environ.get("FAKE_SHUTDOWN_DELAY", "0")))


asyncio.run(main())
sys.exit(int(os.environ.get("FAKE_EXIT_ON_EOF", "0")))
