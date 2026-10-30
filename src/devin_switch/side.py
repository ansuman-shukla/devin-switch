"""Side chats: questions answered from a GUI chat's context without entering that chat.

With `cognition.ai/chains` offered at initialize, the native runs a `session/prompt` whose
`_meta` says `cognition.ai/chain: "side"` as a read-only fork of the chat's main conversation,
even beside a running turn, and tags each notification of that answer the same way. Every side
prompt forks afresh, so a follow-up carries the earlier side exchange in its text.

The bridge serves each chat's side page on loopback behind a random token. A `/btw` typed in
the chat opens it inside Desktop's agents view, in the chat's own preview tab: the bridge emits
the `browser_preview` tool call Desktop turns into a named preview of a local port. An editor
panel would not do: Desktop switches the agents view to IDE mode for any editor tab.
"""

import asyncio
import html
import json
import re
import secrets
import uuid
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from devin_switch.store import SwitchError

CHAIN = "cognition.ai/chain"
SIDE = {CHAIN: "side"}
CAPABILITY = "cognition.ai/chains"
PREVIEW_NAME = "Side chat"
PAGE = Path(__file__).parent / "side_page"
ASSETS = {
    "side.css": "text/css",
    "side.js": "text/javascript",
    "side-thread.js": "text/javascript",
    "markdown.js": "text/javascript",
}
MAX_BODY = 1024 * 1024
KEEPALIVE = 15
QUOTE = re.compile(r"<quoted_artifact\b[^>]*>(.*?)</quoted_artifact>", re.S)
CAPTION = re.compile(r"<caption>(.*?)</caption>", re.S)
CONTENT = re.compile(r"<content>(.*?)</content>", re.S)


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


def is_btw(text: str) -> bool:
    return text == "/btw" or text.startswith(("/btw ", "/btw\n"))


def btw_question(text: str) -> str | None:
    """The side question of a prompt starting with /btw, typed alone or as a quote's comment.

    Desktop sends a quoted chat message as a `<quoted_artifact>` whose `<caption>` holds what
    was typed with it; the quoted text goes along as a Markdown quote. None: not a /btw.
    """
    rest, quoted, comments = text.strip(), [], []
    while found := QUOTE.match(rest):
        caption, content = CAPTION.search(found.group(1)), CONTENT.search(found.group(1))
        comments.append(html.unescape(caption.group(1)).strip() if caption else "")
        if content and (lines := html.unescape(content.group(1)).strip()):
            quoted.append("\n".join(f"> {line}" for line in lines.splitlines()))
        rest = rest[found.end() :].strip()
    asked = next((part for part in (rest, *comments) if is_btw(part)), None)
    if asked is None:
        return None
    return "\n\n".join([*quoted, asked.removeprefix("/btw").strip()]).strip()


