"""A stand-in for the GitHub CLI: answers from, and changes, the pull requests in a JSON file.

`FAKE_GH_HOME` holds `state.json` (what "GitHub" knows) and `calls.jsonl` (one line per run).
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

home = Path(os.environ["FAKE_GH_HOME"])
arguments = sys.argv[1:]
state = json.loads((home / "state.json").read_text())
call = {
    "args": arguments,
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "stdin": sys.stdin.read(),
    "path": os.environ["PATH"],
    "env": {
        key: os.environ.get(key) for key in ("GH_CONFIG_DIR", "GH_PROMPT_DISABLED", "NO_COLOR")
    },
}
with open(home / "calls.jsonl", "a") as log:
    log.write(json.dumps(call) + "\n")


def fail(message):
    sys.exit(message)


def save():
    scratch = home / f"state-{os.getpid()}.json"
    scratch.write_text(json.dumps(state))
    scratch.replace(home / "state.json")


def option(name):
    return arguments[arguments.index(name) + 1] if name in arguments else None


def pull(target):
    if target not in state["pulls"]:
        fail("GraphQL: Could not resolve to a PullRequest with that number.\nmore detail")
    return state["pulls"][target]


command = " ".join(arguments[:2])
if command in state["hangs"]:
    time.sleep(600)
while (gate := state["gates"].get(command)) and not Path(gate).exists():
    time.sleep(0.02)
if command in state["fails"]:
    fail(state["fails"][command])
target = arguments[2] if len(arguments) > 2 and not arguments[2].startswith("-") else None
if command == "pr view":
    if target is None:
        # The pull request of the branch checked out in this folder.
        target = state["branches"].get(Path.cwd().name) or fail("no pull requests found")
    elif target.isdigit():
        target = state["numbers"].get(target) or fail(f"no pull request #{target} here")
    found = pull(target)
    print(json.dumps({key: found[key] for key in option("--json").split(",")}))
elif command == "repo view":
    allowed = state["repos"].get(target) or fail("GraphQL: Could not resolve to a Repository.")
    print(json.dumps({key: allowed[key] for key in option("--json").split(",")}))
elif arguments[:1] == ["api"]:
    repo, _, compared = arguments[1].removeprefix("repos/").partition("/compare/")
    for url, found in state["pulls"].items():
        base, oid = quote(found["baseRefName"], safe=""), found["headRefOid"]
        if url.startswith(f"https://github.com/{repo}/pull/") and compared == f"{base}...{oid}":
            assert arguments[2:] == ["--jq", ".behind_by"]
            print(state["behind"][url])
            break
    else:
        fail("gh: Not Found (HTTP 404)")
elif command == "pr merge":
    found = pull(target)
    if found["state"] != "OPEN" or option("--match-head-commit") != found["headRefOid"]:
        fail("Pull request is not mergeable: the head commit changed")
    found["state"] = "MERGED"
    save()
elif command == "pr update-branch":
    pull(target)["headRefOid"] = "0" * 40
    state["behind"][target] = 0
    save()
elif command == "pr ready":
    pull(target)["isDraft"] = "--undo" in arguments
    save()
elif command == "run rerun":
    for found in state["pulls"].values():
        for check in found["statusCheckRollup"]:
            if f"/actions/runs/{target}/" in check.get("detailsUrl", ""):
                check.update(status="IN_PROGRESS", conclusion="")
    save()
else:
    fail(f"unknown command: {command}")
