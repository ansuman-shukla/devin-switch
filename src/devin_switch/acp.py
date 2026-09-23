import asyncio
import copy
import json
import platform
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from devin_switch import acp_state, handoff, sessions
from devin_switch.native import Native
from devin_switch.store import Account, SwitchError, write_json

MAX_MESSAGE = 8 * 1024 * 1024
LIFECYCLE_TIMEOUT = 30
SHUTDOWN_TIMEOUT = 10
IDLE_TIMEOUT = 15 * 60
IDLE_RECHECK = 30
LOCAL_COMMANDS = [
    {
        "name": "switch",
        "description": "Resume this chat with another saved account; no model request",
        "input": {"hint": "optional account alias"},
    },
    {"name": "switch-status", "description": "Show this chat's saved account and exact session ID"},
]
BLOCKED_COMMANDS = {"login", "logout", "org", "new", "resume"}
LIVE_CONTROLS = {
    "session/set_config_option",
    "session/set_mode",
    "session/set_model",
    "_cognition.ai/command/revise",
}


class RpcError(SwitchError):
    def __init__(self, message: str, code: int = -32603):
        super().__init__(message)
        self.code = code


def failure_detail(error: Exception) -> str:
    return (
        str(error)
        if isinstance(error, SwitchError)
        else f"Operation failed ({type(error).__name__})."
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
    def __init__(self, bridge, account: Account, project: Path, session_id=None, *, chat=True):
        self.bridge = bridge
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
        self.stopping = False
        self.last_activity = time.monotonic()
        bridge.backends.add(self)

    async def start(self):
        lease_task = asyncio.create_task(
            asyncio.to_thread(
                acp_state.Lease,
                self.bridge.native,
                self.account,
                self.project,
                self.session_id,
                chat=self.chat,
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
                cwd=self.project,
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
            return result
        except BaseException:
            await self.close()
            raise

    async def send(self, message):
        if not self.process or self.process.poll() is not None or self.writer.is_closing():
            raise SwitchError(
                "The GUI agent has stopped. Reopen this saved conversation to reconnect."
            )
        self.last_activity = time.monotonic()
        self.writer.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        await self.writer.drain()

    async def request(self, method, params, timeout=None):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
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
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise SwitchError("Native ACP returned an invalid response.")
            self.remember(result)
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

    async def read(self):
        try:
            while line := await self.reader.readline():
                self.last_activity = time.monotonic()
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError
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
        self.client_requests = {}
        self.sequence = 0
        self.tasks = set()
        self.queued = set()
        self.releasing = set()

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
                    if item.get("name") not in BLOCKED_COMMANDS | {"switch", "switch-status"}
                ] + LOCAL_COMMANDS
                backend.commands = update["availableCommands"]
            if backend.suppress_replay:
                return
        if not backend.stopping:
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
            try:
                result = await probe.start()
            finally:
                await probe.close()
            result["authMethods"] = []
            result["agentInfo"] = {
                "name": "devin-switch",
                "title": "Devin Switch",
                "version": "0.3.0",
            }
            result["agentCapabilities"]["sessionCapabilities"] = {
                "list": {},
                "resume": {},
                "close": {},
            }
            self.ready = True
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
        session_id = params.get("sessionId")
        if session_id not in self.chats:
            raise RpcError("Load the exact saved conversation before using it.", -32602)
        chat = self.chats[session_id]
        if notification:
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
        if method in LIVE_CONTROLS and chat.controls is not None:
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
                if await self.local_prompt(chat, params):
                    return {"stopReason": "end_turn"}
                await self.reconnect(chat)
                controls = chat.controls = set()
                try:
                    return await self.forward_request(chat.backend, method, params)
                finally:
                    chat.controls = None
                    await asyncio.gather(*controls, return_exceptions=True)
            if method == "session/cancel" and chat.suspended:
                return {}
            await self.reconnect(chat)
            return await self.forward_request(chat.backend, method, params)

    async def forward_request(self, backend, method, params):
        result = await backend.request(method, params)
        if method in {"session/set_mode", "session/set_model"}:
            category = "mode" if method == "session/set_mode" else "model"
            backend.remember_selection(category, params[f"{category}Id"])
        return result

    async def list_sessions(self, params):
        history = await asyncio.to_thread(sessions.history, self.native.store)
        items = []
        for item in history:
            if params.get("cwd") and item["project"] != params["cwd"]:
                continue
            timestamp = item["last_activity_at"]
            if timestamp > 100_000_000_000:
                timestamp /= 1000
            items.append(
                {
                    "sessionId": item["id"],
                    "cwd": item["project"],
                    "title": item["title"] or "Untitled chat",
                    "updatedAt": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                }
            )
        return {"sessions": items}

    async def open_session(self, method, params):
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise RpcError("Choose an existing absolute project directory.", -32602)
        project = Path(cwd).resolve()
        setup = copy.deepcopy(params)
        setup["cwd"] = str(project)
        session_id = None if method == "session/new" else params.get("sessionId")
        if method != "session/new":
            acp_state.session_key(session_id)
            if session_id in self.chats:
                chat = self.chats[session_id]
                if chat.setup["cwd"] != str(project):
                    raise SwitchError("Resume this conversation from its original project.")
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
                        try:
                            result = await chat.backend.request(
                                "session/load", setup, LIFECYCLE_TIMEOUT
                            )
                            chat.setup = setup
                            return result
                        finally:
                            chat.backend.suppress_replay = False
        account = (
            self.default_account()
            if self.account
            else (
                (acp_state.bound_account(self.native.store, session_id) if session_id else None)
                or self.default_account()
            )
        )
        backend = Backend(self, account, project, session_id)
        backend.suppress_replay = method == "session/resume"
        try:
            await backend.start()
            result = await backend.request(
                "session/new" if session_id is None else "session/load", setup, LIFECYCLE_TIMEOUT
            )
            session_id = session_id or result.get("sessionId")
            await asyncio.to_thread(backend.lease.attach, session_id)
            backend.session_id = session_id
            setup["sessionId"] = session_id
            with self.native.store.lock(timeout=5):
                acp_state.bind(self.native.store, session_id, account)
                acp_state.set_lifecycle(self.native.store, backend.lease.run.id, "open")
            backend.suppress_replay = False
            self.chats[session_id] = Chat(session_id, setup, backend)
            return result
        except BaseException:
            await backend.close()
            raise

    async def local_prompt(self, chat, params):
        blocks = params.get("prompt", [])
        text = next(
            (block.get("text", "") for block in blocks if block.get("type") == "text"), ""
        ).strip()
        words = text.split()
        command = words[0] if words else ""
        if command.lstrip("/") in BLOCKED_COMMANDS and command.startswith("/"):
            await self.note(
                chat.session_id,
                "Manage logins in Terminal with ds login. "
                "Use /switch to change this chat's account.",
            )
            return True
        if command == "!ds" and words[1:2] == ["switch"]:
            words = ["/switch", *words[2:]]
            command = "/switch"
        if command not in {"/switch", "/switch-status"}:
            return False
        if len(blocks) != 1 or len(words) > (2 if command == "/switch" else 1):
            await self.note(
                chat.session_id,
                "Send /switch [ALIAS] or /switch-status alone, without attachments.",
            )
            return True
        if command == "/switch-status":
            connected = chat.backend.process.poll() is None and not chat.backend.writer.is_closing()
            try:
                saved = await asyncio.to_thread(
                    acp_state.saved_chat, self.native.store, chat.session_id
                )
                history = "saved" if saved else "not saved yet"
            except (SwitchError, OSError):
                history = "unavailable"
            connection = (
                "suspended (reconnects on the next message)"
                if chat.suspended
                else ("open" if connected else "closed")
            )
            await self.note(
                chat.session_id,
                f"Account: {chat.backend.account.name}. Session: {chat.session_id}. "
                f"Connection: {connection}. "
                f"Shared history: {history}. Desktop login is unchanged.",
            )
            return True
        try:
            await self.reconnect(chat)
            await self.switch(chat, words[1] if len(words) == 2 else None)
        except (SwitchError, OSError) as exc:
            await self.note(
                chat.session_id,
                str(exc)
                if isinstance(exc, SwitchError)
                else "GUI switch failed safely. No prompt was replayed.",
            )
        return True

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
        return result

    async def restore(self, chat, account, settings, mode, model, *, setup=None, replay=False):
        setup = chat.setup if setup is None else setup
        candidate = Backend(self, account, Path(setup["cwd"]), chat.session_id)
        candidate.suppress_replay = not replay
        stage = "starting the native agent"
        try:
            await candidate.start()
            stage = "loading the saved conversation"
            result = await candidate.request("session/load", setup, LIFECYCLE_TIMEOUT)
            stage = "restoring session settings"
            for identifier, option in settings.items():
                current = candidate.configs.get(identifier)
                if current is None:
                    raise SwitchError("The destination cannot preserve this chat's configuration.")
                if current["currentValue"] != option["currentValue"]:
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
            return candidate, result
        except BaseException as exc:
            await candidate.close()
            if isinstance(exc, (SwitchError, OSError)):
                raise SwitchError(f"{stage}: {failure_detail(exc)}") from exc
            raise

    async def switch(self, chat, account_name):
        old = chat.backend
        store = self.native.store
        if old.process.poll() is not None or old.writer.is_closing():
            raise SwitchError(
                "This GUI connection is closed. Reopen the chat if it appears in shared "
                "history, or start a new one. No account was changed."
            )
        await asyncio.to_thread(
            acp_state.check_handoff, store, chat.session_id, chat.setup["cwd"], old.lease.run.id
        )
        if account_name:
            account = store.account(account_name)
        else:
            await self.note(chat.session_id, "Checking saved accounts for remaining usage…")
            account, _ = await asyncio.to_thread(handoff.choose_best, self.native, old.account.name)
        if account.name == old.account.name:
            raise SwitchError("This GUI chat already uses that account.")
        await asyncio.to_thread(acp_state.check_login, self.native, account)
        await asyncio.to_thread(
            acp_state.check_handoff, store, chat.session_id, chat.setup["cwd"], old.lease.run.id
        )
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
            f"Switched {old.account.name} → {account.name}. Same conversation; "
            "desktop login and other chats are unchanged. Send your next message when ready.",
        )

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
                    saved = acp_state.check_saved(store, chat.session_id, chat.setup["cwd"])
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
