import argparse
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/enroll_accounts.py"
spec = importlib.util.spec_from_file_location("enroll_accounts", SCRIPT)
enroll = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enroll)


def test_targets_map_plus_numbers_to_aliases_and_profiles() -> None:
    args = enroll.parser().parse_args(
        ["--email", "me@gmail.com", "--first", "27", "--last", "29", "--first-alias", "53"]
    )
    assert enroll.targets(args) == [
        enroll.Target(27, "ansuman-53", "me+27@gmail.com", "Plus 27"),
        enroll.Target(28, "ansuman-54", "me+28@gmail.com", "Plus 28"),
        enroll.Target(29, "ansuman-55", "me+29@gmail.com", "Plus 29"),
    ]


@pytest.mark.parametrize("base", ["me+1@gmail.com", "gmail.com", "@gmail.com"])
def test_plus_address_requires_a_plain_base(base: str) -> None:
    with pytest.raises(enroll.EnrollError):
        enroll.plus_address(base, 1)


def test_reversed_range_is_rejected() -> None:
    args = argparse.Namespace(
        email="me@gmail.com", first=5, last=4, first_alias=1, prefix="a", profile_prefix="P"
    )
    with pytest.raises(enroll.EnrollError):
        enroll.targets(args)


def test_login_url_survives_wrapping_and_styling() -> None:
    output = (
        b"Visit \x1b[1mhttps://app.devin.ai/auth/cli/continue?state=abc&prompt=select_\r\n"
        b"account&code_challenge=xyz&cli_pkce_marker=1\x1b[0m to sign in\r\nCode:"
    )
    assert enroll.login_url(output) == (
        "https://app.devin.ai/auth/cli/continue?state=abc&prompt=select_account"
        "&code_challenge=xyz&cli_pkce_marker=1"
    )
    assert enroll.login_url(b"Opening Plus 1. Code:") is None


def test_redact_hides_code_like_values() -> None:
    text = "Copy your login code\nabcdefghijklmnopqrstuvwxyz0123\nCopy code"
    assert "abcdefghij" not in enroll.redact(text)
    assert "Copy your login code" in enroll.redact(text)
