import hashlib
import json
import os
import re
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path

from devin_switch import sessions
from devin_switch.native import Native
from devin_switch.store import Account, Store, SwitchError, private_directory, write_json


def session_key(session_id: str) -> str:
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
        raise SwitchError("An exact GUI conversation ID is required.")
    return hashlib.sha256(session_id.encode()).hexdigest()


def binding_path(store: Store, session_id: str) -> Path:
    return store.root / "acp" / "sessions" / f"{session_key(session_id)}.json"


def bound_account(store: Store, session_id: str) -> Account | None:
    try:
        value = json.loads(binding_path(store, session_id).read_text())
        if value["session_id"] != session_id:
            raise ValueError
        return store.account(value["account"])
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError) as exc:
        raise SwitchError(
            "The saved GUI account binding is invalid; no account was changed."
        ) from exc


def bind(store: Store, session_id: str, account: Account) -> None:
    path = binding_path(store, session_id)
    private_directory(path.parent)
    write_json(path, {"session_id": session_id, "account": account.name})


def saved_chat(store: Store, session_id: str) -> dict | None:
    return next((chat for chat in sessions.history(store) if chat["id"] == session_id), None)


def check_handoff(store: Store, session_id: str, project: str, run_id: str) -> None:
    with store.lock(timeout=5):
        saved = saved_chat(store, session_id)
        if saved is None:
            raise SwitchError(
                "Switch canceled: this conversation is not saved in shared history yet. "
                "The current connection and account are unchanged. Send a normal message "
                "first, then retry /switch. To choose an account before starting a chat, "
                "run ds use ALIAS in Terminal and open a new GUI chat."
            )
        if saved["project"] != project or not Path(project).is_dir():
            raise SwitchError("Switch canceled: the saved project does not match this chat.")
        others = [run for run in sessions.runs(store) if run["id"] != run_id]
        if blocker := sessions.resume_blocker(saved, others):
            raise SwitchError(blocker)


def request_path(store: Store, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise SwitchError("Invalid GUI launch ID.")
    return store.root / "acp" / "requests" / f"{run_id}.json"


def live(store: Store) -> list[dict]:
    return [
        run
        for run in sessions.runs(store)
        if run["active"]
        and run["kind"] == "chat"
        and run["session_id"]
        and store.locked(f"acp-controller-{run['id']}.lock")
    ]


def target(store: Store, session_id: str | None, run_id: str | None) -> dict:
    if session_id is not None:
        session_key(session_id)
    matches = [
        run
        for run in live(store)
        if (run["session_id"] == session_id if session_id is not None else run["id"] == run_id)
    ]
    if len(matches) != 1:
        raise SwitchError("No unique live GUI chat matches. Run ds sessions --gui for exact IDs.")
    return matches[0]


def read_request(path: Path) -> dict:
    try:
        request = json.loads(path.read_text())
        if (
            not isinstance(request, dict)
            or request.get("stage") not in {"queued", "switching"}
            or not isinstance(request.get("session_id"), str)
            or not (request.get("account") is None or isinstance(request["account"], str))
        ):
            raise ValueError
        return request
    except (ValueError, TypeError) as exc:
        raise SwitchError("Invalid queued GUI switch. No account was changed.") from exc


def check_login(native: Native, account: Account) -> None:
    with native.store.account_lock(account, shared=True):
        native.require_login(account)


def queue(native: Native, session_id: str | None, account_name: str | None, *, cancel=False) -> str:
    store = native.store
    with store.lock(timeout=5):
        run = target(store, session_id, os.environ.get("DS_ACP_RUN_ID"))
        path = request_path(store, run["id"])
        if path.exists():
            request = read_request(path)
            if request.get("stage") != "queued":
                raise SwitchError("This GUI handoff has already started; wait for its result.")
            if not cancel:
                raise SwitchError("A GUI switch is already queued. Use --cancel first.")
        if cancel:
            path.unlink(missing_ok=True)
            return "Queued GUI switch canceled. No account was changed."
    if account_name:
        account = store.account(account_name)
        if account.name == run["account"]:
            raise SwitchError("This GUI chat already uses that account.")
        check_login(native, account)
    with store.lock(timeout=5):
        current = target(store, run["session_id"], None)
        if current["id"] != run["id"] or current["account"] != run["account"]:
            raise SwitchError("This GUI chat changed while checking accounts; try again.")
        if path.exists():
            raise SwitchError("A GUI switch is already queued. Use --cancel first.")
        private_directory(path.parent)
        write_json(
            path, {"account": account_name, "session_id": run["session_id"], "stage": "queued"}
        )
    destination = account_name or "best eligible account (checked when idle)"
    return (
        f"Queued GUI switch: {run['account']} → {destination}. "
        "It will run when this conversation is idle. Check /switch-status in the GUI."
    )


class Lease:
    def __init__(
        self, native: Native, account: Account, project: Path, session_id: str | None, *, chat: bool
    ):
        self.store = native.store
        self.stack = ExitStack()
        self.closed = False
        self.run = sessions.Run(
            uuid.uuid4().hex,
            account.name,
            str(project),
            "chat" if chat else "command",
            time.time(),
            session_id,
        )
        try:
            with self.store.lock(timeout=5):
                self.store.account(account.name)
                if session_id:
                    saved = sessions.resumable(self.store, session_id)
                    if saved["project"] != str(project):
                        raise SwitchError("Resume this conversation from its original project.")
                account_fd = self.stack.enter_context(self.store.account_lock(account, shared=True))
                run_fd = self.stack.enter_context(self.store.lock(f"run-{self.run.id}.lock"))
                self.stack.enter_context(self.store.lock(f"acp-controller-{self.run.id}.lock"))
                self.fds = (account_fd, run_fd)
                private_directory(self.store.root / "runs")
                self.path = self.store.root / "runs" / f"{self.run.id}.json"
                sessions.record_process(self.store, self.run.id, os.getpid(), role="wrapper")
                write_json(self.path, asdict(self.run))
            native.require_login(account)
        except BaseException:
            self.close(ended=True)
            raise

    def attach(self, session_id: str) -> None:
        session_key(session_id)
        with self.store.lock(timeout=5):
            others = [run for run in sessions.runs(self.store) if run["id"] != self.run.id]
            if blocker := sessions.resume_blocker({"id": session_id}, others):
                raise SwitchError(blocker)
            self.run = replace(self.run, session_id=session_id)
            write_json(self.path, asdict(self.run))

    def close(self, *, ended: bool) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if ended and hasattr(self, "path"):
                write_json(self.path, asdict(replace(self.run, ended_at=time.time())))
            request_path(self.store, self.run.id).unlink(missing_ok=True)
        finally:
            self.stack.close()