class Server:
    """Each chat's side page, for Desktop's in-chat preview: loopback HTTP behind a token."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.token = secrets.token_urlsafe(24)
        self.port = None
        self.servers = []
        self.listeners: dict[str, set[asyncio.Queue]] = {}
        self.waiting: dict[str, list[str]] = {}

    async def start(self) -> None:
        server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        self.servers = [server]
        # The preview shows localhost:<port>, which may resolve to ::1 first.
        with suppress(OSError):
            self.servers.append(await asyncio.start_server(self.serve, "::1", self.port))

    async def close(self) -> None:
        for server in self.servers:
            server.close()
        for queues in self.listeners.values():
            for queue in queues:
                queue.put_nowait(None)

    def path(self, session_id: str) -> str:
        return f"/side/{quote(session_id, safe='')}?t={self.token}"

    def preview(self, session_id: str) -> dict:
        """The tool call Desktop shows as a "Side chat" preview of the chat's side page."""
        path = self.path(session_id)
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": f"switch-side-{uuid.uuid4().hex}",
            "title": PREVIEW_NAME,
            "kind": "other",
            "status": "completed",
            "_meta": {
                "cognition.ai/eventType": "browser_preview",
                "cognition.ai/name": PREVIEW_NAME,
                "cognition.ai/ports": [self.port],
                "cognition.ai/path": path,
                "cognition.ai/url": f"http://127.0.0.1:{self.port}{path}",
            },
        }

    def showing(self, session_id: str) -> bool:
        return bool(self.listeners.get(session_id))

    def ask(self, session_id: str, text: str) -> None:
        """Hand a /btw question to the chat's open side page, or keep it until one opens."""
        if queues := self.listeners.get(session_id):
            for queue in queues:
                queue.put_nowait(text)
        else:
            self.waiting.setdefault(session_id, []).append(text)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, target, headers, body = await read_request(reader)
            url = urlsplit(target)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            allowed = {f"{host}:{self.port}" for host in ("127.0.0.1", "localhost", "[::1]")}
            if headers.get("host") not in allowed or not secrets.compare_digest(
                query.get("t", ""), self.token
            ):
                return await respond(writer, 403, "Forbidden")
            route = url.path
            if method == "GET" and route.startswith("/side/assets/"):
                name = route.removeprefix("/side/assets/")
                if name not in ASSETS:
                    return await respond(writer, 404, "Not found")
                return await respond(writer, 200, (PAGE / name).read_bytes(), ASSETS[name])
            if method == "GET" and route == "/side/events":
                return await self.events(writer, query.get("session", ""))
            if method == "POST" and route == "/side/ask":
                return await self.answer(reader, writer, json.loads(body))
            if method == "POST" and route == "/side/cancel":
                await self.bridge.side_cancel(str(json.loads(body).get("sessionId")))
                return await respond(writer, 200, "{}", "application/json")
            if method == "GET" and route.startswith("/side/") and route.count("/") == 2:
                return await respond(writer, 200, self.page(unquote(route[6:])), "text/html")
            await respond(writer, 404, "Not found")
        except (ValueError, AttributeError, OSError, asyncio.IncompleteReadError):
            with suppress(OSError):
                await respond(writer, 400, "Bad request")
        finally:
            writer.close()

    def page(self, session_id: str) -> str:
        token = html.escape(self.token)
        assets = lambda name: f"/side/assets/{name}?t={token}"  # noqa: E731
        scripts = "".join(
            f'<script src="{assets(name)}"></script>'
            for name in ("side-thread.js", "markdown.js", "side.js")
        )
        return (
            '<!doctype html><html><head><meta charset="utf-8"><title>Side chat</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<link rel="stylesheet" href="{assets("side.css")}"></head>'
            f'<body data-session="{html.escape(session_id)}" data-token="{token}">'
            f"{scripts}</body></html>"
        )

    async def events(self, writer: asyncio.StreamWriter, session_id: str) -> None:
        """Server-sent /btw questions for one chat's side page."""
        queue: asyncio.Queue = asyncio.Queue()
        for text in self.waiting.pop(session_id, []):
            queue.put_nowait(text)
        self.listeners.setdefault(session_id, set()).add(queue)
        try:
            writer.write(head(200, "text/event-stream"))
            await writer.drain()
            while True:
                try:
                    text = await asyncio.wait_for(queue.get(), KEEPALIVE)
                except TimeoutError:
                    writer.write(b": keepalive\n\n")
                else:
                    if text is None:
                        return
                    writer.write(f"data: {json.dumps({'text': text})}\n\n".encode())
                await writer.drain()
        except OSError:
            pass
        finally:
            self.listeners[session_id].discard(queue)
            if not self.listeners[session_id]:
                del self.listeners[session_id]

    async def answer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: dict
    ) -> None:
        """Stream one side answer as JSON lines; a page that goes away stops the answer."""
        session_id = str(request.get("sessionId"))

        async def send(message: dict) -> None:
            if writer.is_closing():
                return
            with suppress(OSError):
                writer.write((json.dumps(message) + "\n").encode())
                await writer.drain()

        async def watch() -> None:
            # The page sends nothing more, so end of input means it closed or reloaded.
            with suppress(OSError):
                await reader.read()
            writer.close()
            await self.bridge.side_cancel(session_id)

        writer.write(head(200, "application/x-ndjson"))
        watcher = asyncio.create_task(watch())
        try:
            text = prompt_text(request.get("history", []), request.get("text"))
            result = await self.bridge.side_ask(
                session_id, text, lambda update: send({"update": update})
            )
            await send({"done": result["stopReason"]})
        except SwitchError as exc:
            await send({"error": str(exc)})
        finally:
            watcher.cancel()


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict, bytes]:
    method, target, _ = (await reader.readline()).decode("latin-1").split(" ", 2)
    headers = {}
    while (line := (await reader.readline()).decode("latin-1").strip()) != "":
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if not 0 <= length <= MAX_BODY:
        raise ValueError("body too large")
    return method, target, headers, await reader.readexactly(length) if length else b""


def head(status: int, content_type: str) -> bytes:
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}[status]
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}; charset=utf-8\r\n"
        "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
        "Content-Security-Policy: default-src 'self'\r\n"
        "Connection: close\r\n\r\n"
    ).encode()


async def respond(
    writer: asyncio.StreamWriter, status: int, body: str | bytes, kind: str = "text/plain"
) -> None:
    writer.write(head(status, kind) + (body.encode() if isinstance(body, str) else body))
    await writer.drain()
