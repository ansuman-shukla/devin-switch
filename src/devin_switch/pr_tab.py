"""The PR tab: the pull requests seen in a GUI chat, in a Browser tab of the chat.

Desktop's own PR tab loads pull requests through Devin's cloud, which cannot see private
repositories. This one is local. The bridge notes every GitHub pull request link that shows up
in a chat: in what the agent and the user wrote, and in what a `gh pr` command was given and
printed. `/pr` typed in the chat opens the page that lists them, served by `side.Server` like
the side page. While a page is open the bridge looks them up with the `gh` CLI, under the
user's own GitHub login, and the page's buttons run `gh` the same way. What may be done to a
pull request is decided here, never in the page. Everything is kept in memory only.
"""

import asyncio
import html
import json
import os
import re
import shutil
import signal
import time
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from devin_switch import native, subagent_tab
from devin_switch.store import Store

PAGE = Path(__file__).parent / "pr_page"
ASSETS = {"pr.css": "text/css", "pr-view.js": "text/javascript", "pr.js": "text/javascript"}
# Only a pull request itself: not an issue, the list of pulls or a file. A number followed by
# a letter or by more digits than GitHub numbers have is not one.
LINK = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)/pull/"
    r"0*([1-9][0-9]{0,8})(?![A-Za-z0-9])"
)
CHUNKS = ("agent_message_chunk", "user_message_chunk")
STEPS = ("tool_call", "tool_call_update")
COMMAND = "gh pr"
NUMBER = re.compile(r"#?0*([1-9][0-9]{0,8})")
RUN = re.compile(r"/actions/runs/([0-9]+)")
# A tool call's command is written once the call runs; its output is whole once it has ended.
RAN = ("in_progress", "completed", "failed")
ENDED = ("completed", "failed")
MAX_PULLS = 20
MAX_CALLS = 50
MAX_WRITERS = 50
# Longer than any link: the end of a text that may hold one still being written.
TAIL = 200
FIELDS = (
    "number,title,state,isDraft,url,headRefName,baseRefName,headRefOid,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,additions,deletions,changedFiles,"
    "author,updatedAt,latestReviews,reviewRequests"
)
ALLOWED = "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed,viewerDefaultMergeMethod"
# Where Homebrew puts gh: a bridge started by an app from the Dock has no shell PATH.
FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
TIMEOUT = 30
REFRESH = 30
BATCH = 4
ERROR_LIMIT = 200
MISSING = "GitHub CLI (gh) was not found"
UNREADABLE = "gh printed something that is not a pull request"
OK = {"ok": True}
ACTIONS = ("merge", "update", "ready", "draft", "rerun")
# What GitHub calls a pull request that can be merged now (hooks and failing optional checks
# do not stop a merge).
MERGEABLE = ("clean", "unstable", "has_hooks")
STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
CONFLICTS = {"CONFLICTING": True, "MERGEABLE": False}
MERGE_STATES = ("clean", "behind", "blocked", "dirty", "draft", "unstable", "has_hooks")
DECISIONS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "REVIEW_REQUIRED": "review_required",
}
REVIEWS = ("approved", "changes_requested", "commented", "dismissed", "pending")
# How a finished check run ended: any other ending (failure, cancelled, timed out) failed.
CONCLUSIONS = {"SUCCESS": "pass", "SKIPPED": "skipped", "NEUTRAL": "skipped"}
# A commit status: any other state (pending, expected) is still pending.
STATUSES = {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail"}
# Each merge method, in the order shown, with what says that a repository allows it.
METHODS = {
    "merge": ("MERGE", "mergeCommitAllowed"),
    "squash": ("SQUASH", "squashMergeAllowed"),
    "rebase": ("REBASE", "rebaseMergeAllowed"),
}


def links(text: str, ended: bool = False) -> tuple[list[str], str]:
    """The pull request links in a text, and the end of it that may hold one more.

    A link that ends the text counts only once the text has `ended`: while it is still being
    written, "…/pull/1" can become "…/pull/123". Links come without what follows the number.
    """
    found, start = [], 0
    for match in LINK.finditer(text):
        if match.end() == len(text) and not ended:
            break
        found.append(f"https://github.com/{match[1]}/{match[2]}/pull/{match[3]}")
        start = match.end()
    return found, "" if ended else text[max(start, len(text) - TAIL) :]


def texts(value: object) -> Iterator[str]:
    """Every text inside a tool call's input or output, in order, however it is nested."""
    waiting = [value]
    while waiting:
        item = waiting.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict | list):
            waiting.extend(reversed(list(item.values()) if isinstance(item, dict) else item))


