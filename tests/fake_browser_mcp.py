"""An offline stand-in for the Browser MCP server, answering in its text format.

It keeps tabs of its own next to one that belongs to the owner, logs every `commands` string
it receives, and reads `control.json` beside the log before each call:
`moves` maps a command name to the URL the tab is on once that command ran, `late` makes the
move happen only when the next call starts (so no Page line reports it), and `error` is
returned instead of running anything. Addresses on a host that starts with `down.` fail to
load: like upstream, `tab new <url>` then leaves a new current tab without naming it.
"""

import json
import os
import re
import sys
from pathlib import Path

folder = Path(os.environ["FAKE_BROWSER_MCP"])
OWNER_TAB = 7
COMMANDS = "goto back tab browser help get snapshot click eval screenshot upload crash".split()
NO_TAB = (
    "There's no tab yet: use `goto <url>`, or `tab list` and `tab select <id>` to use one of "
    "the user's tabs"
)
histories = {OWNER_TAB: ["https://owner.test/inbox"]}
opened: set[int] = set()
state: dict = {"current": None, "last": 500, "late": None}


class Failure(Exception):
    pass


def control() -> dict:
    path = folder / "control.json"
    return json.loads(path.read_text()) if path.exists() else {}


def words(line: str) -> list[str]:
    found = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', line)
    return [json.loads(word) if word.startswith('"') else word for word in found]


def normalize(url: str) -> str:
    if re.match(r"[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
        return url
    return ("http://" if re.match(r"localhost|127\.|\[::1\]", url) else "https://") + url


def title(url: str) -> str:
    return f"Title of {url.split('/')[2]}" if "://" in url else ""


def info() -> tuple[int, str, str] | None:
    tab = state["current"]
    return None if tab is None else (tab, histories[tab][-1], title(histories[tab][-1]))


def history() -> list[str]:
    if state["current"] is None:
        raise Failure(NO_TAB)
    return histories[state["current"]]


def load(url: str) -> None:
    if "://down." in url:
        raise Failure(f"net::ERR_CONNECTION_REFUSED at {url}")
    history().append(url)


def create(url: str | None) -> int:
    state["last"] += 1
    tab = state["current"] = state["last"]
    histories[tab] = ["about:blank"]
    opened.add(tab)
    if url:
        load(url)
    return tab


def tab_step(args: list[str], notes: list[str], printed: list[str]) -> None:
    browser = None
    if "--browser" in args:
        at = args.index("--browser")
        browser = args[at + 1]
        del args[at : at + 2]
    if args[0] == "list":
        printed.extend(
            f"{'*' if tab == state['current'] else ' '} {tab} {json.dumps(title(urls[-1]))}"
            f" \u2014 {urls[-1]}"
            for tab, urls in histories.items()
        )
    elif args[0] == "new":
        tab = create(normalize(args[1]) if len(args) > 1 else None)
        notes.append(f"tab {tab} in {browser}" if browser else f"tab {tab}")
    elif args[0] == "select":
        if int(args[1]) not in histories:
            raise Failure(f"There's no tab {args[1]}. Run `tab list`")
        state["current"] = int(args[1])
    else:
        tab = int(args[1]) if len(args) > 1 else state["current"]
        if tab is None:
            raise Failure("There's no current tab")
        if tab not in opened:
            raise Failure(
                f"Tab {tab} isn't one you opened, so it stays open. Switch away with "
                "`tab select <id>`"
            )
        del histories[tab]
        opened.discard(tab)
        if state["current"] == tab:
            state["current"] = None


def roots() -> str:
    print(json.dumps({"jsonrpc": "2.0", "id": "roots-1", "method": "roots/list"}), flush=True)
    return json.loads(sys.stdin.readline())["result"]["roots"][0]["uri"]


def step(line: str, notes: list[str], printed: list[str], images: list[dict]) -> None:
    name, *args = words(line)
    if name == "goto":
        if state["current"] is None:
            create(None)
        load(normalize(args[0]))
    elif name == "back":
        if len(history()) < 2:
            raise Failure("There's no page to go back to")
        history().pop()
    elif name == "tab":
        tab_step(args, notes, printed)
    elif name == "browser":
        printed.append('* b1 "Work" \u2014 3 tabs')
    elif name == "help":
        printed.append("goto <url>")
    elif name == "upload":
        printed.append(f"root {roots()}")
    elif name == "crash":
        sys.exit(3)
    else:
        url = history()[-1]
        if name == "click" and args[0] == "missing":
            raise Failure("No element matches missing")
        if name == "get":
            printed.append({"url": url, "title": title(url), "text": f"text of {url}"}[args[0]])
        elif name == "screenshot":
            images.append({"type": "image", "data": "aW1hZ2U=", "mimeType": "image/jpeg"})
            notes.append("2\u00d72 JPEG")
        elif name in ("snapshot", "eval"):
            printed.append(f"{name} of {url}")
    target = control().get("moves", {}).get(name)
    if target and control().get("late"):
        state["late"] = (state["current"], target)
    elif target:
        history().append(target)
        if name == "eval":
            printed[-1] = f"eval of {target}"


def call(commands: str) -> dict:
    if error := control().get("error"):
        return {"content": [{"type": "text", "text": error}], "isError": True}
    if state["late"]:
        tab, url = state["late"]
        histories[tab].append(url)
        state["late"] = None
    lines = [line.strip() for line in re.split(r"\r?\n", commands)]
    lines = [line for line in lines if line and not line.startswith("#")]
    invalid = [
        f"\u2717 step {number}: {line.split()[0]} \u2014 Unknown command {line.split()[0]}. "
        f"Commands: {', '.join(COMMANDS)}"
        for number, line in enumerate(lines, 1)
        if line.split()[0] not in COMMANDS
    ]
    if invalid:
        text = "\n".join([*invalid, "Nothing ran: fix this line and send the call again"])
        return {"content": [{"type": "text", "text": text}], "isError": True}
    start, out, images, failed = info(), [], [], False
    for number, line in enumerate(lines, 1):
        notes: list[str] = []
        printed: list[str] = []
        try:
            step(line, notes, printed, images)
        except Failure as failure:
            failed = True
            out.extend(printed)
            out.append(f"\u2717 step {number}: {line} \u2014 {failure}")
            if number < len(lines):
                out.append(f"Steps {number + 1}\u2013{len(lines)} not run")
            break
        out.append(f"\u2713 {line}" + (f" \u2014 {'; '.join(notes)}" if notes else ""))
        out.extend(printed)
    end = info()
    if end and (failed or end != start):
        out.append(f"Page: {end[1]} \u2014 {json.dumps(end[2])}")
    result = {"content": [{"type": "text", "text": "\n".join(out)}, *images]}
    return {**result, "isError": True} if failed else result


for received in sys.stdin:
    message = json.loads(received)
    method = message.get("method")
    if "id" not in message:
        with (folder / "notifications").open("a") as notifications:
            notifications.write(f"{method}\n")
        continue
    if method == "initialize":
        answer = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Browser MCP", "version": "0"},
            "instructions": "Upstream instructions: act in batches.",
        }
    elif method == "tools/list":
        answer = {"tools": [{"name": "browser", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        with (folder / "log").open("a") as log:
            log.write(json.dumps(message["params"]["arguments"]["commands"]) + "\n")
        answer = call(message["params"]["arguments"]["commands"])
    else:
        answer = {"answered": method}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": answer}), flush=True)
