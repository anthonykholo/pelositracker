from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


@dataclass(slots=True)
class Session:
    username: str
    csrf_token: str
    expires_at: datetime
    idle_expires_at: datetime


class AuthManager:
    """Argon2 credentials plus hashed, expiring, individually revocable sessions."""

    def __init__(self, users: dict[str, str], *, ttl_seconds: int = 8 * 3600,
                 idle_seconds: int = 30 * 60, max_sessions: int = 10_000):
        self._passwords = dict(users)
        self._ttl = ttl_seconds
        self._idle = min(idle_seconds, ttl_seconds)
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._hasher = PasswordHasher()

    @classmethod
    def from_plaintext(cls, users: dict[str, str], *, ttl_seconds: int = 8 * 3600,
                       idle_seconds: int = 30 * 60, max_sessions: int = 10_000):
        hasher = PasswordHasher()
        return cls({username: hasher.hash(password) for username, password in users.items()},
                   ttl_seconds=ttl_seconds, idle_seconds=idle_seconds,
                   max_sessions=max_sessions)

    def _purge_expired(self, now: datetime) -> None:
        """Drop sessions past their absolute or idle expiry (caller holds the
        lock). Without this the store grows with every login until each token
        happens to be re-verified after expiry; the sweep bounds it to live
        sessions. A generous ``max_sessions`` ceiling then evicts the
        soonest-to-expire as an anti-abuse backstop."""
        expired = [digest for digest, session in self._sessions.items()
                   if session.expires_at <= now or session.idle_expires_at <= now]
        for digest in expired:
            del self._sessions[digest]
        overflow = len(self._sessions) - self._max_sessions
        if overflow > 0:
            for digest, _ in sorted(
                self._sessions.items(), key=lambda item: item[1].expires_at
            )[:overflow]:
                del self._sessions[digest]

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def login(self, username: str, password: str, *, as_of: datetime | None = None) \
            -> tuple[str, Session] | None:
        encoded = self._passwords.get(username)
        if encoded is None:
            self._hasher.hash(password or "missing")
            return None
        try:
            self._hasher.verify(encoded, password)
        except VerifyMismatchError:
            return None
        now = as_of or datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)
        session = Session(username, secrets.token_urlsafe(24),
                          now + timedelta(seconds=self._ttl),
                          now + timedelta(seconds=self._idle))
        with self._lock:
            self._purge_expired(now)
            self._sessions[self._digest(token)] = session
        return token, session

    def verify(self, token: str | None, *, as_of: datetime | None = None) -> Session | None:
        if not token:
            return None
        now = as_of or datetime.now(timezone.utc)
        digest = self._digest(token)
        with self._lock:
            session = self._sessions.get(digest)
            if session is not None and (session.expires_at <= now
                                        or session.idle_expires_at <= now):
                self._sessions.pop(digest, None)
                return None
            if session is not None:
                session.idle_expires_at = min(
                    session.expires_at, now + timedelta(seconds=self._idle)
                )
            return session

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._digest(token), None)


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float,
                 *, purge_interval_seconds: float | None = None):
        self.limit = limit
        self.window_seconds = window_seconds
        # One bounded FIFO of hit timestamps per key. Expired hits are dropped
        # from the left in place rather than rebuilding the whole list on every
        # call, which removes avoidable allocation churn on this hot path while
        # keeping identical sliding-window semantics.
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        # Periodically evict keys whose window has fully drained so the map stays
        # bounded to keys active within the window instead of retaining one
        # (eventually empty) deque per key ever seen. Default cadence is one
        # window. Keys with live hits are never touched, so decisions are unchanged.
        self._purge_interval = (window_seconds if purge_interval_seconds is None
                                else purge_interval_seconds)
        self._next_purge = 0.0

    def _purge(self, cutoff: float) -> None:
        stale = [key for key, hits in self._attempts.items()
                 if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._attempts[key]

    def allow(self, key: str, *, now: float | None = None) -> bool:
        instant = time.monotonic() if now is None else now
        cutoff = instant - self.window_seconds
        with self._lock:
            if instant >= self._next_purge:
                self._purge(cutoff)
                self._next_purge = instant + self._purge_interval
            attempts = self._attempts.get(key)
            if attempts is None:
                attempts = deque()
                self._attempts[key] = attempts
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.limit:
                return False
            attempts.append(instant)
            return True
