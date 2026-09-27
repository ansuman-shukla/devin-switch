import errno
import fcntl
import json
import os
import pty
import queue
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field

from devin_switch import handoff
from devin_switch.store import Store, SwitchError


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@dataclass
class ExitRequest:
    store: Store
    run_id: str
    timeout: float = 10
    request: dict | None = None
    started: float | None = None
    next_key: float = 0
    escaped: bool = False

    def read(self) -> dict | None:
        try:
            with handoff.state_path(self.store, self.run_id, "request").open() as handle:
                value = json.loads(handle.read(65536))
            if (
                isinstance(value, dict)
                and type(value.get("requester_pid")) is int
                and value["requester_pid"] > 1
            ):
                return value
        except (OSError, ValueError):
            pass
        return None

    def advance(self, now: float) -> tuple[bytes, str]:
        request = self.read()
        if request != self.request:
            self.request, self.started = request, now if request else None
            self.next_key, self.escaped = now, False
        if request is None or handoff.state_path(self.store, self.run_id, "exit").is_file():
            return b"", ""
        if now - self.started >= self.timeout:
            try:
                with self.store.lock():
                    if handoff.state_path(self.store, self.run_id, "exit").is_file():
                        return b"", ""
                    if self.read() == request:
                        handoff.cancel(self.store, {"id": self.run_id})
            except SwitchError:
                return b"", ""
            self.request = None
            return b"", "Switch canceled: the CLI did not exit normally. This chat is still open."
        if (
            request.get("automatic") is not True and process_exists(request["requester_pid"])
        ) or now < self.next_key:
            return b"", ""
        if not self.escaped:
            self.escaped, self.next_key = True, now + 0.2
            return b"\x1b", ""
        self.next_key = now + 0.5
        return b"\x04", ""


QUOTA_TEXT = re.compile(rb"quota\s*exhausted|usage\s*limit\s*reached", re.IGNORECASE)
TERMINAL_ESCAPE = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


@dataclass
class QuotaWatch:
    """Notice the CLI's quota alert; the handoff itself requires fresh usage confirmation."""

    native: object
    account: object
    cooldown: float = 60
    tail: bytes = b""
    next_check: float = 0
    worker: threading.Thread | None = None
    messages: queue.SimpleQueue = field(default_factory=queue.SimpleQueue)

    def feed(self, output: bytes, now: float) -> None:
        self.tail = TERMINAL_ESCAPE.sub(b"", self.tail + output)[-512:]
        if (
            now < self.next_check
            or (self.worker is not None and self.worker.is_alive())
            or not QUOTA_TEXT.search(self.tail)
        ):
            return
        self.tail, self.next_check = b"", now + self.cooldown
        self.worker = threading.Thread(target=self.check, daemon=True)
        self.worker.start()

    def check(self) -> None:
        try:
            message = handoff.queue_automatic(self.native, self.account)
        except (SwitchError, OSError):
            message = "Automatic switching could not check usage. This chat is unchanged."
        if message:
            self.messages.put(message)

    def message(self) -> str:
        try:
            return self.messages.get_nowait()
        except queue.Empty:
            return ""


def write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        remaining = remaining[written:]


def claim_terminal() -> None:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def run(native, account, arguments: tuple[str, ...]) -> int:
    input_fd, output_fd = sys.stdin.fileno(), sys.stdout.fileno()
    settings = termios.tcgetattr(input_fd)
    master, slave = pty.openpty()
    process = None
    previous_resize = signal.getsignal(signal.SIGWINCH)

    def resize(*_) -> None:
        size = fcntl.ioctl(input_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)

    try:
        resize()
        with native.store.lock(f"controller-{native.run_id}.lock"):
            with native.launch_selection(account):
                process = subprocess.Popen(
                    (str(native.binary), *arguments),
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=native.environment(account),
                    pass_fds=native.lock_fds,
                    start_new_session=True,
                    preexec_fn=claim_terminal,
                )
                native.track_process(process.pid)
            os.close(slave)
            slave = -1
            signal.signal(signal.SIGWINCH, resize)
            tty.setraw(input_fd)
            request = ExitRequest(native.store, native.run_id)
            watch = QuotaWatch(native, account)
            while True:
                ready, _, _ = select.select([master, input_fd], [], [], 0.05)
                if master in ready:
                    try:
                        output = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        output = b""
                    if not output:
                        break
                    write_all(output_fd, output)
                    watch.feed(output, time.monotonic())
                if process.poll() is not None and master not in ready:
                    break
                if input_fd in ready and process.poll() is None:
                    incoming = os.read(input_fd, 65536)
                    if not incoming:
                        break
                    write_all(master, incoming)
                keys, status = request.advance(time.monotonic())
                if keys and process.poll() is None:
                    write_all(master, keys)
                for message in (status, watch.message()):
                    if message:
                        write_all(output_fd, f"\r\nds: {message}\r\n".encode())
    finally:
        signal.signal(signal.SIGWINCH, previous_resize)
        try:
            termios.tcsetattr(input_fd, termios.TCSADRAIN, settings)
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)
            if process is not None:
                process.wait(timeout=5)
    return process.returncode if process.returncode >= 0 else 128 - process.returncode
