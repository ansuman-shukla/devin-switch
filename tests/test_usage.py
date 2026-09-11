import json
import stat
import urllib.error
from dataclasses import asdict
from pathlib import Path

import pytest

from devin_switch import desktop, usage
from devin_switch.store import Store, SwitchError

NOW = 1_800_000_000.0


def response(**overrides: object) -> dict:
    return {
        "userStatus": {
            "email": "owner@example.invalid",
            "planStatus": {
                "planInfo": {"billingStrategy": "BILLING_STRATEGY_QUOTA", "planName": "Teams"},
                "dailyQuotaRemainingPercent": 75,
                "weeklyQuotaRemainingPercent": 22,
                "dailyQuotaResetAtUnix": str(int(NOW + 3600)),
                "weeklyQuotaResetAtUnix": str(int(NOW + 86400)),
                **overrides,
            },
        }
    }


def save_credentials(store: Store, name: str = "ansuman-1") -> Path:
    path = store.credentials(store.account(name))
    path.write_text(f'windsurf_api_key = "fake-secret-{name}"\n')
    return path


def test_percentage_direction_resets_identity_and_no_raw_response() -> None:
    data = response()
    data["userStatus"]["privateField"] = "do-not-cache"
    result = usage.parse_status(data, NOW)
    assert result.status == "ok"
    assert result.daily.used_percent == 25
    assert result.weekly.used_percent == 78
    assert result.daily.resets_at == NOW + 3600
    assert result.email == "owner@example.invalid"
    assert result.plan == "Teams"
    assert "do-not-cache" not in json.dumps(asdict(result))


def test_proto3_omitted_percent_is_exhausted_only_with_quota_plan_and_reset() -> None:
    data = response()
    del data["userStatus"]["planStatus"]["weeklyQuotaRemainingPercent"]
    assert usage.parse_status(data, NOW).weekly.used_percent == 100
    del data["userStatus"]["planStatus"]["weeklyQuotaResetAtUnix"]
    missing = usage.parse_status(data, NOW)
    assert missing.weekly.used_percent is None
    assert missing.status == "unavailable"
    result = usage.parse_status(
        response(planInfo={"billingStrategy": "BILLING_STRATEGY_CREDITS"}), NOW
    )
    assert result.daily.used_percent is None
    assert result.status == "unavailable"


def test_hidden_daily_quota_is_not_reported_as_zero_usage() -> None:
    result = usage.parse_status(
        response(planInfo={"billingStrategy": 2, "hideDailyQuota": True}), NOW
    )
    assert result.daily.state == "not_applicable"
    assert result.daily.used_percent is None
    assert result.weekly.used_percent == 78


@pytest.mark.parametrize("value", [-1, 101, "nan", "inf", True, {}, None])
def test_rejects_invalid_percentages(value: object) -> None:
    with pytest.raises(ValueError):
        usage.parse_status(response(dailyQuotaRemainingPercent=value), NOW)


def test_transport_destination_auth_timeout_and_redirect_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            assert limit == usage.MAX_RESPONSE_BYTES + 1
            return json.dumps(response()).encode()

    class Client:
        def open(self, request, *, timeout):
            assert request.full_url == usage.STATUS_URL
            assert request.get_header("Authorization") == "Bearer fake-only"
            assert json.loads(request.data)["metadata"]["apiKey"] == "fake-only"
            assert timeout == 15
            return Response()

    def opener(handler):
        assert (
            handler.redirect_request(None, None, 302, None, None, "https://untrusted.invalid")
            is None
        )
        return Client()

    monkeypatch.setattr(usage.urllib.request, "build_opener", opener)
    assert usage.fetch_status(b'windsurf_api_key="fake-only"') == response()
    with pytest.raises(SwitchError, match="custom API server"):
        usage.fetch_status(
            b'windsurf_api_key="fake-only"\napi_server_url="https://untrusted.invalid"'
        )


