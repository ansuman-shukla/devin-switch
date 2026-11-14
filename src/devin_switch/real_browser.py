"""Real mode: agents drive the owner's own Chrome through the Browser MCP extension.

Websites can tell the agent browser's headless copy is a robot, so real mode works in the
Chrome the owner is signed into. The owner keeps two locks: the extension's own Stop/Resume
button, and the site allowlist kept here, which starts empty.
"""

import re
from collections.abc import Collection

from devin_switch.agent_browser import save_settings, settings
from devin_switch.store import Store, SwitchError

ACTIONS = ("allow", "disallow", "sites")
BLANK = "about:blank"
# What JavaScript's trim() and \s count as white space: the upstream parser splits on these.
SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
WEB = re.compile(
    rf"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)*)(?::[0-9]+)?(?:[/?#][^{SPACE}]*)?",
    re.ASCII | re.IGNORECASE,
)
NO_SITES = "No site is allowed yet, so real mode opens nothing. Add one: ds browser allow SITE"


def web_host(url: str) -> str | None:
    """The host of a plain http(s) URL; None for every other scheme and anything unusual.

    Narrow on purpose (no user name, no escapes, only letters, digits, dots and hyphens in the
    host), so this reading of a URL and Chrome's can never name different hosts.
    """
    found = WEB.fullmatch(url)
    return found[1].lower() if found else None


def permitted(url: str, allowed: Collection[str]) -> bool:
    """Whether real mode may open, read and act on this URL: a blank tab or an allowed site."""
    if url == BLANK:
        return True
    host = web_host(url)
    return host is not None and any(host == site or host.endswith(f".{site}") for site in allowed)


def sites(store: Store) -> list[str]:
    value = settings(store).get("real_sites")
    return [site for site in value if isinstance(site, str)] if isinstance(value, list) else []


def named_site(value: str) -> str:
    """The host the owner named, given as a host or as a full address."""
    text = value.strip()
    host = web_host(text if "://" in text else f"https://{text}")
    if host is None:
        raise SwitchError(
            f"{value!r} is not a site. Name a host such as example.com (its subdomains are "
            "included) or paste an http(s) address."
        )
    return host


def command(args, store: Store) -> int:
    if args.action == "sites":
        print("\n".join(sites(store)) or NO_SITES)
        return 0
    named = list(dict.fromkeys(named_site(value) for value in args.sites))
    current = sites(store)
    if args.action == "allow":
        save_settings(store, real_sites=sorted({*current, *named}))
        for site in named:
            print(f"Real mode may use {site} and its subdomains.")
    else:
        save_settings(store, real_sites=[site for site in current if site not in named])
        for site in named:
            print(
                f"Real mode may no longer use {site}."
                if site in current
                else f"{site} was not an allowed site."
            )
    return 0
