"""Loopback relay between one GUI chat's native agent and its API server.

The native binds credentials to a session and its subagents when the session starts, never
retries a quota error, and keeps subagents only in memory, so restarting it to change logins
loses every subagent. Instead the native is authenticated with a key Switch issues and an API
server URL pointing here. Requests are forwarded with the active login's key; when the first
Connect frame of a response reports exhausted usage, the same request is replayed with the next
saved login, so the native, its subagents and the parent agent never see the error. Anything
unexpected is passed through unchanged, which leaves the restart-based auto-switch as fallback.
"""

import gzip
import http.client
import json
import re
import ssl
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from devin_switch.store import Account, Store

ROTATION_LIMIT = 3
EXHAUSTED_MEMORY = 600
LOG_LIMIT = 1_000_000
UPSTREAM_TIMEOUT = 300
CHUNK = 64 * 1024
END_STREAM = 0x02
COMPRESSED = 0x01
QUOTA_MESSAGE = re.compile(r"\b(?:daily|weekly) usage quota has been exhausted")
HOP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True)
class Login:
    account: Account
    key: str
    server: str


def login(store: Store, account: Account) -> Login | None:
    """The saved API login the relay can present for an account, if it has one."""
    path = store.credentials(account)
    try:
        if path.is_symlink():
            return None
        values = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    key, server = values.get("windsurf_api_key"), values.get("api_server_url")
    if not isinstance(key, str) or not key or not isinstance(server, str):
        return None
    parts = urlsplit(server)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.query or parts.fragment:
        return None
    return Login(account, key, server.rstrip("/"))


def compatible(issued: Login, replacement: Login) -> bool:
    # A same-length key keeps the protobuf length prefixes around it valid.
    return replacement.server == issued.server and len(replacement.key) == len(issued.key)


def quota_frame(head: bytes, encoding: str | None) -> bool:
    """Whether a Connect stream starts with an end-of-stream error for exhausted usage."""
    if len(head) < 5 or not head[0] & END_STREAM:
        return False
    size = int.from_bytes(head[1:5], "big")
    payload = head[5 : 5 + size]
    if len(payload) != size:
        return False
    if head[0] & COMPRESSED:
        if encoding != "gzip":
            return False
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError):
            return False
    try:
        error = json.loads(payload).get("error")
    except (ValueError, AttributeError):
        return False
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and QUOTA_MESSAGE.search(message) is not None


def first_frame(response: http.client.HTTPResponse) -> bytes:
    """Read just enough of a Connect stream to classify its first frame."""
    head = b""
    while len(head) < 5 and (chunk := response.read1(CHUNK)):
        head += chunk
    if len(head) >= 5 and head[0] & END_STREAM:
        size = 5 + int.from_bytes(head[1:5], "big")
        while len(head) < min(size, CHUNK) and (chunk := response.read1(CHUNK)):
            head += chunk
    return head


