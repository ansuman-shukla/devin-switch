"""Side chats: questions answered from a GUI chat's context without entering that chat.

With `cognition.ai/chains` offered at initialize, the native runs a `session/prompt` whose
`_meta` says `cognition.ai/chain: "side"` as a read-only fork of the chat's main conversation,
even beside a running turn, and tags each notification of that answer the same way. Every side
prompt forks afresh, so a follow-up carries the earlier side exchange in its text.

Each bridge serves its Desktop window's side panel on a loopback port. The panel's `ds side
view` helper finds that port through a record naming the window's extension host, which
Desktop passes to the agents it starts as WINDSURF_EXT_HOST_PID.
"""

import asyncio
import json
import os
import secrets
import sys
from contextlib import suppress
from pathlib import Path

from devin_switch.store import Store, SwitchError, write_json

CHAIN = "cognition.ai/chain"
SIDE = {CHAIN: "side"}
CAPABILITY = "cognition.ai/chains"
HOST_VARIABLE = "WINDSURF_EXT_HOST_PID"
MAX_LINE = 8 * 1024 * 1024
RECONNECT_DELAY = 2.0
NO_BRIDGE = "No Switch chat is open in this window yet. Open a chat with the Devin Switch agent."
BRIDGE_GONE = "This window's Switch agent closed before answering. Ask again."


def is_side(message: dict) -> bool:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and meta.get(CHAIN) == "side"


def prompt_text(history: object, text: object) -> str:
    """The question, after the earlier turns of this side chat (the native forgets them)."""
    if not isinstance(text, str) or not text.strip():
        raise SwitchError("Type a side question first.")
    if not isinstance(history, list) or not all(
        isinstance(turn, dict)
        and isinstance(turn.get("user"), str)
        and isinstance(turn.get("assistant"), str)
        for turn in history
    ):
        raise SwitchError("Invalid side chat history.")
    if not history:
        return text
    turns = "\n\n".join(f"User: {turn['user']}\nYou: {turn['assistant']}" for turn in history)
    return f"<side_chat_history>\n{turns}\n</side_chat_history>\n\n{text}"


def records(store: Store) -> Path:
    return store.root / "side"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find(store: Store, host_pid: int) -> list[dict]:
    """Live bridges of one window, newest first; records of exited bridges are removed."""
    found = []
    for path in records(store).glob("*.json"):
        try:
            record = json.loads(path.read_text())
            pid, host = int(record["pid"]), int(record["host_pid"])
            int(record["port"]), str(record["token"])
            modified = path.stat().st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not alive(pid):
            path.unlink(missing_ok=True)
        elif host == host_pid:
            found.append((modified, record))
    return [record for _, record in sorted(found, key=lambda item: item[0], reverse=True)]


class Client:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.lock = asyncio.Lock()
        self.asking: dict[object, str] = {}

    async def send(self, message: dict) -> None:
        async with self.lock:
            if self.writer.is_closing():
                return
            self.writer.write((json.dumps(message) + "\n").encode())
            with suppress(OSError):
                await self.writer.drain()


