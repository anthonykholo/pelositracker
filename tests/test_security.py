from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.notify import validate_webhook_url
from app.security import AuthManager, SlidingWindowLimiter
from app.settings import Settings


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])


def test_sessions_expire_and_revoke_individually():
    manager = AuthManager.from_plaintext({"alice": "correct"}, ttl_seconds=60)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    first = manager.login("alice", "correct", as_of=now)
    second = manager.login("alice", "correct", as_of=now)
    assert first and second and first[0] != second[0]
    manager.revoke(first[0])
    assert manager.verify(first[0], as_of=now) is None
    assert manager.verify(second[0], as_of=now) is not None
    assert manager.verify(second[0], as_of=now + timedelta(seconds=61)) is None


def test_session_idle_timeout_is_sliding_but_never_exceeds_absolute_expiry():
    manager = AuthManager.from_plaintext(
        {"alice": "correct"}, ttl_seconds=60, idle_seconds=10
    )
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    login = manager.login("alice", "correct", as_of=now)
    assert login is not None
    assert manager.verify(login[0], as_of=now + timedelta(seconds=9)) is not None
    assert manager.verify(login[0], as_of=now + timedelta(seconds=18)) is not None
    assert manager.verify(login[0], as_of=now + timedelta(seconds=29)) is None


def test_rate_limiter_fails_closed_at_limit():
    limiter = SlidingWindowLimiter(2, 10)
    assert limiter.allow("client", now=0)
    assert limiter.allow("client", now=1)
    assert not limiter.allow("client", now=2)
    assert limiter.allow("client", now=11)


def test_rate_limiter_evicts_fully_drained_keys():
    limiter = SlidingWindowLimiter(2, 10)
    assert limiter.allow("a", now=0)
    assert limiter.allow("b", now=0)
    assert set(limiter._attempts) == {"a", "b"}
    # A later request past both keys' windows triggers a purge of drained keys;
    # a key with live hits would be untouched (the fails-closed test still holds).
    assert limiter.allow("c", now=100)
    assert set(limiter._attempts) == {"c"}


def test_expired_sessions_are_swept_on_login_not_left_to_accumulate():
    manager = AuthManager.from_plaintext({"alice": "correct"}, ttl_seconds=60)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    first = manager.login("alice", "correct", as_of=now)
    assert first is not None
    # A later login (after the first has expired) sweeps the dead session instead
    # of leaving it resident until its token is next verified.
    later = manager.login("alice", "correct", as_of=now + timedelta(seconds=61))
    assert later is not None
    assert len(manager._sessions) == 1
    assert manager.verify(first[0], as_of=now + timedelta(seconds=61)) is None
    assert manager.verify(later[0], as_of=now + timedelta(seconds=61)) is not None


def test_production_rejects_default_credentials_and_multiple_workers():
    with pytest.raises(ValueError, match="credentials"):
        Settings.from_env({"APP_ENV": "production"})
    with pytest.raises(ValueError, match="WEB_CONCURRENCY"):
        Settings.from_env({"APP_ENV": "production", "AUTHORIZED_USERS": "a:b",
                           "WEB_CONCURRENCY": "2"})


def test_webhooks_reject_non_https_and_unlisted_hosts_before_request():
    async def scenario():
        with pytest.raises(ValueError):
            await validate_webhook_url("http://127.0.0.1/internal")
        with pytest.raises(ValueError, match="allowlisted"):
            await validate_webhook_url("https://example.invalid/hook")
        with pytest.raises(ValueError):
            await validate_webhook_url("https://discord.com:444/api/webhooks/example")
        with pytest.raises(ValueError):
            await validate_webhook_url("https://discord.com/api/webhooks/example#internal")
    asyncio.run(scenario())


def test_mutation_requires_csrf_and_security_headers_are_strict():
    with TestClient(app) as client:
        login = client.post("/api/login", data={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        denied = client.post("/api/events", json={
            "name": "A vs B", "sport": "basketball", "home": "A", "away": "B"
        })
        assert denied.status_code == 403
        headers = client.get("/").headers
        assert "'unsafe-inline'" not in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"


def test_static_pages_have_no_inline_code_handlers_styles_or_duplicate_ids():
    with TestClient(app) as client:
        for path in ("/", "/watch"):
            html = client.get(path).text
            lowered = html.casefold()
            assert "<style" not in lowered
            assert "<script>" not in lowered
            assert "onclick=" not in lowered
            assert "style=" not in lowered
            parser = IdParser()
            parser.feed(html)
            assert len(parser.ids) == len(set(parser.ids))