class Relay:
    def __init__(
        self,
        issued: Login,
        rotate: Callable[[Login, frozenset[str]], Login | None],
        log: Path | None = None,
    ):
        self.issued = issued
        self.active = issued
        self.rotate = rotate
        self.log = log
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.exhausted: dict[str, float] = {}
        self.server = Server(self)
        threading.Thread(target=self.server.serve_forever, name="ds-relay", daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def record(self, event: str) -> None:
        """Append a key-free line to the relay log: endpoint names, statuses and aliases only."""
        if self.log is None:
            return
        line = f"{datetime.now().isoformat(timespec='milliseconds')} {event}\n"
        with self.log_lock:
            try:
                self.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.log.exists() and self.log.stat().st_size > LOG_LIMIT:
                    self.log.replace(self.log.with_suffix(".log.1"))
                with open(self.log, "a") as handle:
                    handle.write(line)
            except OSError:
                pass

    def mark_exhausted(self, name: str) -> None:
        with self.lock:
            self.exhausted[name] = time.monotonic()

    def recently_exhausted(self) -> frozenset[str]:
        now = time.monotonic()
        return frozenset(
            name for name, seen in self.exhausted.items() if now - seen < EXHAUSTED_MEMORY
        )

    def recover(self, tried: frozenset[str]) -> Login | None:
        """After a quota error reached the native: a login to continue on in the same process.

        Keeps the active login when the relay already moved past every exhausted one, and
        otherwise rotates to the next login with credits.
        """
        with self.lock:
            tried = tried | self.recently_exhausted()
            if self.active.account.name not in tried:
                return self.active
            replacement = self.rotate(self.active, tried)
            if replacement is None or not compatible(self.issued, replacement):
                self.record(f"recover failed tried={','.join(sorted(tried))}")
                return None
            self.record(f"recover {self.active.account.name} -> {replacement.account.name}")
            self.active = replacement
            return replacement

    def advance(self, tried: frozenset[str]) -> Login | None:
        """The login to replay with after every login in `tried` reported exhausted usage."""
        with self.lock:
            current = self.active
            if current.account.name not in tried:
                return current
            if len(tried) > ROTATION_LIMIT:
                self.record(f"rotation limit reached tried={','.join(sorted(tried))}")
                return None
            started = time.monotonic()
            replacement = self.rotate(current, tried)
            if replacement is None or not compatible(self.issued, replacement):
                self.record(f"rotate from {current.account.name} failed")
                return None
            self.record(
                f"rotate {current.account.name} -> {replacement.account.name} "
                f"in {time.monotonic() - started:.1f}s"
            )
            self.active = replacement
            return replacement

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def endpoint(path: str) -> str:
    """The RPC method name of a request path, without query strings."""
    return path.split("?", 1)[0].rsplit("/", 1)[-1][:80]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, relay: Relay):
        self.relay = relay
        super().__init__(("127.0.0.1", 0), Handler)

    def handle_error(self, request, client_address):
        # The native routinely drops connections; anything else still reports a traceback.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Server

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        relay = self.server.relay
        body = self.read_body()
        if body is None:
            return self.refuse(400)
        # Only the native knows the issued key; some calls (analytics) carry it only in the body.
        if (
            relay.issued.key not in self.headers.get("Authorization", "")
            and relay.issued.key.encode() not in body
        ):
            return self.refuse(403)
        tried = set()
        login = relay.active
        while True:
            tried.add(login.account.name)
            try:
                connection, response = self.forward(login, body)
            except OSError:
                return self.refuse(502)
            streaming = (response.getheader("Content-Type") or "").startswith("application/connect")
            head = first_frame(response) if streaming else b""
            if head and quota_frame(head, response.getheader("Connect-Content-Encoding")):
                relay.mark_exhausted(login.account.name)
                relay.record(f"quota {login.account.name} {endpoint(self.path)}")
                replacement = relay.advance(frozenset(tried))
                if replacement is not None:
                    connection.close()
                    login = replacement
                    continue
                relay.record(f"quota passed to the agent {endpoint(self.path)}")
            elif response.status >= 400:
                relay.record(f"status {response.status} {login.account.name} {endpoint(self.path)}")
            break
        try:
            self.pass_through(response, head)
        finally:
            connection.close()

    do_GET = do_POST

    def read_body(self) -> bytes | None:
        try:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                parts = []
                while size := int(self.rfile.readline().split(b";")[0], 16):
                    parts.append(self.rfile.read(size))
                    self.rfile.readline()
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(parts)
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return None

    def forward(self, login: Login, body: bytes):
        issued = self.server.relay.issued
        parts = urlsplit(login.server)
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                parts.hostname,
                parts.port,
                timeout=UPSTREAM_TIMEOUT,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parts.hostname, parts.port, timeout=UPSTREAM_TIMEOUT
            )
        headers = {
            name: value.replace(issued.key, login.key)
            for name, value in self.headers.items()
            if name.lower() not in HOP_HEADERS
        }
        headers["Host"] = parts.netloc
        try:
            connection.request(
                self.command,
                parts.path + self.path,
                body=body.replace(issued.key.encode(), login.key.encode()),
                headers=headers,
            )
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    def pass_through(self, response: http.client.HTTPResponse, head: bytes) -> None:
        self.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() not in HOP_HEADERS:
                self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = head or response.read1(CHUNK)
        while chunk:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
            chunk = response.read1(CHUNK)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def refuse(self, status: int) -> None:
        self.send_response_only(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