class Server:
    """The side panel's door into this bridge: JSON lines over loopback, after a token."""

    def __init__(self, bridge, host_pid: int):
        self.bridge = bridge
        self.host_pid = host_pid
        self.token = secrets.token_hex(16)
        self.path = records(bridge.native.store) / f"{os.getpid()}.json"
        self.server = None
        self.clients: set[Client] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0, limit=MAX_LINE)
        port = self.server.sockets[0].getsockname()[1]
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # write_json's temporary file is private (0600) before it takes the record's name.
        record = {"pid": os.getpid(), "host_pid": self.host_pid, "port": port, "token": self.token}
        await asyncio.to_thread(write_json, self.path, record)

    async def close(self) -> None:
        self.path.unlink(missing_ok=True)
        if self.server:
            self.server.close()
        for client in list(self.clients):
            client.writer.close()

    async def push(self, event: dict) -> bool:
        """Send an event to every connected panel; False when none is listening."""
        clients = list(self.clients)
        for client in clients:
            await client.send(event)
        return bool(clients)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(writer)
        tasks = set()
        try:
            hello = json.loads(await reader.readline() or b"null")
            if not isinstance(hello, dict) or not secrets.compare_digest(
                str(hello.get("token", "")), self.token
            ):
                return
            self.clients.add(client)
            while line := await reader.readline():
                task = asyncio.create_task(self.handle(client, line))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (ValueError, OSError):
            pass
        finally:
            self.clients.discard(client)
            # A closed panel can't read its answers; stop them instead of paying for them.
            for session_id in set(client.asking.values()):
                with suppress(SwitchError, OSError):
                    await self.bridge.side_cancel(session_id)
            writer.close()

    async def handle(self, client: Client, line: bytes) -> None:
        request: dict = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError
            identifier, op = request.get("id"), request.get("op")
            if op == "chats":
                result = {"chats": await self.bridge.side_chats()}
            elif op == "ask":
                session_id = str(request.get("sessionId"))
                text = prompt_text(request.get("history", []), request.get("text"))

                async def forward(update):
                    await client.send({"id": identifier, "event": "update", "update": update})

                client.asking[identifier] = session_id
                try:
                    result = await self.bridge.side_ask(session_id, text, forward)
                finally:
                    client.asking.pop(identifier, None)
            elif op == "cancel":
                await self.bridge.side_cancel(str(request.get("sessionId")))
                result = {}
            else:
                raise SwitchError(f"Unknown side request {op!r}.")
            await client.send({"id": identifier, "ok": True, **result})
        except (SwitchError, OSError, ValueError, TypeError, KeyError) as exc:
            text = str(exc) if isinstance(exc, SwitchError) else "Invalid side request."
            await client.send({"id": request.get("id"), "ok": False, "error": text})


async def view(store: Store, host_pid: int) -> None:
    """Serve one Desktop window's side panel over stdio, through that window's bridge.

    Stays connected while the window has a Switch bridge, so pushed events (a /btw typed in
    a chat) reach the panel; reconnects when the bridge is replaced.
    """
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=MAX_LINE)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    stdout = asyncio.StreamWriter(transport, protocol, None, loop)
    bridge: asyncio.StreamWriter | None = None
    pending: set = set()
    pumps: set[asyncio.Task] = set()
    connecting = asyncio.Lock()

    async def emit(message: dict) -> None:
        stdout.write((json.dumps(message) + "\n").encode())
        await stdout.drain()

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal bridge
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if "ok" in message:
                    pending.discard(message.get("id"))
                await emit(message)
        except (ValueError, OSError):
            pass
        finally:
            if bridge is writer:
                bridge = None
                for identifier in list(pending):
                    pending.discard(identifier)
                    await emit({"id": identifier, "ok": False, "error": BRIDGE_GONE})
            writer.close()

    async def connect() -> asyncio.StreamWriter | None:
        nonlocal bridge
        async with connecting:
            if bridge is not None:
                return bridge
            for record in await asyncio.to_thread(find, store, host_pid):
                try:
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", record["port"], limit=MAX_LINE
                    )
                    writer.write((json.dumps({"token": record["token"]}) + "\n").encode())
                    await writer.drain()
                except OSError:
                    continue
                bridge = writer
                task = asyncio.create_task(pump(reader, writer))
                pumps.add(task)
                task.add_done_callback(pumps.discard)
                return bridge
            return None

    async def keep_connected() -> None:
        while True:
            await connect()
            await asyncio.sleep(RECONNECT_DELAY)

    keeper = asyncio.create_task(keep_connected())
    try:
        while line := await stdin.readline():
            try:
                request = json.loads(line)
                identifier = request.get("id") if isinstance(request, dict) else None
            except ValueError:
                continue
            writer = await connect()
            if writer is None:
                await emit({"id": identifier, "ok": False, "error": NO_BRIDGE})
                continue
            pending.add(identifier)
            writer.write(line if line.endswith(b"\n") else line + b"\n")
            with suppress(OSError):
                await writer.drain()
    finally:
        keeper.cancel()
        if bridge is not None:
            bridge.close()
