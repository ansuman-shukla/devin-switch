import json

import pytest

from devin_switch import cli, real_browser
from devin_switch.store import Store, SwitchError

ALLOWED = ["example.com", "localhost"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/",
        "http://example.com/path?next=https://other.test/#top",
        "https://EXAMPLE.com:8443/a",
        "https://studio.example.com/watch?v=1",
        "https://a.b.example.com",
        "http://localhost:3000/",
        "about:blank",
    ],
)
def test_allowed_sites_cover_the_host_and_its_subdomains(url: str) -> None:
    assert real_browser.permitted(url, ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://notexample.com/",
        "https://example.com.evil.test/",
        "https://evil.test/example.com",
        "https://evil.test/?u=https://example.com",
        "https://evil.test#.example.com",
        "https://example.com@evil.test/",
        "https://user:pass@example.com/",
        "https://evil.test\\@example.com/",
        "https://evil.test%23.example.com/",
        "https://example.com%2eevil.test/",
        "https://example.com./",
        "https://example.com:/",
        "https://example.com:80@evil.test/",
        "https://example.com\t.evil.test/",
        "https://\u212a.example.com/",
        "https://[::1]/",
        "https://com/",
        "https:example.com",
        "//example.com/",
        "example.com",
        "ftp://example.com/",
        "file:///etc/hosts",
        "javascript:alert(1)//example.com",
        "data:text/html,https://example.com/",
        "view-source:https://example.com/",
        "chrome://settings/",
        "chrome-extension://example.com/popup.html",
        "blob:https://example.com/1",
        "about:blank#example.com",
        "about:srcdoc",
        "",
    ],
)
def test_other_hosts_schemes_and_look_alikes_are_not_allowed(url: str) -> None:
    assert not real_browser.permitted(url, ALLOWED)


def test_nothing_but_a_blank_tab_is_allowed_until_a_site_is_added() -> None:
    assert not real_browser.permitted("https://example.com/", [])
    assert real_browser.permitted("about:blank", [])


def run(store: Store, *arguments: str) -> int:
    return cli.execute(cli.parser().parse_args(["browser", *arguments]), store)


def test_owner_adds_lists_and_removes_sites(store: Store, capsys: pytest.CaptureFixture) -> None:
    assert run(store, "sites") == 0
    assert "No site is allowed yet" in capsys.readouterr().out
    assert real_browser.sites(store) == []

    assert run(store, "allow", "Studio.Example.COM", "https://example.org:8443/a?b#c") == 0
    assert "studio.example.com" in capsys.readouterr().out
    assert run(store, "allow", "example.org", "localhost:3000") == 0
    assert real_browser.sites(store) == ["example.org", "localhost", "studio.example.com"]
    saved = json.loads((store.root / "browser/settings.json").read_text())
    assert saved["real_sites"] == ["example.org", "localhost", "studio.example.com"]

    capsys.readouterr()
    assert run(store, "sites") == 0
    assert capsys.readouterr().out == "example.org\nlocalhost\nstudio.example.com\n"

    assert run(store, "disallow", "http://EXAMPLE.org/", "never-added.test") == 0
    out = capsys.readouterr().out
    assert "no longer use example.org" in out and "never-added.test was not" in out
    assert real_browser.sites(store) == ["localhost", "studio.example.com"]


def test_allowing_sites_keeps_the_other_browser_settings(store: Store) -> None:
    real_browser.save_settings(store, installed=True, default="Profile 3")
    assert run(store, "allow", "example.com") == 0
    assert real_browser.settings(store) == {
        "installed": True,
        "default": "Profile 3",
        "real_sites": ["example.com"],
    }


@pytest.mark.parametrize(
    "value",
    ["", "   ", "https://", "*.example.com", "exa mple.com", "ftp://example.com", "file:///tmp/x"],
)
def test_allow_rejects_what_names_no_site(store: Store, value: str) -> None:
    with pytest.raises(SwitchError, match="is not a site"):
        run(store, "allow", "example.com", value)
    assert real_browser.sites(store) == []
