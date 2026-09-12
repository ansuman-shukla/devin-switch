import errno
import fcntl
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass

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
        if request is None:
            return b"", ""
        if now - self.started >= self.timeout:
            try:
                with self.store.lock():
                    if self.read() == request:
                        handoff.cancel(self.store, {"id": self.run_id})
            except SwitchError:
                return b"", ""
            self.request = None
            return b"", "Switch canceled: the CLI did not exit normally. This chat is still open."
        if process_exists(request["requester_pid"]) or now < self.next_key:
            return b"", ""
        if not self.escaped:
            self.escaped, self.next_key = True, now + 0.2
            return b"\x1b", ""
        self.next_key = now + 0.5
        return b"\x04", ""


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
            os.close(slave)
            slave = -1
            signal.signal(signal.SIGWINCH, resize)
            tty.setraw(input_fd)
            request = ExitRequest(native.store, native.run_id)
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
                if process.poll() is not None and master not in ready:
                    break
                if input_fd in ready and process.poll() is None:
                    incoming = os.read(input_fd, 65536)
                    if not incoming:
                        break
                    write_all(master, incoming)
                keys, message = request.advance(time.monotonic())
                if keys and process.poll() is None:
                    write_all(master, keys)
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
