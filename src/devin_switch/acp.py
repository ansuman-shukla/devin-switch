import asyncio
import copy
import json
import platform
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from devin_switch import acp_state, handoff, relay, sessions, subagents
from devin_switch.native import Native
from devin_switch.store import Account, SwitchError, write_json

MAX_MESSAGE = 8 * 1024 * 1024
LIFECYCLE_TIMEOUT = 30
SHUTDOWN_TIMEOUT = 10
IDLE_TIMEOUT = 15 * 60
IDLE_RECHECK = 30
HANDOVER_TIMEOUT = 10
ORPHAN_TIMEOUT = 30
LISTER_IDLE = 60
SPARE_IDLE = 30 * 60
LOCAL_COMMANDS = [
    {
        "name": "switch",
        "description": "Resume this chat with another saved account; no model request",
        "input": {"hint": "optional account alias"},
    },
    {
        "name": "switch-continue",
        "description": f"Switch accounts, then send “{handoff.CONTINUE_PROMPT}” to resume the work",
        "input": {"hint": "optional account alias"},
    },
    {"name": "switch-status", "description": "Show this chat's saved account and exact session ID"},
    {
        "name": "resume-subagents",
        "description": "Ask the agent to continue subagents that stopped before finishing",
    },
    {
        "name": "auto-switch",
        "description": "Show or set switching automatically when usage runs out (all chats)",
        "input": {"hint": "on or off"},
    },
]
LOCAL_NAMES = {"/" + command["name"] for command in LOCAL_COMMANDS}
AGENT_STOPPED = "_cognition.ai/agent_stopped"
RESOURCE_EXHAUSTED = -32011
RESOURCE_NOT_FOUND = -32002
AUTO_SWITCH_LIMIT = 3
CATALOG_ATTEMPTS = 3
CATALOG_RETRY_DELAY = 1.0
BLOCKED_COMMANDS = {"login", "logout", "org", "new", "resume"}
LIVE_CONTROLS = {
    "session/set_config_option",
    "session/set_mode",
    "session/set_model",
    "_cognition.ai/command/revise",
    "_cognition.ai/subagent/cancel",
}
OPENING = {"session/new", "session/load"}
# Project MCP config the native reads from its process folder (not the session cwd).
PROJECT_MCP_FILES = (
    ".mcp.json",
    ".devin/mcp_config.json",
    ".devin/mcp_config.local.json",
    ".cursor/mcp.json",
)
# Editor state Desktop sends once (didOpen right after initialize): replay it to later natives.
# The value says whether the state is kept per document/repository uri or only the latest.
EDITOR_STATE = {
    "_cognition.ai/document/didOpen": True,
    "_cognition.ai/document/didFocus": False,
    "_cognition.ai/workspace/didChangeRevision": True,
}
DOCUMENT_CLOSED = "_cognition.ai/document/didClose"
EDITOR_CONTEXT = ("_cognition.ai/document/", "_cognition.ai/workspace/", "_cognition.ai/terminal/")
BACKGROUND_UPDATES = {
    "available_commands_update",
    "config_option_update",
    "current_mode_update",
    "session_info_update",
    "usage_update",
}
BUSY_ELSEWHERE = (
    "This chat is busy in another Devin window. Let its turn finish or stop it there, "
    "then open it here again."
)
STILL_CLOSING = (
    "This chat's previous agent is still shutting down after Devin reloaded its agents "
    "(it is never force-killed). Send your message again in a few seconds."
)
BLANK_RELEASED = (
    "This empty chat was closed after 15 idle minutes to free its agent. Nothing was sent "
    "to the model; start a new chat."
)


class RpcError(SwitchError):
    def __init__(self, message: str, code: int = -32603, data: object = None):
        super().__init__(message)
        self.code = code
        self.data = data


class CatalogUnavailable(SwitchError):
    pass


def quota_exhausted(error: RpcError) -> bool:
    # Rate limits share this kind but carry a retry delay; they are not exhausted quota.
    data = error.data if isinstance(error.data, dict) else {}
    return (
        error.code == RESOURCE_EXHAUSTED
        and data.get("cognition.ai/errorKind") == "resource_exhausted"
        and data.get("cognition.ai/retryAfterSeconds") is None
    )


def prompt_command(params: dict) -> tuple[list, list[str], str]:
    blocks = params.get("prompt", [])
    text = next(
        (block.get("text", "") for block in blocks if block.get("type") == "text"), ""
    ).strip()
    words = text.split()
    command = words[0] if words else ""
    if command == "!ds" and words[1:2] == ["switch"]:
        options = words[2:]
        command = "/switch-continue" if "--continue" in options else "/switch"
        words = [command, *(word for word in options if word != "--continue")]
    return blocks, words, command


def is_local_command(command: str) -> bool:
    return command in LOCAL_NAMES or (
        command.startswith("/") and command.lstrip("/") in BLOCKED_COMMANDS
    )


def has_project_mcp(folder: Path) -> bool:
    # In the home folder these are the user-level files every native loads anyway.
    if folder.resolve() == Path.home().resolve():
        return False
    return any((folder / name).is_file() for name in PROJECT_MCP_FILES)


def spare_fits(spare_folder: Path, project: Path) -> bool:
    """A native started in spare_folder gives a chat in project the right MCP servers."""
    return spare_folder == project or not (
        has_project_mcp(spare_folder) or has_project_mcp(project)
    )


def continue_params(session_id: str) -> dict:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": handoff.CONTINUE_PROMPT}]}


def failure_detail(error: Exception) -> str:
    return (
        str(error)
        if isinstance(error, SwitchError)
        else f"Operation failed ({type(error).__name__})."
    )


def is_activity(message: dict) -> bool:
    if "method" not in message or "id" in message:
        return True
    update = message.get("params", {}).get("update", {})
    return (
        message["method"] == "session/update"
        and update.get("sessionUpdate") not in BACKGROUND_UPDATES
    )


def registry(native: Native, account: str | None, sandbox: bool) -> dict:
    arguments = ([] if getattr(sys, "frozen", False) else ["-m", "devin_switch.cli"]) + ["acp"]
    if account:
        arguments += ["--account", account]
    if sandbox:
        arguments.append("--sandbox")
    system = "darwin" if sys.platform == "darwin" else "linux"
    architecture = "aarch64" if platform.machine() in ("arm64", "aarch64") else "x86_64"
    return {
        "version": "1.0.0",
        "agents": [
            {
                "id": "devin-switch",
                "name": "Devin Switch",
                "version": "0.3.0",
                "description": "Saved-account switching with shared local conversation history",
                "authors": ["Devin Switch contributors"],
                "license": "Apache-2.0",
                "distribution": {
                    "binary": {
                        f"{system}-{architecture}": {
                            "archive": "",
                            "cmd": sys.executable,
                            "args": arguments,
                            "env": {
                                "DS_HOME": str(native.store.root),
                                "DS_BINARY": str(native.binary),
                            },
                        }
                    }
                },
            }
        ],
        "extensions": [],
    }