def command(update: dict) -> str:
    """What a tool call says it runs: its title, and the command line of a shell call.

    Not the rest of its input: a file written about `gh pr` is not a `gh pr` command.
    """
    given = update.get("rawInput")
    line = given.get("command") if isinstance(given, dict) else given
    return f"{word(update.get('title'))}\n{word(line)}"


def written(update: dict) -> str:
    """The text of a message chunk ("": it holds something else)."""
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) and content.get("type") == "text" else ""


@dataclass(frozen=True)
class Ran:
    """How one `gh` command ended."""

    code: int
    out: str = ""
    err: str = ""


async def run(store: Store, folder: Path, *arguments: str) -> Ran | None:
    """Run `gh` in a folder as the user would (None: gh is not installed).

    The user's own GitHub login applies, even when the bridge was started inside one login's
    isolated environment. A command that takes too long is killed with all it started.
    """
    found = shutil.which("gh")
    gh = found or next((path for path in FALLBACKS if os.access(path, os.X_OK)), None)
    if gh is None:
        return None
    environment = {
        **os.environ,
        "GH_CONFIG_DIR": native.github_config_directory(store),
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
    }
    if not found:
        # gh runs git, and both sit in Homebrew's folder.
        environment["PATH"] = f"{Path(gh).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    try:
        process = await asyncio.create_subprocess_exec(
            gh,
            *arguments,
            cwd=folder,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return Ran(1, err=f"gh could not be started: {exc.strerror or exc}")
    try:
        out, err = await asyncio.wait_for(process.communicate(), TIMEOUT)
    except TimeoutError:
        return Ran(1, err=f"gh did not answer within {TIMEOUT} seconds")
    finally:
        if process.returncode is None:
            # Its own session, so whatever it started (git, a credential helper) goes with it.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 5)
    return Ran(process.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


def trouble(ran: Ran) -> str:
    """Why a `gh` command failed, in one short line."""
    lines = [line.strip() for line in ran.err.splitlines() if line.strip()]
    return (lines[0] if lines else f"gh ended with code {ran.code}")[:ERROR_LIMIT]


def short(error: BaseException) -> str:
    """An exception in one short line."""
    lines = str(error).strip().splitlines()
    name = type(error).__name__
    return (f"{name}: {lines[0].strip()}" if lines else name)[:ERROR_LIMIT]


def answer(ran: Ran) -> dict | None:
    """The JSON object a `gh` command printed (None: it failed, or printed something else)."""
    if ran.code:
        return None
    try:
        value = json.loads(ran.out)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def word(value: object) -> str:
    return value if isinstance(value, str) else ""


def count(value: object) -> int:
    return value if type(value) is int else 0


def rows(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def login(value: object) -> str:
    return word(value.get("login")) if isinstance(value, dict) else ""


def check(item: dict) -> dict:
    """One entry of gh's `statusCheckRollup`: a check run, or a commit status."""
    if item.get("__typename") == "StatusContext":
        name, workflow, url = item.get("context"), None, item.get("targetUrl")
        status = STATUSES.get(word(item.get("state")), "pending")
    else:
        name, workflow, url = item.get("name"), item.get("workflowName"), item.get("detailsUrl")
        ended = item.get("status") == "COMPLETED"
        status = CONCLUSIONS.get(word(item.get("conclusion")), "fail") if ended else "pending"
    return {
        "name": word(name),
        "workflow": word(workflow) or None,
        "status": status,
        "url": word(url) or None,
    }


def review(view: dict) -> dict:
    """Who reviewed a pull request and who was asked to, from gh's review fields."""
    reviews = []
    for item in rows(view.get("latestReviews")):
        state = word(item.get("state")).lower()
        reviews.append(
            {
                "author": login(item.get("author")),
                "state": state if state in REVIEWS else "commented",
            }
        )
    # A requested person has a login, a requested team a name.
    asked = (
        word(item.get("login")) or word(item.get("name"))
        for item in rows(view.get("reviewRequests"))
    )
    return {
        "decision": DECISIONS.get(word(view.get("reviewDecision")), "none"),
        "reviews": reviews,
        "requested": [name for name in asked if name],
    }


def allowed(repository: dict) -> list[str]:
    """The merge methods a repository allows, its default first."""
    default = repository.get("viewerDefaultMergeMethod")
    methods = [method for method, (_, allows) in METHODS.items() if repository.get(allows) is True]
    return sorted(methods, key=lambda method: METHODS[method][0] != default)


def failed_runs(checks: list[dict]) -> list[str]:
    """The GitHub Actions runs of the checks that failed, each once."""
    found = (RUN.search(item["url"] or "") for item in checks if item["status"] == "fail")
    return list(dict.fromkeys(match[1] for match in found if match))


def blocker(details: dict) -> str:
    """Why an open pull request cannot be merged now: the first reason that applies."""
    behind, conflicts, base = details["behind"], details["conflicts"], details["base"]
    if details["draft"]:
        return "Still a draft"
    if conflicts:
        return "Has merge conflicts"
    if behind:
        return f"{behind} commit{'' if behind == 1 else 's'} behind {base}"
    if details["mergeState"] == "blocked":
        return "Blocked by branch rules (reviews or required checks)"
    if conflicts is None or details["mergeState"] == "unknown":
        return "GitHub is still checking if it can merge"
    if behind is None:
        return f"Can't tell if it is behind {base}"
    return "GitHub says it can't merge yet"


def gate(details: dict) -> tuple[dict, dict]:
    """What may be done to a pull request now, and why an open one cannot be merged.

    A merge needs GitHub to say it can merge and the branch to hold all of its base.
    """
    opened, draft, behind = details["state"] == "open", details["draft"], details["behind"]
    can = {
        "merge": opened
        and not draft
        and details["conflicts"] is False
        and behind == 0
        and details["mergeState"] in MERGEABLE,
        "update": opened and behind is not None and behind > 0,
        "ready": opened and draft,
        "draft": opened and not draft,
        "rerun": opened and bool(failed_runs(details["checks"])),
    }
    return can, {"merge": blocker(details)} if opened and not can["merge"] else {}


def describe(view: dict, behind: int | None, methods: list[str]) -> dict:
    """What the page shows of a pull request, from what `gh pr view` printed."""
    merge_state = word(view.get("mergeStateStatus")).lower()
    details = {
        "title": word(view.get("title")),
        "state": STATES.get(word(view.get("state")), "closed"),
        "draft": view.get("isDraft") is True,
        "head": word(view.get("headRefName")),
        "base": word(view.get("baseRefName")),
        "author": login(view.get("author")),
        "updated": word(view.get("updatedAt")),
        "additions": count(view.get("additions")),
        "deletions": count(view.get("deletions")),
        "files": count(view.get("changedFiles")),
        "behind": behind,
        "conflicts": CONFLICTS.get(word(view.get("mergeable"))),
        "mergeState": merge_state if merge_state in MERGE_STATES else "unknown",
        "review": review(view),
        "checks": [check(item) for item in rows(view.get("statusCheckRollup"))],
        "methods": methods,
    }
    details["can"], details["why"] = gate(details)
    return details


@dataclass
class Pull:
    """One pull request of a chat's tab: what the page shows, and what only actions need."""

    url: str
    repo: str
    number: int
    loading: bool = True
    error: str | None = None
    busy: str | None = None
    details: dict = field(default_factory=dict)
    oid: str = ""
    # One look at a time, so an older answer never replaces a newer one.
    asking: asyncio.Lock = field(default_factory=asyncio.Lock)

    def shown(self) -> dict:
        return {
            "url": self.url,
            "repo": self.repo,
            "number": self.number,
            "loading": self.loading,
            "error": self.error,
            "busy": self.busy,
            **self.details,
        }


def refusal(pull: Pull, action: str, method: object) -> str | None:
    """Why an action may not run on a pull request now (None: it may)."""
    details = pull.details
    if pull.busy:
        return "Another action is still running on this pull request"
    if not details:
        return pull.error or "This pull request is still loading"
    if details["state"] != "open":
        return f"This pull request is {details['state']}"
    if not details["can"][action]:
        if action == "merge":
            return details["why"]["merge"]
        if action == "update":
            known, base = details["behind"] is not None, details["base"]
            return (
                f"Already up to date with {base}" if known else f"Can't tell if it is behind {base}"
            )
        return {
            "ready": "Already ready for review",
            "draft": "Already a draft",
            "rerun": "No failed GitHub Actions run to re-run",
        }[action]
    if action == "merge" and method not in details["methods"]:
        if not details["methods"]:
            return "Can't tell which merge methods this repository allows"
        return "This repository does not allow that merge method"
    return None


def commands(pull: Pull, action: str, method: object) -> list[tuple[str, ...]]:
    """The `gh` commands of an action that `refusal` let through."""
    if action == "merge":
        # The commit is named after the pull request, and only the commit the page showed is
        # merged. The branch stays, and nothing waits for checks or overrides branch rules.
        merge = ("pr", "merge", pull.url, f"--{method}", "--subject", pull.details["title"])
        return [(*merge, "--match-head-commit", pull.oid)]
    if action == "rerun":
        runs = failed_runs(pull.details["checks"])
        return [("run", "rerun", run, "--failed", "-R", pull.repo) for run in runs]
    return {
        "update": [("pr", "update-branch", pull.url)],
        "ready": [("pr", "ready", pull.url)],
        "draft": [("pr", "ready", pull.url, "--undo")],
    }[action]


@dataclass
class Tab:
    """One chat's pull requests in first-seen order, the text still being read, and its pages."""

    pulls: dict[str, Pull] = field(default_factory=dict)
    dismissed: set[str] = field(default_factory=set)
    notice: str | None = None
    # Per writer (None: the chat's own agent, or the user): the message being written and its
    # end, which may hold a link still being written.
    tails: dict[str | None, tuple[tuple, str]] = field(default_factory=dict)
    # The chat's `gh pr` tool calls, each with the links its input and output ended with so far.
    calls: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    listeners: set[asyncio.Queue] = field(default_factory=set)
    watcher: asyncio.Task | None = None
    # What the pages are told when the watcher gave up by itself, until one is started again.
    stopped: str | None = None
    added: asyncio.Event = field(default_factory=asyncio.Event)


class Pulls:
    """Each chat's pull requests, and the pages watching them."""

    def __init__(self, store: Store, browse: Callable[[str], bool] | None = None) -> None:
        self.store = store
        # What opens a link in the user's own browser.
        self.browse = browse or webbrowser.open
        self.tabs: dict[str, Tab] = {}
        self.tasks: set[asyncio.Task] = set()
        # Whether gh was missing the last time it was needed.
        self.missing = False
        self.methods: dict[str, list[str]] = {}
        self.asking: dict[str, asyncio.Lock] = {}

    def feed(self, session_id: object, update: object) -> None:
        """Note the links in one `session/update` of a chat, live or replayed.

        Never raises and never waits (no `gh` runs here): the caller is the chat's read loop.
        """
        try:
            if isinstance(session_id, str) and isinstance(update, dict):
                self.read(self.tabs.setdefault(session_id, Tab()), update)
        except Exception:
            # Whatever a native sends, a tab beside the chat must never end the chat's read loop.
            pass

    def read(self, tab: Tab, update: dict) -> None:
        kind, writer = update.get("sessionUpdate"), subagent_tab.author(update)
        if kind in STEPS:
            # A writer that calls a tool has finished what it was saying.
            self.end(tab, writer)
            if isinstance(call := update.get("toolCallId"), str):
                self.step(tab, call, update)
        if kind not in CHUNKS:
            # Usage, plans, thoughts and settings arrive in the middle of a message that is
            # being streamed too, so they say nothing about where it ends.
            return
        meta = update.get("_meta")
        message = (kind, meta.get(subagent_tab.STREAM) if isinstance(meta, dict) else None)
        if writer in tab.tails and tab.tails[writer][0] != message:
            # The writer's next message begins.
            self.end(tab, writer)
        found, tail = links(tab.tails.get(writer, (message, ""))[1] + written(update))
        tab.tails[writer] = (message, tail)
        self.keep(tab, found)
        if len(tab.tails) > MAX_WRITERS:
            for other in list(tab.tails):
                self.end(tab, other)

    def end(self, tab: Tab, writer: str | None) -> None:
        """A writer's message has ended, so a link it ended with is whole."""
        if writer in tab.tails:
            self.keep(tab, links(tab.tails.pop(writer)[1], ended=True)[0])

    def step(self, tab: Tab, call: str, update: dict) -> None:
        """Read a `gh pr` command's input and output; other tool calls say nothing here.

        The native streams a command while it is written and its output while it runs, each
        time whole so far. A link that ends such a text waits until the text is known whole.
        """
        if call not in tab.calls:
            if COMMAND not in command(update):
                return
            tab.calls[call] = {"input": [], "output": []}
            for oldest in list(tab.calls)[:-MAX_CALLS]:
                del tab.calls[oldest]
        held, found = tab.calls[call], []
        for part, keys in (("input", ("rawInput",)), ("output", ("content", "rawOutput"))):
            if any(key in update for key in keys):
                held[part] = []
                for text in texts([update.get(key) for key in keys]):
                    whole, tail = links(text)
                    found += whole
                    held[part] += links(tail, ended=True)[0]
        status = update.get("status")
        for part, states in (("input", RAN), ("output", ENDED)):
            if status in states:
                found += held[part]
                held[part] = []
        self.keep(tab, found)

    def flush(self, session_id: str) -> None:
        """Count what the chat's messages and commands ended with: its turn is over."""
        if tab := self.tabs.get(session_id):
            for writer in list(tab.tails):
                self.end(tab, writer)
            for held in tab.calls.values():
                for part in held:
                    self.keep(tab, held[part])
                    held[part] = []

    def said(self, session_id: str, text: str) -> None:
        """Note the links in a prompt the user sent: a whole message."""
        if found := links(text, ended=True)[0]:
            self.keep(self.tabs.setdefault(session_id, Tab()), found)

    def keep(self, tab: Tab, urls: list[str], asked: bool = False) -> None:
        """Add links to a chat's tab. One the user dismissed returns only when `asked` for."""
        new = False
        for url in urls:
            key = url.lower()
            if key in tab.pulls or (key in tab.dismissed and not asked):
                continue
            tab.dismissed.discard(key)
            owner, name, _, number = url.split("/")[3:]
            tab.pulls[key] = Pull(url, f"{owner}/{name}", int(number))
            new = True
        for oldest in list(tab.pulls)[:-MAX_PULLS]:
            del tab.pulls[oldest]
        if new:
            # A page that is open shows it at once, and its watcher looks it up.
            tab.added.set()
            self.tell(tab)

    def show(self, session_id: str, words: list[str], folder: Path) -> None:
        """Take a `/pr` typed in a chat: alone, or with links and numbers to add to its tab.

        Nothing here waits. A number, or with nothing listed yet the pull request of the
        branch checked out in the chat's folder, is found with `gh` beside the chat.
        """
        tab = self.tabs.setdefault(session_id, Tab())
        tab.notice = None
        self.flush(session_id)
        self.start(tab)
        numbers = []
        for item in words:
            if found := links(item, ended=True)[0]:
                self.keep(tab, found, asked=True)
            elif number := NUMBER.fullmatch(item):
                numbers.append(number[1])
            else:
                tab.notice = f"Not a pull request link or number: {item[:ERROR_LIMIT]}"
        if numbers or not (words or tab.pulls):
            task = asyncio.create_task(self.find(tab, numbers, folder))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        self.tell(tab)

    async def find(self, tab: Tab, numbers: list[str], folder: Path) -> None:
        """Add a folder's pull requests by number, or without one its current branch's."""
        for number in numbers or [None]:
            target = [number] if number else []
            seen = answer(await self.gh("pr", "view", *target, "--json", "url", folder=folder))
            if found := links(word(seen.get("url")) if seen else "", ended=True)[0]:
                self.keep(tab, found, asked=True)
            elif number:
                tab.notice = f"No pull request #{number} in {folder.name}"
                self.tell(tab)

    async def act(self, request: dict) -> dict | None:
        """Do what a page's button asks for: ok, or why not (None: the chat has no tab)."""
        session_id, action, url = request.get("session"), request.get("action"), request.get("url")
        if not isinstance(session_id, str) or not isinstance(action, str):
            raise ValueError("an action names its chat and what to do")
        tab = self.tabs.get(session_id)
        if tab is None:
            return None
        if action == "refresh":
            tab.notice = None
            # A watcher that had stopped starts again, and its first pass looks at them all.
            if not self.start(tab):
                await self.refresh(tab, list(tab.pulls.values()))
            self.tell(tab)
            return OK
        key = url.lower() if isinstance(url, str) else None
        if key not in tab.pulls:
            return {"error": "That pull request is not in this chat's tab"}
        pull = tab.pulls[key]
        if action == "dismiss":
            # Remembered, so the same link said again in the chat does not bring it back.
            del tab.pulls[key]
            tab.dismissed.add(key)
            self.tell(tab)
            return OK
        if action == "open":
            return await self.visit(pull, request.get("target") or pull.url)
        if action not in ACTIONS:
            return {"error": "Unknown action"}
        if reason := refusal(pull, action, request.get("method")):
            return {"error": reason}
        pull.busy, failed = action, None
        self.tell(tab)
        try:
            for arguments in commands(pull, action, request.get("method")):
                ran = await self.gh(*arguments)
                failed = failed or (trouble(ran) if ran.code else None)
            # Whatever gh said, the page then shows the pull request as GitHub has it now.
            await self.look(tab, pull)
        finally:
            pull.busy = None
            self.tell(tab)
        return {"error": failed} if failed else OK

    async def visit(self, pull: Pull, target: object) -> dict:
        """Open a pull request, or one of its checks, in the user's own browser."""
        known = {pull.url, *(item["url"] for item in pull.details.get("checks", ()))}
        web = isinstance(target, str) and target.startswith(("https://", "http://"))
        if not web or target not in known:
            return {"error": "That link is not this pull request's"}
        opened = await asyncio.to_thread(self.browse, target)
        return OK if opened else {"error": "The browser could not be opened"}

    async def gh(self, *arguments: str, folder: Path | None = None) -> Ran:
        """Run `gh`: in the user's home, or in a chat's folder for what depends on its repo."""
        ran = await run(self.store, folder or Path.home(), *arguments)
        self.missing = ran is None
        return ran or Ran(127, err=MISSING)

    async def behind(self, repo: str, base: str, oid: str) -> int | None:
        """How many commits of its base a pull request lacks (None: GitHub did not say)."""
        path = f"repos/{repo}/compare/{quote(base, safe='')}...{oid}"
        ran = await self.gh("api", path, "--jq", ".behind_by")
        text = ran.out.strip()
        return int(text) if not ran.code and text.isascii() and text.isdigit() else None

    async def allowed(self, repo: str) -> list[str]:
        """The merge methods a repository allows, asked once ([]: GitHub has not said yet)."""
        async with self.asking.setdefault(repo, asyncio.Lock()):
            if repo not in self.methods:
                seen = answer(await self.gh("repo", "view", repo, "--json", ALLOWED))
                if seen is None:
                    return []
                self.methods[repo] = allowed(seen)
            return self.methods[repo]

    async def look(self, tab: Tab, pull: Pull) -> None:
        """Ask GitHub about one pull request. What goes wrong is told on that pull request."""
        async with pull.asking:
            ran = await self.gh("pr", "view", pull.url, "--json", FIELDS)
            view = answer(ran)
            if view is None:
                pull.error = trouble(ran) if ran.code else UNREADABLE
            else:
                base, oid = word(view.get("baseRefName")), word(view.get("headRefOid"))
                # Only an open pull request can be merged, so only then does its base matter.
                behind = (
                    await self.behind(pull.repo, base, oid) if view.get("state") == "OPEN" else None
                )
                pull.details = describe(view, behind, await self.allowed(pull.repo))
                pull.error, pull.oid = None, oid
            pull.loading = False
        self.tell(tab)

    async def refresh(self, tab: Tab, pulls: list[Pull]) -> None:
        """Look pull requests up, a few at a time."""
        for start in range(0, len(pulls), BATCH):
            await asyncio.gather(*(self.look(tab, pull) for pull in pulls[start : start + BATCH]))

    async def watch(self, tab: Tab) -> None:
        """Keep a chat's pull requests fresh while a page shows them.

        All of them every `REFRESH` seconds, and one that was just added at once.
        """
        while True:
            again = time.monotonic() + REFRESH
            await self.refresh(tab, list(tab.pulls.values()))
            while (left := again - time.monotonic()) > 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(tab.added.wait(), left)
                tab.added.clear()
                await self.refresh(tab, [pull for pull in tab.pulls.values() if pull.loading])

    def start(self, tab: Tab) -> bool:
        """Start looking a tab's pull requests up, if a page shows them and nothing does yet."""
        if not tab.listeners or tab.watcher:
            return False
        tab.stopped = None
        tab.watcher = watcher = asyncio.create_task(self.watch(tab))
        watcher.add_done_callback(lambda ended: self.gave_up(tab, ended))
        return True

    def gave_up(self, tab: Tab, watcher: asyncio.Task) -> None:
        """A watcher that ended on an error says so on its tab: a page must not look live
        while nothing looks its pull requests up. One that was cancelled just stops."""
        if watcher.cancelled():
            return
        why = short(watcher.exception()).rstrip(".")
        if tab.watcher is watcher:
            tab.watcher = None
            tab.stopped = f"Stopped checking GitHub: {why}. Press Refresh to try again."
            self.tell(tab)

    def snapshot(self, session_id: str) -> dict:
        """What the page shows."""
        tab = self.tabs.get(session_id)
        return {
            "pulls": [pull.shown() for pull in tab.pulls.values()] if tab else [],
            "notice": MISSING if self.missing else (tab.stopped or tab.notice) if tab else None,
        }

    def showing(self, session_id: str) -> bool:
        return session_id in self.tabs and bool(self.tabs[session_id].listeners)

    @contextmanager
    def listen(self, session_id: str) -> Iterator[asyncio.Queue]:
        """A connected page's queue. A token in it says: send the page a fresh snapshot.

        A chat's pull requests are looked up only while a page of it is connected.
        """
        tab = self.tabs.setdefault(session_id, Tab())
        queue: asyncio.Queue = asyncio.Queue()
        # The page shows the pull requests as they are right after connecting.
        queue.put_nowait(True)
        tab.listeners.add(queue)
        self.start(tab)
        try:
            yield queue
        finally:
            tab.listeners.discard(queue)
            if not tab.listeners and tab.watcher:
                tab.watcher.cancel()
                tab.watcher = None

    def tell(self, tab: Tab) -> None:
        # One waiting token covers every change until it is sent, so a stalled page holds one.
        for queue in tab.listeners:
            if queue.empty():
                queue.put_nowait(True)

    def release(self, tab: Tab) -> None:
        """End a tab's connected pages' streams, and stop looking its pull requests up."""
        if tab.watcher:
            tab.watcher.cancel()
            tab.watcher = None
        for queue in tab.listeners:
            queue.put_nowait(None)

    def forget(self, session_id: str) -> None:
        """Drop a chat's tab: the chat was closed, so nothing of it is kept or looked up."""
        if tab := self.tabs.pop(session_id, None):
            self.release(tab)

    async def close(self) -> None:
        """End every connected page's stream, and stop looking pull requests up."""
        running = [*self.tasks, *(tab.watcher for tab in self.tabs.values() if tab.watcher)]
        for tab in self.tabs.values():
            self.release(tab)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


def page(session_id: str, token: str) -> str:
    """The page shell: its scripts read the chat and token from <body>."""
    token = html.escape(token)
    files = {name: f"/pr/assets/{name}?t={token}" for name in ASSETS}
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Pull requests</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link rel="stylesheet" href="{files["pr.css"]}"></head>'
        f'<body data-session="{html.escape(session_id)}" data-token="{token}">'
        f'<script src="{files["pr-view.js"]}"></script><script src="{files["pr.js"]}"></script>'
        "</body></html>"
    )