@pytest.mark.parametrize("code", [401, 403, 429, 500, 302])
def test_transport_errors_never_expose_response_or_credentials(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    class Client:
        def open(self, request, *, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "fake-secret", {}, None)

    monkeypatch.setattr(usage.urllib.request, "build_opener", lambda *_: Client())
    with pytest.raises(SwitchError) as exc:
        usage.fetch_status(b'windsurf_api_key="fake-secret"')
    assert "fake-secret" not in str(exc.value)


def test_cache_throttle_stale_errors_and_credential_change(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = save_credentials(store)
    account = store.account("ansuman-1")
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    monkeypatch.setattr(usage, "fetch_status", lambda _: response())
    assert usage.refresh_account(store, account).daily.used_percent == 25
    cache = store.directory(account.name) / "usage.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert "fake-secret" not in cache.read_text()

    def fail(_):
        raise SwitchError("Network unavailable")

    monkeypatch.setattr(usage, "fetch_status", fail)
    assert usage.refresh_account(store, account).status == "ok"  # Throttled; no request.
    assert usage.read_cached(store, account, now=NOW + 121).status == "stale"
    result = usage.refresh_account(store, account, force=True)
    assert result.status == "stale"
    assert result.daily.used_percent == 25
    assert result.fetched_at == NOW
    assert result.message == "Network unavailable"
    credentials.write_text('windsurf_api_key="different-account"')
    assert usage.read_cached(store, account).daily.used_percent is None
    credentials.unlink()
    assert usage.read_cached(store, account).status == "sign_in"


def test_refresh_handles_relogin_during_request_without_misattribution(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = save_credentials(store)

    def relogin(_):
        credentials.write_text('windsurf_api_key="new-identity"')
        return response()

    monkeypatch.setattr(usage, "fetch_status", relogin)
    result = usage.refresh_account(store, store.account("ansuman-1"))
    assert result.daily.used_percent is None
    assert not (credentials.parents[2] / "usage.json").exists()


def test_refresh_does_not_contact_service_for_unsigned_or_symlinked_login(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(_):
        pytest.fail("Unsigned account must not send an authenticated request")

    monkeypatch.setattr(usage, "fetch_status", forbidden)
    account = store.account("ansuman-1")
    assert usage.refresh_account(store, account).status == "sign_in"
    other = tmp_path / "other-token"
    other.write_text("private")
    store.credentials(account).symlink_to(other)
    assert usage.refresh_account(store, account).status == "sign_in"


def test_usage_refresh_works_while_cli_busy_and_isolates_account_errors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_credentials(store)
    save_credentials(store, "ansuman-2")
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    monkeypatch.setattr(desktop.browser, "profiles", lambda: ())

    def fetch(credentials):
        if b"ansuman-2" in credentials:
            raise SwitchError("Sign in again")
        return response()

    monkeypatch.setattr(usage, "fetch_status", fetch)
    with store.lock():
        result = desktop.action(store, {"action": "usage", "force": "true"})
    accounts = result["state"]["accounts"]
    assert accounts[0]["usage"]["daily"]["used_percent"] == 25
    assert accounts[1]["usage"]["status"] == "unavailable"
    assert result["state"]["busy"] is True


@pytest.mark.parametrize("elapsed", [59.9, 60.0, 60.1])
def test_forced_desktop_tick_reads_fresh_usage_near_cache_boundary(
    store: Store, monkeypatch: pytest.MonkeyPatch, elapsed: float
) -> None:
    save_credentials(store)
    monkeypatch.setattr(desktop.browser, "profiles", lambda: ())
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    monkeypatch.setattr(usage, "fetch_status", lambda _: response())
    first = desktop.action(store, {"action": "usage", "force": "true"})
    assert first["state"]["accounts"][0]["usage"]["daily"]["used_percent"] == 25
    monkeypatch.setattr(usage.time, "time", lambda: NOW + elapsed)
    monkeypatch.setattr(usage, "fetch_status", lambda _: response(dailyQuotaRemainingPercent=50))
    refreshed = desktop.action(store, {"action": "usage", "force": "true"})
    reading = refreshed["state"]["accounts"][0]["usage"]
    assert reading["daily"]["used_percent"] == 50
    assert reading["fetched_at"] == NOW + elapsed


def test_reset_boundary_marks_even_recent_cache_stale(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_credentials(store)
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    monkeypatch.setattr(usage, "fetch_status", lambda _: response(dailyQuotaResetAtUnix=NOW + 5))
    account = store.account("ansuman-1")
    usage.refresh_account(store, account)
    assert usage.read_cached(store, account, now=NOW + 6).status == "stale"