class Backend:
    def __init__(
        self, bridge, account: Account, project: Path, session_id=None, *, chat=True, spare=False
    ):
        self.bridge = bridge
        self.spare = spare
        self.account = account
        self.project = project
        self.session_id = session_id
        self.chat = chat
        self.lease = None
        self.process = None
        self.reader = None
        self.writer = None
        self.read_transport = None
        self.commands = LOCAL_COMMANDS
        self.pump = None
        self.watcher = None
        self.pending = {}
        self.sequence = 0
        self.configs = {}
        self.mode = None
        self.model = None
        self.suppress_replay = False
        # Subagent events count only once the conversation is loaded, not during its replay.
        self.live = False
        self.running = set()
        # What Desktop was sent since the latest session/new or session/load: replaying it answers
        # a reload during a turn, which a native session/load would end. JSON text keeps it small.
        self.opened = {}
        self.transcript = []
        self.latest = {}
        self.emitting = asyncio.Lock()
        self.stopping = False
        self.stop_cause = None
        self.relay = None
        self.loop = None
        self.last_activity = time.monotonic()
        self.editor_ready = False
        bridge.backends.add(self)

    async def start(self):
        lease_task = asyncio.create_task(
            asyncio.to_thread(
                acp_state.Lease,
                self.bridge.native,
                self.account,
                self.project,
                self.session_id,
                # A warm spare is not a chat until it is attached to one.
                chat=self.chat and not self.spare,
                # The listing native needs no login, and Desktop's window waits for initialize.
                verify_login=self.chat,
            )
        )
        try:
            try:
                self.lease = await asyncio.shield(lease_task)
            except asyncio.CancelledError:
                self.lease = await lease_task
                raise
            environment = self.bridge.native.environment(self.account)
            environment.pop("DEVIN_MODEL", None)
            environment["DS_ACP_RUN_ID"] = self.lease.run.id
            self.process = subprocess.Popen(
                (str(self.bridge.native.binary), *self.bridge.flags, "acp"),
                env=environment,
                # The native serves the session's cwd from any folder, even a deleted one.
                cwd=self.project if self.project.is_dir() else Path.home(),
                pass_fds=self.lease.fds,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self.watcher = asyncio.create_task(self.watch())
            loop = asyncio.get_running_loop()
            self.reader = asyncio.StreamReader(limit=MAX_MESSAGE)
            self.read_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(self.reader),
                self.process.stdout,
            )
            transport, protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin,
                self.process.stdin,
            )
            self.writer = asyncio.StreamWriter(transport, protocol, None, loop)
            self.pump = asyncio.create_task(self.read())
            await asyncio.to_thread(
                sessions.record_process,
                self.bridge.native.store,
                self.lease.run.id,
                self.process.pid,
                role="native",
            )
            result = await self.request("initialize", self.bridge.initialization, LIFECYCLE_TIMEOUT)
            if result.get("protocolVersion") != 1 or not result.get("agentCapabilities", {}).get(
                "loadSession"
            ):
                raise SwitchError(
                    "This native CLI lacks compatible ACP session loading. Update Devin CLI."
                )
            # Written without awaiting, so no newer editor context can be sent before the replay.
            for method, params in self.bridge.editor_state.values():
                self.write({"method": method, "params": params})
            self.editor_ready = True
            await self.writer.drain()
            if self.chat and (issued := relay.login(self.bridge.native.store, self.account)):
                await self.authenticate(issued)
            return result
        except BaseException:
            await self.close()
            raise

    async def authenticate(self, issued):
        """Route the native's API traffic through a relay that can change logins in place."""
        self.loop = asyncio.get_running_loop()
        self.relay = relay.Relay(
            issued,
            lambda current, tried: self.bridge.rotate(self, issued, current, tried),
            self.bridge.native.store.root / "logs" / "relay.log",
        )
        try:
            await self.request(
                "authenticate",
                {
                    "methodId": "windsurf-api-key",
                    "_meta": {"api_key": issued.key, "api_server_url": self.relay.url},
                },
                LIFECYCLE_TIMEOUT,
            )
        except RpcError:
            # A failed host login (e.g. offline) leaves the native on its saved credentials.
            await asyncio.to_thread(self.close_relay)

    def close_relay(self):
        if self.relay:
            closing, self.relay = self.relay, None
            closing.close()

    def write(self, message):
        self.writer.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())

    async def send(self, message):
        if not self.process or self.process.poll() is not None or self.writer.is_closing():
            raise SwitchError(
                "The GUI agent has stopped. Reopen this saved conversation to reconnect."
            )
        self.last_activity = time.monotonic()
        self.write(message)
        await self.writer.drain()

    async def notify_editor(self, method, params):
        """Best effort and not activity, so editor context never keeps an idle chat open."""
        if not self.editor_ready or self.stopping or self.writer.is_closing():
            return
        if self.process.poll() is not None:
            return
        self.write({"method": method, "params": params})
        with suppress(OSError):
            await self.writer.drain()

    async def request(self, method, params, timeout=None):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        if method in OPENING:
            self.transcript.clear()
            self.latest.clear()
        try:
            await self.send({"id": identifier, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                error = response["error"]
                code = error.get("code", -32603) if isinstance(error, dict) else -32603
                code = code if type(code) is int else -32603
                raise RpcError(
                    f"Native ACP rejected the request (RPC code {code}). No prompt was replayed.",
                    code,
                    error.get("data") if isinstance(error, dict) else None,
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise SwitchError("Native ACP returned an invalid response.")
            self.remember(result)
            if method in OPENING:
                self.opened = copy.deepcopy(result)
            return result
        except TimeoutError as exc:
            raise SwitchError(
                "The GUI agent did not respond in time. No prompt was replayed."
            ) from exc
        finally:
            self.pending.pop(identifier, None)

    def remember_selection(self, category, value):
        setattr(self, category, value)
        for option in self.configs.values():
            if option.get("category") == category:
                option["currentValue"] = value

    def remember(self, result):
        if isinstance(result.get("configOptions"), list):
            self.configs = {
                option["id"]: copy.deepcopy(option)
                for option in result["configOptions"]
                if isinstance(option, dict) and "id" in option and "currentValue" in option
            }
            for option in self.configs.values():
                if option.get("category") in ("mode", "model"):
                    setattr(self, option["category"], option["currentValue"])
        if isinstance(result.get("modes"), dict):
            self.remember_selection("mode", result["modes"].get("currentModeId"))
        if isinstance(result.get("models"), dict):
            self.remember_selection("model", result["models"].get("currentModelId"))

    def record(self, message):
        update = message["params"]["update"]
        text = json.dumps(message)
        if update.get("sessionUpdate") in BACKGROUND_UPDATES:
            self.latest[update["sessionUpdate"]] = text
        else:
            self.transcript.append(text)

    def record_prompt(self, params):
        """The native echoes prompts only when it replays a load, so keep them for a replay."""
        for block in params.get("prompt", []):
            self.transcript.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": params["sessionId"],
                            "update": {"sessionUpdate": "user_message_chunk", "content": block},
                        },
                    }
                )
            )

    def reopened(self):
        """The load result with the settings as they are now."""
        result = copy.deepcopy(self.opened)
        result.pop("sessionId", None)
        if "configOptions" in result:
            result["configOptions"] = copy.deepcopy(list(self.configs.values()))
        for category in ("mode", "model"):
            if isinstance(listing := result.get(f"{category}s"), dict) and getattr(self, category):
                listing[f"current{category.title()}Id"] = getattr(self, category)
        return result

    async def read(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError
                if is_activity(message):
                    self.last_activity = time.monotonic()
                if "method" in message:
                    await self.bridge.from_backend(self, message)
                elif future := self.pending.get(message.get("id")):
                    if not future.done():
                        future.set_result(message)
        except (ValueError, TypeError, KeyError, OSError, SwitchError):
            pass
        finally:
            if self.writer:
                self.writer.close()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        SwitchError(
                            "The GUI agent connection closed. Reopen the saved chat to reconnect."
                        )
                    )

    def close_pipes(self):
        if self.writer:
            self.writer.close()
        elif self.process:
            self.process.stdin.close()
        if self.read_transport:
            self.read_transport.close()
        elif self.process:
            self.process.stdout.close()

    async def watch(self):
        while self.process.poll() is None:
            await asyncio.sleep(0.05)
        if self.pump:
            self.pump.cancel()
            with suppress(asyncio.CancelledError):
                await self.pump
        self.lease.close(ended=True)
        self.bridge.drop_client_requests(self)
        self.close_pipes()
        await asyncio.to_thread(self.close_relay)

    async def close(self):
        self.stopping = True
        if self.process:
            if self.writer:
                self.writer.close()
            else:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(asyncio.shield(self.watcher), SHUTDOWN_TIMEOUT)
            except TimeoutError:
                return False
        await asyncio.to_thread(self.close_relay)
        if self.lease:
            self.lease.close(ended=True)
        self.bridge.backends.discard(self)
        return True


@dataclass
class Chat:
    session_id: str
    setup: dict
    backend: Backend
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    controls: set[asyncio.Task] | None = None
    suspended: bool = False
    blank: bool = False
    canceled: bool = False
    next_idle_check: float = 0

    @asynccontextmanager
    async def access(self):
        async with self.gate:
            self.backend.last_activity = time.monotonic()
            try:
                yield
            finally:
                self.backend.last_activity = time.monotonic()


class Bridge:
    def __init__(self, native: Native, emit, *, account=None, sandbox=False):
        self.native = native
        self.emit = emit
        self.account = account
        self.flags = ("--sandbox",) if sandbox else ()
        self.initialization = None
        self.ready = False
        self.chats = {}
        self.backends = set()
        self.editor_state = {}
        self.client_requests = {}
        self.sequence = 0
        self.tasks = set()
        self.queued = set()
        self.releasing = set()
        self.opening = {}
        self.lister = None
        self.listing = asyncio.Lock()
        self.spare = None
        self.spare_folder = Path.home()
        self.filling = False

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def default_account(self):
        store = self.native.store
        return store.account(self.account) if self.account else store.selected()

    async def note(self, session_id, text):
        await self.emit(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text + "\n\n"},
                    },
                },
            }
        )

    def drop_client_requests(self, backend):
        for identifier, (owner, _) in list(self.client_requests.items()):
            if owner is backend:
                self.client_requests.pop(identifier)

    async def from_backend(self, backend, message):
        if "id" in message:
            self.sequence += 1
            identifier = f"ds-client-{self.sequence}"
            self.client_requests[identifier] = (backend, message["id"])
            await self.emit({**message, "id": identifier})
            return
        params = message.get("params")
        if (
            message.get("method") == AGENT_STOPPED
            and isinstance(params, dict)
            and params.get("sessionId") == backend.session_id
        ):
            backend.stop_cause = params.get("cause")
        if message.get("method") == "session/update":
            update = message.get("params", {}).get("update", {})
            kind = update.get("sessionUpdate")
            if kind == "config_option_update":
                backend.remember(update)
            if kind == "current_mode_update":
                backend.remember_selection("mode", update.get("currentModeId"))
            if kind == "available_commands_update":
                update["availableCommands"] = [
                    item
                    for item in update.get("availableCommands", [])
                    if "/" + str(item.get("name")) not in LOCAL_NAMES
                    and item.get("name") not in BLOCKED_COMMANDS
                ] + LOCAL_COMMANDS
                backend.commands = update["availableCommands"]
            meta = update.get("_meta")
            if backend.live and backend.session_id and isinstance(meta, dict):
                for key in (subagents.STARTED, subagents.COMPLETED):
                    if isinstance(event := meta.get(key), dict):
                        await self.track_subagent(backend, key, event)
            if backend.chat:
                backend.record(message)
            if backend.suppress_replay:
                return
        if not backend.stopping:
            async with backend.emitting:
                await self.emit(message)

    async def handle(self, message):
        identifier = message.get("id") if isinstance(message, dict) else None
        respond = not isinstance(message, dict) or "id" in message
        try:
            if identifier is not None and type(identifier) not in (int, str):
                identifier = None
                raise RpcError("Invalid ACP request ID.", -32600)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise RpcError("Invalid ACP message.", -32600)
            if "method" not in message:
                if owner := self.client_requests.pop(identifier, None):
                    backend, original = owner
                    await backend.send({**message, "id": original})
                return
            method = message["method"]
            params = message.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError("Invalid ACP parameters.", -32602)
            result = await self.dispatch(method, params, notification="id" not in message)
            if "id" in message:
                await self.emit({"jsonrpc": "2.0", "id": identifier, "result": result})
        except (SwitchError, OSError, ValueError, TypeError, KeyError) as exc:
            if respond:
                text = (
                    str(exc)
                    if isinstance(exc, SwitchError)
                    else "GUI bridge operation failed safely. Reopen the saved chat and retry."
                )
                await self.emit(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {
                            "code": getattr(exc, "code", -32603),
                            "message": text,
                        },
                    }
                )

    async def dispatch(self, method, params, *, notification=False):
        if (
            notification
            and method not in {"initialized", "session/cancel"}
            and not method.startswith("_")
        ):
            raise RpcError("This ACP method requires a request ID.", -32600)
        if method == "initialize":
            if self.initialization is not None:
                raise RpcError("ACP is already initialized.", -32600)
            self.initialization = copy.deepcopy(params)
            self.initialization["protocolVersion"] = 1
            probe = Backend(self, self.default_account(), Path.cwd(), chat=False)
            result = await probe.start()
            self.lister = probe
            result["authMethods"] = []
            result["agentInfo"] = {
                "name": "devin-switch",
                "title": "Devin Switch",
                "version": "0.3.0",
            }
            native_sessions = result["agentCapabilities"].get("sessionCapabilities", {})
            # Desktop then sends a chat's extra workspace folders, which load/new forward as-is.
            result["agentCapabilities"]["sessionCapabilities"] = {
                "list": {},
                "resume": {},
                "close": {},
                **{
                    key: native_sessions[key]
                    for key in ("additionalDirectories",)
                    if key in native_sessions
                },
            }
            self.ready = True
            self.spawn(self.refill())
            return result
        if not self.ready:
            raise RpcError("Initialize the GUI bridge first.", -32600)
        if method == "authenticate":
            raise SwitchError(
                "This agent uses saved ds logins, not the desktop login. "
                "Use ds login ALIAS in Terminal."
            )
        if method == "initialized":
            return {}
        if method == "session/list":
            return await self.list_sessions(params)
        if method in {"session/new", "session/load", "session/resume"}:
            return await self.open_session(method, params)
        if method.startswith("_") and "sessionId" not in params and not notification:
            return await self.ask_lister(method, params)
        if method.startswith(EDITOR_CONTEXT) and "sessionId" not in params and notification:
            # Desktop's editor and terminal context belongs to the window, so every native gets it.
            self.remember_editor(method, params)
            await asyncio.gather(
                *(backend.notify_editor(method, params) for backend in list(self.backends))
            )
            return {}
        session_id = params.get("sessionId")
        if session_id not in self.chats:
            raise RpcError("Load the exact saved conversation before using it.", -32602)
        chat = self.chats[session_id]
        if notification:
            if method == "session/cancel":
                chat.canceled = True
            if chat.suspended:
                if method == "session/cancel":
                    return {}
                async with chat.access():
                    if self.chats.get(session_id) is not chat:
                        return {}
                    await self.reconnect(chat)
                    await chat.backend.send({"method": method, "params": params})
            else:
                await chat.backend.send({"method": method, "params": params})
            return {}
        # The native advertises midTurnPrompts: steering joins the running turn, not a new one.
        steering = (
            method == "session/prompt"
            and not chat.canceled
            and not is_local_command(prompt_command(params)[2])
        )
        if (method in LIVE_CONTROLS or steering) and chat.controls is not None:
            if steering:
                chat.backend.record_prompt(params)
            task = self.spawn(self.forward_request(chat.backend, method, params))
            chat.controls.add(task)
            task.add_done_callback(chat.controls.discard)
            return await task
        if method == "session/fork":
            raise RpcError("Forking is not supported by this bridge.", -32601)
        if method == "session/close":
            path = acp_state.request_path(self.native.store, chat.backend.lease.run.id)
            with self.native.store.lock(timeout=5):
                if path.exists() and acp_state.read_request(path)["stage"] == "queued":
                    path.unlink()
            if chat.gate.locked() and not chat.suspended and not chat.backend.stopping:
                await chat.backend.send(
                    {"method": "session/cancel", "params": {"sessionId": session_id}}
                )
        async with chat.access():
            if self.chats.get(session_id) is not chat:
                raise SwitchError("This GUI chat was closed. Reopen the saved conversation.")
            if method == "session/close":
                if not await chat.backend.close():
                    raise SwitchError(
                        "The GUI agent is still shutting down; it was not force-killed."
                    )
                self.chats.pop(session_id)
                return {}
            if method == "session/prompt":
                chat.canceled = False
                if (result := await self.local_prompt(chat, params)) is not None:
                    return result
                await self.reconnect(chat)
                chat.blank = False
                return await self.prompt_turn(chat, params)
            if method == "session/cancel" and chat.suspended:
                return {}
            await self.reconnect(chat)
            return await self.forward_request(chat.backend, method, params)

    async def prompt_turn(self, chat, params):
        tried = {chat.backend.account.name}
        while True:
            backend = chat.backend
            backend.stop_cause = None
            controls = chat.controls = set()
            result = error = None
            backend.record_prompt(params)
            try:
                result = await self.forward_request(backend, "session/prompt", params)
            except RpcError as exc:
                error = exc
            finally:
                chat.controls = None
                await asyncio.gather(*controls, return_exceptions=True)
            exhausted = (
                quota_exhausted(error)
                if error
                else backend.stop_cause == "quota_exhausted"
                and result.get("stopReason") != "cancelled"
            )
            if (
                not exhausted
                or chat.canceled
                or len(tried) > AUTO_SWITCH_LIMIT
                or not await asyncio.to_thread(handoff.auto_switch_enabled, self.native.store)
            ):
                if error:
                    raise error
                return result
            if backend.relay:
                tried |= backend.relay.recently_exhausted()
            spent = ", ".join(sorted(tried)) or backend.account.name
            if backend.relay and backend.process.poll() is None:
                # Subagents live only in this process: continue in it on another login.
                try:
                    login = await asyncio.to_thread(backend.relay.recover, frozenset(tried))
                except (SwitchError, OSError):
                    login = None
                if login is not None:
                    backend.account = login.account
                    tried.add(login.account.name)
                    await self.note(
                        chat.session_id,
                        f"Out of usage on {spent}. Continuing on {login.account.name} in the "
                        "same agent, so subagents keep running. "
                        f"Sending “{handoff.CONTINUE_PROMPT}”…",
                    )
                    if chat.canceled:
                        return {"stopReason": "cancelled"}
                    params = await self.follow_up_params(chat)
                    continue
            await self.note(
                chat.session_id,
                f"Out of usage on {spent}. Auto-switch is on; restarting on another saved login…",
            )
            try:
                await self.switch(chat, None, exclude=frozenset(tried), follow_up=True)
            except (SwitchError, OSError) as exc:
                await self.note(
                    chat.session_id,
                    f"Automatic switch stopped: {failure_detail(exc)} "
                    "Turn it off with /auto-switch off.",
                )
                if error:
                    raise error from exc
                return result
            if chat.canceled:
                return {"stopReason": "cancelled"}
            tried.add(chat.backend.account.name)
            params = await self.follow_up_params(chat)

    async def forward_request(self, backend, method, params):
        result = await backend.request(method, params)
        if method in {"session/set_mode", "session/set_model"}:
            category = "mode" if method == "session/set_mode" else "model"
            backend.remember_selection(category, params[f"{category}Id"])
        return result

    def remember_editor(self, method, params):
        uri = params.get("uri")
        if method == DOCUMENT_CLOSED:
            self.editor_state.pop(("_cognition.ai/document/didOpen", uri), None)
        elif method in EDITOR_STATE:
            key = (method, uri if EDITOR_STATE[method] else None)
            # Re-inserting keeps the replay in the order Desktop last sent each piece.
            self.editor_state.pop(key, None)
            self.editor_state[key] = (method, params)

    async def list_sessions(self, params):
        """Let the native list shared history: Desktop needs its filters, paging and _meta."""
        return await self.ask_lister("session/list", params, LIFECYCLE_TIMEOUT)

    async def ask_lister(self, method, params, timeout=None):
        """Send a request that belongs to no chat (history, Customize's MCPs/skills/rules)."""
        async with self.listing:
            lister = self.lister
            if lister is None or lister.process.poll() is not None or lister.writer.is_closing():
                self.lister = None
                if lister:
                    await lister.close()
                lister = Backend(self, self.default_account(), Path.cwd(), chat=False)
                await lister.start()
                self.lister = lister
            lister.last_activity = time.monotonic()
        return await lister.request(method, params, timeout)

    async def close_lister(self):
        async with self.listing:
            lister = self.lister
            if (
                lister
                and not lister.pending
                and time.monotonic() - lister.last_activity >= LISTER_IDLE
            ):
                self.lister = None
                await lister.close()

    async def refill(self):
        """Start a logged-in agent for the default login, so the next chat skips startup.

        It runs in the latest chat's folder (first the window's): the native reads a project's
        own MCP servers from its process folder, so it only serves chats in that folder.
        """
        if self.filling or self.spare is not None:
            return
        self.filling = True
        try:
            backend = Backend(self, self.default_account(), self.spare_folder, spare=True)
            await backend.start()
            self.spare = backend
        except (SwitchError, OSError, RpcError):
            pass
        finally:
            self.filling = False

    async def drop_spare(self):
        spare, self.spare = self.spare, None
        if spare is not None:
            await spare.close()

    async def agent_for(self, account, project, session_id):
        """A started native for a chat: the warm spare when it has the same login."""
        if session_id:
            await self.claim(session_id)
        spare = self.spare
        if spare is not None and (
            spare.process.poll() is not None or spare.writer.is_closing() or spare.stopping
        ):
            await self.drop_spare()
            spare = None
        # The native reads project MCP servers from its process folder: a project with MCP config
        # needs a native started there, ordinary folders share one started in the home folder.
        self.spare_folder = project if has_project_mcp(project) else Path.home()
        usable = spare is not None and spare_fits(spare.project, project)
        if not usable or spare.account.name != account.name:
            if spare is not None and (
                spare.account.name != self.default_account().name
                or not spare_fits(spare.project, self.spare_folder)
            ):
                await self.drop_spare()
            self.spawn(self.refill())
            backend = Backend(self, account, project, session_id)
            await backend.start()
            return backend
        self.spare = None
        self.spawn(self.refill())
        try:
            if session_id:
                await asyncio.to_thread(spare.lease.attach, session_id, project, saved=True)
            spare.spare = False
            spare.project, spare.session_id = project, session_id
            spare.last_activity = time.monotonic()
        except BaseException:
            await spare.close()
            raise
        return spare

    async def track_subagent(self, backend, key, event):
        agent = event.get("agentId")
        if key == subagents.STARTED:
            backend.running.add(agent)
        else:
            backend.running.discard(agent)
        try:
            await asyncio.to_thread(
                subagents.record, self.native.store, backend.session_id, key, event
            )
        except (SwitchError, OSError):
            pass

    async def go_live(self, backend):
        """Start counting subagent events; ones an earlier process left running are now stopped."""
        backend.live = True
        try:
            await asyncio.to_thread(
                subagents.interrupt_running, self.native.store, backend.session_id
            )
        except (SwitchError, OSError):
            pass

    async def offer_resume(self, chat):
        try:
            pending = await asyncio.to_thread(
                subagents.interrupted, self.native.store, chat.session_id
            )
        except (SwitchError, OSError):
            return
        if pending:
            titles = ", ".join(info.get("title") or "untitled" for info in pending.values())
            await self.note(
                chat.session_id,
                f"{len(pending)} subagent(s) stopped before finishing ({titles}). Their "
                "transcripts are saved: send /resume-subagents to have the agent continue them "
                "from where they stopped.",
            )

    async def follow_up_params(self, chat, *, resume_only=False):
        """`continue`, plus a request to resume any subagents that stopped before finishing."""
        store = self.native.store
        params = continue_params(chat.session_id)
        try:
            pending = await asyncio.to_thread(subagents.interrupted, store, chat.session_id)
            if not pending:
                return None if resume_only else params
            text = await asyncio.to_thread(subagents.resume_text, store, chat.session_id, pending)
            await asyncio.to_thread(subagents.forget, store, chat.session_id, pending)
        except (SwitchError, OSError):
            return None if resume_only else params
        block = params["prompt"][0]
        block["text"] = text if resume_only else f"{block['text']}\n\n{text}"
        return params

    async def claim(self, session_id):
        """Take a saved conversation over from another window's idle GUI connection."""
        store = self.native.store
        own = {backend.lease.run.id for backend in self.backends if backend.lease}
        asked, holders, orphans = await asyncio.to_thread(
            acp_state.request_handover, store, session_id, own
        )
        waiting = holders + orphans
        # An orphan's stdin is already closed; it may still be finishing a tool call.
        deadline = time.monotonic() + (ORPHAN_TIMEOUT if orphans else HANDOVER_TIMEOUT)
        while waiting and await asyncio.to_thread(acp_state.held, store, session_id, waiting):
            if time.monotonic() >= deadline:
                await asyncio.to_thread(acp_state.cancel_handover, store, asked)
                if await asyncio.to_thread(acp_state.held, store, session_id, holders):
                    raise SwitchError(BUSY_ELSEWHERE)
                raise SwitchError(STILL_CLOSING)
            await asyncio.sleep(0.2)

    async def open_session(self, method, params):
        if method == "session/new":
            return await self.open_once(method, params)
        # Desktop retries a slow load; let the retry reuse the first load's agent.
        acp_state.session_key(params.get("sessionId"))
        async with self.opening.setdefault(params["sessionId"], asyncio.Lock()):
            return await self.open_once(method, params)

    async def open_once(self, method, params):
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise RpcError("Choose an absolute project directory.", -32602)
        project = Path(cwd).resolve()
        setup = copy.deepcopy(params)
        setup["cwd"] = str(project)
        session_id = None if method == "session/new" else params.get("sessionId")
        if method != "session/new":
            acp_state.session_key(session_id)
            if session_id in self.chats:
                chat = self.chats[session_id]
                if chat.controls is not None:
                    return await self.replay(chat, method)
                async with chat.access():
                    if self.chats.get(session_id) is not chat:
                        raise SwitchError(
                            "This GUI chat was closed. Reopen the saved conversation."
                        )
                    if chat.suspended:
                        return await self.reconnect(
                            chat, setup=setup, replay=method == "session/load"
                        )
                    if chat.backend.process.poll() is not None or chat.backend.writer.is_closing():
                        if not await chat.backend.close():
                            raise SwitchError(
                                "The previous agent is still shutting down. Retry after it exits."
                            )
                        self.chats.pop(session_id)
                    else:
                        chat.backend.suppress_replay = method == "session/resume"
                        chat.backend.live = False
                        try:
                            result = await chat.backend.request(
                                "session/load", setup, LIFECYCLE_TIMEOUT
                            )
                            chat.setup = setup
                            return result
                        finally:
                            chat.backend.suppress_replay = False
                            chat.backend.live = True
        account = (
            self.default_account()
            if self.account
            else (
                (acp_state.bound_account(self.native.store, session_id) if session_id else None)
                or self.default_account()
            )
        )
        backend = None
        try:
            backend = await self.agent_for(account, project, session_id)
            backend.suppress_replay = method == "session/resume"
            result = await backend.request(
                "session/new" if session_id is None else "session/load", setup, LIFECYCLE_TIMEOUT
            )
            session_id = session_id or result.get("sessionId")
            await asyncio.to_thread(backend.lease.attach, session_id, project)
            backend.session_id = session_id
            setup["sessionId"] = session_id
            with self.native.store.lock(timeout=5):
                acp_state.bind(self.native.store, session_id, account)
                acp_state.set_lifecycle(self.native.store, backend.lease.run.id, "open")
            backend.suppress_replay = False
            chat = self.chats[session_id] = Chat(
                session_id, setup, backend, blank=method == "session/new"
            )
            await self.go_live(backend)
            self.spawn(self.offer_resume(chat))
            return result
        except BaseException:
            if backend is not None:
                await backend.close()
            raise

    async def replay(self, chat, method):
        """Reopen a chat whose turn is running (Desktop reloads it when switching surfaces).

        A native session/load would end the turn, so replay what Desktop was already sent.
        """
        backend = chat.backend
        async with backend.emitting:
            if method == "session/load":
                for text in [*backend.transcript, *backend.latest.values()]:
                    await self.emit(json.loads(text))
            return backend.reopened()

    async def local_prompt(self, chat, params):
        blocks, words, command = prompt_command(params)
        done = {"stopReason": "end_turn"}
        if command.lstrip("/") in BLOCKED_COMMANDS and command.startswith("/"):
            await self.note(
                chat.session_id,
                "Manage logins in Terminal with ds login. "
                "Use /switch to change this chat's account.",
            )
            return done
        if command not in LOCAL_NAMES:
            return None
        limit = 2 if command in {"/switch", "/switch-continue", "/auto-switch"} else 1
        if (
            len(blocks) != 1
            or len(words) > limit
            or (command == "/auto-switch" and words[1:] not in ([], ["on"], ["off"]))
        ):
            await self.note(
                chat.session_id,
                "Send /switch [ALIAS], /switch-continue [ALIAS], /switch-status, "
                "/resume-subagents, or /auto-switch [on|off] alone, without attachments.",
            )
            return done
        store = self.native.store
        if command == "/auto-switch":
            if len(words) == 2:
                await asyncio.to_thread(handoff.set_auto_switch, store, words[1] == "on")
            await self.note(
                chat.session_id, await asyncio.to_thread(handoff.auto_switch_status, store)
            )
            return done
        if command == "/resume-subagents":
            await self.reconnect(chat)
            params = await self.follow_up_params(chat, resume_only=True)
            if params is None:
                await self.note(chat.session_id, "No subagents of this chat are waiting to resume.")
                return done
            chat.blank = False
            return await self.prompt_turn(chat, params)
        if command == "/switch-status":
            connected = chat.backend.process.poll() is None and not chat.backend.writer.is_closing()
            try:
                saved = await asyncio.to_thread(acp_state.saved_chat, store, chat.session_id)
                history = "saved" if saved else "not saved yet"
            except (SwitchError, OSError):
                history = "unavailable"
            connection = (
                "closed while empty (start a new chat)"
                if chat.suspended and chat.blank
                else "suspended (reconnects on the next message)"
                if chat.suspended
                else ("open" if connected else "closed")
            )
            automatic = await asyncio.to_thread(handoff.auto_switch_enabled, store)
            await self.note(
                chat.session_id,
                f"Account: {chat.backend.account.name}. Session: {chat.session_id}. "
                f"Connection: {connection}. "
                f"Shared history: {history}. Auto-switch: {'on' if automatic else 'off'}. "
                "Desktop login is unchanged.",
            )
            return done
        follow_up = command == "/switch-continue"
        try:
            await self.reconnect(chat)
            await self.switch(chat, words[1] if len(words) == 2 else None, follow_up=follow_up)
        except (SwitchError, OSError) as exc:
            await self.note(
                chat.session_id,
                str(exc)
                if isinstance(exc, SwitchError)
                else "GUI switch failed safely. No prompt was replayed.",
            )
            return done
        if not follow_up:
            await self.offer_resume(chat)
            return done
        if chat.canceled:
            return {"stopReason": "cancelled"}
        return await self.prompt_turn(chat, await self.follow_up_params(chat))

    async def publish_settings(self, chat):
        for update in (
            {
                "sessionUpdate": "config_option_update",
                "configOptions": list(chat.backend.configs.values()),
            },
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": chat.backend.commands,
            },
        ):
            await self.emit(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": chat.session_id,
                        "update": update,
                    },
                }
            )

    async def reconnect(self, chat, *, setup=None, replay=False):
        if not chat.suspended:
            return None
        if chat.blank:
            raise SwitchError(BLANK_RELEASED)
        old = chat.backend
        if old.process.poll() is None:
            raise SwitchError("The previous agent is still shutting down. Retry after it exits.")
        await old.close()
        if old.process.returncode != 0:
            raise SwitchError(
                "The previous agent exited abnormally. Close this tab and reopen the saved chat. "
                "No prompt was sent."
            )
        candidate, result = await self.restore(
            chat,
            old.account,
            copy.deepcopy(old.configs),
            old.mode,
            old.model,
            setup=setup,
            replay=replay,
        )
        chat.backend = candidate
        chat.suspended = False
        if setup is not None:
            chat.setup = setup
        await self.publish_settings(chat)
        await self.offer_resume(chat)
        return result

    async def restore(self, chat, account, settings, mode, model, *, setup=None, replay=False):
        for attempt in range(1, CATALOG_ATTEMPTS + 1):
            try:
                return await self.restore_once(
                    chat,
                    account,
                    settings,
                    mode,
                    model,
                    setup=setup,
                    replay=replay and attempt == 1,
                )
            except CatalogUnavailable:
                if attempt == CATALOG_ATTEMPTS:
                    raise
            await self.note(
                chat.session_id,
                f"{account.name} has not loaded its options yet (slow network?); "
                "retrying with a fresh agent…",
            )
            await asyncio.sleep(CATALOG_RETRY_DELAY * attempt)

    async def restore_once(self, chat, account, settings, mode, model, *, setup, replay):
        setup = chat.setup if setup is None else setup
        candidate = None
        stage = "starting the native agent"
        try:
            candidate = await self.agent_for(account, Path(setup["cwd"]), chat.session_id)
            candidate.suppress_replay = not replay
            stage = "loading the saved conversation"
            result = await candidate.request("session/load", setup, LIFECYCLE_TIMEOUT)
            stage = "restoring session settings"
            for identifier, option in settings.items():
                current = candidate.configs.get(identifier)
                if current is None:
                    raise SwitchError("The destination cannot preserve this chat's configuration.")
                if current["currentValue"] != option["currentValue"]:
                    try:
                        await candidate.request(
                            "session/set_config_option",
                            {
                                "sessionId": chat.session_id,
                                "configId": identifier,
                                "value": option["currentValue"],
                                **({"type": "boolean"} if option.get("type") == "boolean" else {}),
                            },
                            LIFECYCLE_TIMEOUT,
                        )
                    except RpcError as exc:
                        if (
                            exc.code == RESOURCE_NOT_FOUND
                            and current.get("type") == "select"
                            and not current.get("options")
                        ):
                            raise CatalogUnavailable(
                                f"{account.name} could not load its {identifier} list "
                                "(slow or unavailable network). No prompt was replayed."
                            ) from exc
                        raise
            if (
                mode
                and mode != candidate.mode
                and not any(option.get("category") == "mode" for option in settings.values())
            ):
                await candidate.request(
                    "session/set_mode",
                    {"sessionId": chat.session_id, "modeId": mode},
                    LIFECYCLE_TIMEOUT,
                )
            if (
                model
                and model != candidate.model
                and not any(option.get("category") == "model" for option in settings.values())
            ):
                await candidate.request(
                    "session/set_model",
                    {"sessionId": chat.session_id, "modelId": model},
                    LIFECYCLE_TIMEOUT,
                )
            if any(
                candidate.configs.get(identifier, {}).get("currentValue") != option["currentValue"]
                for identifier, option in settings.items()
            ):
                raise SwitchError("The destination did not preserve this chat's configuration.")
            if (mode is not None and candidate.mode != mode) or (
                model is not None and candidate.model != model
            ):
                raise SwitchError("The destination did not preserve this chat's mode or model.")
            if candidate.process.poll() is not None:
                raise SwitchError("The destination agent exited before the conversation was ready.")
            with self.native.store.lock(timeout=5):
                acp_state.set_lifecycle(self.native.store, candidate.lease.run.id, "open")
            result = {**result, "configOptions": list(candidate.configs.values())}
            if isinstance(result.get("modes"), dict):
                result["modes"]["currentModeId"] = candidate.mode
            if isinstance(result.get("models"), dict):
                result["models"]["currentModelId"] = candidate.model
            candidate.suppress_replay = False
            await self.go_live(candidate)
            return candidate, result
        except BaseException as exc:
            closed = await candidate.close() if candidate is not None else True
            if isinstance(exc, CatalogUnavailable) and closed:
                raise CatalogUnavailable(f"{stage}: {exc}") from exc
            if isinstance(exc, (SwitchError, OSError)):
                raise SwitchError(f"{stage}: {failure_detail(exc)}") from exc
            raise

    async def switch(self, chat, account_name, *, exclude=frozenset(), follow_up=False):
        old = chat.backend
        source = old.account
        store = self.native.store
        if old.process.poll() is not None or old.writer.is_closing():
            raise SwitchError(
                "This GUI connection is closed. Reopen the chat if it appears in shared "
                "history, or start a new one. No account was changed."
            )
        await asyncio.to_thread(acp_state.check_handoff, store, chat.session_id, old.lease.run.id)
        if account_name:
            account = store.account(account_name)
        else:
            await self.note(chat.session_id, "Checking saved accounts for remaining usage…")
            account, _ = await asyncio.to_thread(
                handoff.choose_best, self.native, source.name, exclude
            )
        # The relay may have moved this chat to another login meanwhile.
        if account.name in (source.name, old.account.name):
            raise SwitchError("This GUI chat already uses that account.")
        await asyncio.to_thread(acp_state.check_login, self.native, account)
        await asyncio.to_thread(acp_state.check_handoff, store, chat.session_id, old.lease.run.id)
        settings, mode, model = copy.deepcopy(old.configs), old.mode, old.model
        with store.lock(timeout=5):
            acp_state.check_switchable(store, old.lease.run.id)
            acp_state.set_lifecycle(store, old.lease.run.id, "closing")
        if not await old.close():
            raise SwitchError(
                "The current agent is still shutting down. No replacement was started or "
                "force-killed; reopen the saved chat after it exits."
            )
        if old.process.returncode != 0:
            raise SwitchError(
                "The previous agent exited abnormally. No replacement was started; "
                "reopen the saved conversation explicitly."
            )
        candidate = None
        try:
            candidate, _ = await self.restore(chat, account, settings, mode, model)
            with store.lock(timeout=5):
                acp_state.bind(store, chat.session_id, account)
                try:
                    store.select(account)
                except OSError:
                    acp_state.bind(store, chat.session_id, old.account)
                    raise
            chat.backend = candidate
        except (SwitchError, OSError) as exc:
            if candidate:
                await candidate.close()
            try:
                chat.backend, _ = await self.restore(chat, old.account, settings, mode, model)
            except (SwitchError, OSError) as recovery:
                raise SwitchError(
                    f"Switch to {account.name} failed ({failure_detail(exc)}). "
                    f"Recovery to {old.account.name} failed ({failure_detail(recovery)}). "
                    "Check shared history before reopening. No prompt was replayed; "
                    "the default is unchanged."
                ) from exc
            await self.publish_settings(chat)
            raise SwitchError(
                f"Switch to {account.name} failed ({failure_detail(exc)}); "
                "the previous account was restored. "
                "No prompt was replayed and the default is unchanged."
            ) from exc
        await self.publish_settings(chat)
        await self.note(
            chat.session_id,
            f"Switched {source.name} → {account.name}. Same conversation; "
            "desktop login and other chats are unchanged. "
            + (
                f"Sending “{handoff.CONTINUE_PROMPT}”…"
                if follow_up
                else "Send your next message when ready."
            ),
        )

    def rotate(self, backend, issued, current, tried):
        """Pick the relay's next login when every login in `tried` ran out (relay thread)."""
        store = self.native.store
        try:
            if not handoff.auto_switch_enabled(store):
                return None
            account, _ = handoff.choose_best(self.native, current.account.name, tried)
            replacement = relay.login(store, account)
            if replacement is None or not relay.compatible(issued, replacement):
                return None
            backend.lease.adopt(account)
            with store.lock(timeout=5):
                if backend.session_id:
                    acp_state.bind(store, backend.session_id, account)
                store.select(account)
        except (SwitchError, OSError):
            return None
        backend.loop.call_soon_threadsafe(setattr, backend, "account", account)
        return replacement

    async def queued_switch(self, chat, path):
        try:
            async with chat.access():
                with self.native.store.lock(timeout=5):
                    if not path.exists():
                        return
                    request = acp_state.read_request(path)
                    if (
                        request.get("session_id") != chat.session_id
                        or request.get("stage") != "queued"
                    ):
                        return
                    write_json(path, {**request, "stage": "switching"})
                try:
                    await self.switch(chat, request["account"])
                except (SwitchError, OSError) as exc:
                    await self.note(
                        chat.session_id,
                        str(exc)
                        if isinstance(exc, SwitchError)
                        else "GUI switch failed safely; no prompt was replayed.",
                    )
        except (SwitchError, OSError, ValueError, KeyError):
            await self.note(
                chat.session_id,
                "The queued GUI switch could not be applied. Check /switch-status and retry.",
            )
        finally:
            path.unlink(missing_ok=True)
            self.queued.discard(chat.session_id)

    async def release_idle(self, chat):
        manual = False
        store = self.native.store
        run_id = chat.backend.lease.run.id
        try:
            if chat.gate.locked() or chat.suspended or self.chats.get(chat.session_id) is not chat:
                return False
            async with chat.gate:
                backend = chat.backend
                if (
                    backend.stopping
                    or backend.process.poll() is not None
                    or backend.writer.is_closing()
                    or backend.pending
                    or chat.controls is not None
                    or any(owner is backend for owner, _ in self.client_requests.values())
                    or chat.session_id in self.queued
                    or backend.running
                ):
                    return False
                with store.lock(timeout=5):
                    state = acp_state.lifecycle(store, run_id)
                    manual = state == "close_requested"
                    if state not in ("open", "close_requested"):
                        return False
                    if not manual and time.monotonic() - backend.last_activity < IDLE_TIMEOUT:
                        return False
                    if acp_state.request_path(store, run_id).exists():
                        return False
                    if chat.blank and (manual or acp_state.saved_chat(store, chat.session_id)):
                        chat.blank = False
                    if not chat.blank:
                        saved = acp_state.check_saved(store, chat.session_id)
                        others = [run for run in sessions.runs(store) if run["id"] != run_id]
                        if blocker := sessions.resume_blocker(saved, others):
                            raise SwitchError(blocker)
                    acp_state.set_lifecycle(store, run_id, "closing")
                    chat.suspended = True
                return await backend.close()
        except (SwitchError, OSError):
            if manual and not chat.suspended:
                with store.lock(timeout=5):
                    acp_state.set_lifecycle(store, run_id, "open")
                await self.note(
                    chat.session_id,
                    "Close canceled: saved history or connection state is unavailable. "
                    "The current agent was left open. Retry after checking /switch-status.",
                )
            return False
        finally:
            chat.next_idle_check = time.monotonic() + IDLE_RECHECK
            self.releasing.discard(chat.session_id)

    async def poll(self):
        while True:
            if (
                self.lister
                and not self.listing.locked()
                and time.monotonic() - self.lister.last_activity >= LISTER_IDLE
            ):
                self.spawn(self.close_lister())
            if self.spare and time.monotonic() - self.spare.last_activity >= SPARE_IDLE:
                self.spawn(self.drop_spare())
            for chat in list(self.chats.values()):
                if chat.suspended:
                    continue
                run_id = chat.backend.lease.run.id
                path = acp_state.request_path(self.native.store, run_id)
                if path.exists() and chat.session_id not in self.queued:
                    self.queued.add(chat.session_id)
                    self.spawn(self.queued_switch(chat, path))
                elif not chat.gate.locked() and chat.session_id not in self.releasing:
                    try:
                        manual = acp_state.lifecycle(self.native.store, run_id) == "close_requested"
                    except (SwitchError, OSError):
                        continue
                    now = time.monotonic()
                    if manual or (
                        now >= chat.next_idle_check
                        and now - chat.backend.last_activity >= IDLE_TIMEOUT
                    ):
                        self.releasing.add(chat.session_id)
                        self.spawn(self.release_idle(chat))
            await asyncio.sleep(0.1)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(
            *(backend.close() for backend in list(self.backends)), return_exceptions=True
        )
        for backend in self.backends:
            if backend.lease:
                backend.lease.close(ended=not backend.process or backend.process.poll() is not None)
            backend.close_pipes()


async def serve(native: Native, *, account=None, sandbox=False):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    output_transport, output_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(output_transport, output_protocol, None, loop)

    async def emit(message):
        writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()

    bridge = Bridge(native, emit, account=account, sandbox=sandbox)
    poll = asyncio.create_task(bridge.poll())
    try:
        while line := await reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            bridge.spawn(bridge.handle(message))
    finally:
        poll.cancel()
        with suppress(asyncio.CancelledError):
            await poll
        await bridge.close()
        transport.close()
        writer.close()
