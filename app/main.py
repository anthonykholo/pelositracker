from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Form
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .engine import SignalEngine
from . import __version__, backtest, shadow_eval
from .accounts import AccountBook, DEFAULT_STRATEGIES, line_type
from .tennis_model import game_prob_from_prematch, match_win_prob, parse_tennis_score
from .diagnostics import edge_health
from .advice import market_views, position_views
from .history import HistoryDB
from .ledger import Ledger
from .lines import pregame_priors
from .gameclock import validate_state_transition
from .models import Event, GameState, Quote, as_json
from .monitor_state import MonitorState
from .sources import (_odds_quota, extract_polymarket_slug, infer_polymarket_event,
                      odds_api_poll, polymarket_event,
                      polymarket_market_stream, polymarket_sports_events,
                      polymarket_sports_stream, sports_game_status)
from .actionnetwork import action_network_poll
from .pinnacle import pinnacle_poll
from .store import Store
from .settings import Settings
from .security import AuthManager, SlidingWindowLimiter
from .calibration import load_calibration
from .model_registry import load_independent_models
from .telemetry import runtime_telemetry
from .identity import (CanonicalEvent, MappingDecision, MappingStatus)
from .domain.time import parse_provider_timestamp
from .notify import notify_webhook


logger = logging.getLogger(__name__)

load_dotenv()
settings = Settings.from_env()
store = Store()
ledger: Ledger | None = None
account_book: AccountBook | None = None
history_db: HistoryDB | None = None
monitor_state: MonitorState | None = None
engine = SignalEngine(settings.confidence_threshold,
                      settings.edge_threshold,
                      settings.max_data_age_seconds,
                      kelly_fraction=settings.kelly_fraction,
                      enable_independent_model=settings.enable_independent_models)
calibration_artifact = load_calibration(settings.calibration_artifact)
if calibration_artifact is not None:
    engine.install_calibration(calibration_artifact)
independent_model_artifact = load_independent_models(settings.independent_model_artifact)
if independent_model_artifact is not None:
    engine.install_independent_models(independent_model_artifact)
tasks: dict[str, list[asyncio.Task]] = {}
_finalized: set[str] = set()
_terminal_events: dict[str, str] = {}  # event_id -> final | canceled | deleted | shutdown
_event_locks: dict[str, asyncio.Lock] = {}
_pregame: dict[str, dict] = {}  # event_id -> {"spread": home point, "total": line}, captured near tip
_subscribers: set[asyncio.Queue] = set()  # SSE clients for real-time dashboard pushes
_notification_tasks: set[asyncio.Task] = set()
_sports_status: dict[str, dict] = {}  # latest public Polymarket sport_result by event slug
_config_state = {"auto_monitor": False}

_auth_users_env = os.getenv("AUTHORIZED_USERS")
if _auth_users_env:
    AUTHORIZED_USERS = {}
    for pair in _auth_users_env.split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            AUTHORIZED_USERS[u.strip()] = p.strip()
else:
    AUTHORIZED_USERS = {
        os.getenv("ADMIN_USERNAME", "admin"): os.getenv("ADMIN_PASSWORD", "admin")
    }

auth_manager = AuthManager.from_plaintext(AUTHORIZED_USERS)
login_limiter = SlidingWindowLimiter(10, 5 * 60)
api_limiter = SlidingWindowLimiter(300, 60)


def _cookie_name(base: str) -> str:
    return f"__Host-{base}" if settings.environment in {"production", "prod"} else base

async def verify_auth(request: Request):
    session = auth_manager.verify(request.cookies.get(_cookie_name("session_token")))
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        header = request.headers.get("x-csrf-token", "")
        cookie = request.cookies.get(_cookie_name("csrf_token"), "")
        if (not header or not cookie or not secrets.compare_digest(header, cookie)
                or not secrets.compare_digest(header, session.csrf_token)):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
    request.state.session = session

def _notify_subscribers() -> None:
    """Wake every SSE client that a snapshot changed (coalesced per client)."""
    for queue in list(_subscribers):
        if queue.empty():
            try:
                queue.put_nowait(1)
            except asyncio.QueueFull:
                pass


def _schedule_notification(payload: dict) -> None:
    task = asyncio.create_task(notify_webhook(payload["webhook_url"], payload))
    _notification_tasks.add(task)
    task.add_done_callback(_notification_tasks.discard)
_FINAL_STATUSES = {"final", "ended", "closed", "complete", "completed", "finished"}
_CANCELED_STATUSES = {"canceled", "cancelled", "abandoned", "void", "voided"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _terminal_kind(status: object) -> str | None:
    normalized = re.sub(r"[_-]+", " ", str(status or "").strip().casefold())
    if normalized in _CANCELED_STATUSES:
        return "canceled"
    if normalized in _FINAL_STATUSES:
        return "final"
    return None


def _event_lock(event_id: str) -> asyncio.Lock:
    return _event_locks.setdefault(event_id, asyncio.Lock())


async def _cancel_tasks(group: list[asyncio.Task]) -> None:
    current = asyncio.current_task()
    pending = [task for task in group if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _require_safe_id(value: str | None, field: str) -> None:
    # These are interpolated into outbound API paths; reject path/query injection.
    if value is not None and not _SAFE_ID.match(value):
        raise HTTPException(400, f"invalid {field}")


async def on_state(state: GameState):
    terminal = _terminal_kind(state.status)
    if terminal is not None:
        # Close the entry gate synchronously, before history I/O yields control
        # to quote callbacks that might otherwise place a known-result bet.
        _terminal_events.setdefault(state.event_id, terminal)
    event = store.events.get(state.event_id)
    previous_states = store.states.get(state.event_id)
    previous = previous_states[-1] if previous_states else None
    if terminal is None and event is not None and not state.quarantined:
        validation = validate_state_transition(
            sport=event.sport, league=event.league, period=state.period, clock=state.clock,
            home_score=state.home_score, away_score=state.away_score,
            previous_period=previous.period if previous else None,
            previous_clock=previous.clock if previous else None,
            previous_home_score=previous.home_score if previous else None,
            previous_away_score=previous.away_score if previous else None,
        )
        if not validation.valid:
            state.quarantined = True
            state.quarantine_reason = validation.reason
    store.add_state(state)
    if history_db is not None:
        try:
            await asyncio.to_thread(history_db.log_state, state)
        except Exception as exc:
            logger.warning("Could not persist state telemetry for %s: %s", state.event_id, exc)
    if terminal is not None:
        await finalize_event(state.event_id, canceled=terminal == "canceled")
    else:
        await record(state.event_id, as_of=state.processed_at)


async def on_sports_status(slug: str, payload: dict) -> None:
    """Cache authoritative live/final state for discovery without paid polling."""
    snapshot = dict(payload)
    snapshot["_received_at"] = datetime.now(timezone.utc).isoformat()
    _sports_status[slug] = snapshot
    raw_status = next((snapshot.get(key) for key in
                       ("status", "gameStatus", "game_status", "state")
                       if snapshot.get(key)), None)
    terminal = _terminal_kind(raw_status)
    matched = next((event for event in store.events.values()
                    if event.polymarket_slug == slug), None)
    if terminal is not None and matched is not None:
        # The status callback runs before score parsing in the shared sports
        # stream. Close entry immediately even if a malformed final score keeps
        # the subsequent GameState callback from being emitted.
        _terminal_events.setdefault(matched.id, terminal)
        if terminal == "canceled":
            await finalize_event(matched.id, canceled=True)
    normalized = sports_game_status(snapshot)
    if not _discover_cache.get("data") or normalized is None:
        return
    if normalized == "final":
        _discover_cache["data"] = [game for game in _discover_cache["data"]
                                   if game.get("slug") != slug]
        return
    for game in _discover_cache["data"]:
        if game.get("slug") == slug:
            game["status"] = normalized
            game["status_source"] = "polymarket-live-feed"


async def on_quotes(quotes: list[Quote]):
    store.add_quotes(quotes)
    event = store.events.get(quotes[0].event_id) if quotes else None
    if event is not None and monitor_state is not None and event.odds_api_event_id:
        # Background matching may resolve this ID after registration.
        await asyncio.to_thread(monitor_state.save_event, event)
    if history_db is not None and quotes:
        try:
            await asyncio.to_thread(history_db.log_quotes, quotes)
            odds_provider_ids = {
                q.provider_event_id for q in quotes
                if q.provider_event_id and not q.condition_id
                and q.provider_event_id == event.odds_api_event_id
            } if event is not None else set()
            if event is not None and event.canonical_event_id and odds_provider_ids:
                try:
                    start = parse_provider_timestamp(event.game_start)
                except (TypeError, ValueError, OverflowError):
                    start = None
                canonical = CanonicalEvent.create(
                    event.sport, event.league, start, event.home, event.away
                )
                for provider_id in odds_provider_ids:
                    await asyncio.to_thread(
                        history_db.log_event_identity, canonical,
                        MappingDecision(
                            "the-odds-api", provider_id, event.canonical_event_id,
                            MappingStatus.MAPPED, 1.0,
                            "shared matcher verified participants and start window",
                            orientation="direct",
                        ),
                    )
        except Exception as exc:
            logger.warning("Could not persist quote telemetry for %s: %s", quotes[0].event_id, exc)
    if quotes:
        await record(quotes[0].event_id, as_of=max(quote.processed_at for quote in quotes))


def _is_tennis(event: Event) -> bool:
    return (event.sport or "").strip().casefold() == "tennis"


def _tennis_side(outcome: str, event: Event) -> str | None:
    """Map a moneyline outcome label to home/away for a tennis match.

    Handles Polymarket's binary shape where one side is the player name and the
    other is the negated condition (``"Not <player>"``)."""
    label = (outcome or "").strip().casefold()
    home = (event.home or "").strip().casefold()
    away = (event.away or "").strip().casefold()
    if label in ("home", home):
        return "home"
    if label in ("away", away):
        return "away"
    if label.startswith("not "):
        remainder = label[4:].strip()
        if remainder in ("home", home):
            return "away"
        if remainder in ("away", away):
            return "home"
    return None


def _is_tennis_moneyline(market: str) -> bool:
    """True for either side of a tennis win market, including Polymarket's
    ``"moneyline condition"`` label on the negated binary outcome."""
    return line_type(market) == "moneyline" or (
        market or "").strip().casefold() == "moneyline condition"


def _tennis_score_now(event: Event) -> tuple[int, int, int, int] | None:
    """Parse the latest live tennis score cached from the sports feed."""
    status = _sports_status.get(event.polymarket_slug or "")
    if not status:
        return None
    event_state = status.get("eventState")
    source = event_state if isinstance(event_state, dict) else status
    score = str(source.get("score") or status.get("score") or "")
    period = str(source.get("period") or status.get("period") or "")
    if not score:
        return None
    return parse_tennis_score(score, period)


def _tennis_model_probabilities(event: Event, signals: list) -> dict[str, float]:
    """Independent in-play win probabilities per Polymarket token for a tennis
    match: pre-match anchor propagated through the live set/game score. Empty
    unless the model is enabled, the event is tennis, we captured a clean
    pre-match anchor, and a live score is available."""
    if not settings.enable_tennis_model or not _is_tennis(event):
        return {}
    parsed = _tennis_score_now(event)
    if parsed is None:
        return {}
    p0 = _pregame.get(event.id, {}).get("tennis_p0")
    if p0 is None or not 0 < p0 < 1:
        return {}
    sets_home, sets_away, games_home, games_away = parsed
    g = game_prob_from_prematch(p0)
    pm_home = match_win_prob(sets_home, sets_away, games_home, games_away, g)
    probabilities: dict[str, float] = {}
    for signal in signals:
        if not signal.token_id or not _is_tennis_moneyline(signal.market):
            continue
        side = _tennis_side(signal.outcome, event)
        if side is not None:
            probabilities[signal.token_id] = pm_home if side == "home" else 1.0 - pm_home
    return probabilities


def recompute(event_id: str, *, as_of: datetime) -> list:
    event = store.events.get(event_id)
    if event is None:  # event removed between emit and callback
        return []
    quotes = store.quotes[event_id]
    prior = _pregame.setdefault(event_id, {"spread": None, "total": None})
    if prior["spread"] is None or prior["total"] is None:
        spread, total = pregame_priors(quotes, event.home, event.away)
        prior["spread"] = prior["spread"] if prior["spread"] is not None else spread
        prior["total"] = prior["total"] if prior["total"] is not None else total
    signals = engine.evaluate(event_id, quotes, store.states[event_id], event.away,
                              sport=event.sport, league=event.league,
                              home_outcome=event.home,
                              pregame_spread=prior["spread"], pregame_total=prior["total"],
                              as_of=as_of, canonical_event_id=event.canonical_event_id)
    store.set_signals(event_id, signals)
    # Anchor the tennis model to the market's pre-match view: capture the home
    # win probability only while the match is at its start (score 0-0) or still
    # pregame. Joining mid-match yields no anchor, so we never fabricate one.
    if settings.enable_tennis_model and _is_tennis(event) and prior.get("tennis_p0") is None:
        parsed = _tennis_score_now(event)
        if parsed is None or parsed == (0, 0, 0, 0):
            home_signal = next(
                (signal for signal in signals
                 if _tennis_side(signal.outcome, event) == "home"
                 and 0 < (signal.model_probability or 0) < 1),
                None,
            )
            if home_signal is not None:
                prior["tennis_p0"] = float(home_signal.model_probability)
    return signals


async def record(event_id: str, *, as_of: datetime | None = None) -> None:
    async with _event_lock(event_id):
        if event_id in _terminal_events or event_id in _finalized:
            return
        decision_at = as_of or datetime.now(timezone.utc)
        signals = recompute(event_id, as_of=decision_at)
        _notify_subscribers()  # push the fresh snapshot to the dashboard immediately
        event = store.events.get(event_id)
        # Ledger commits fsync to disk; keep that off the event loop.
        if ledger is not None and event is not None and signals:
            await asyncio.to_thread(ledger.record_signals, event, signals)
        # A terminal state can arrive while the ledger write is in flight. It
        # closes the gate immediately; do not begin a new account entry after it.
        if event_id in _terminal_events or event_id in _finalized:
            return
        if account_book is not None and event is not None:
            quotes = store.quotes[event_id]
            exited_bets = await asyncio.to_thread(
                account_book.mark_and_cash_out, event, quotes, signals, as_of=decision_at
            )
            for paper_event in exited_bets:
                if paper_event.get("webhook_url"):
                    _schedule_notification(paper_event)
            if signals:
                model_probabilities = _tennis_model_probabilities(event, signals)
                placed_bets = await asyncio.to_thread(
                    account_book.place, event, signals, quotes, as_of=decision_at,
                    allow_uncalibrated=settings.allow_uncalibrated_paper,
                    model_probabilities=model_probabilities,
                )
                for paper_event in placed_bets:
                    if paper_event.get("webhook_url"):
                        _schedule_notification(paper_event)


def _winner_labels(event: Event, home_score: float, away_score: float) -> set[str]:
    if home_score > away_score:
        return {"home", event.home}
    if away_score > home_score:
        return {"away", event.away}
    return {"draw", "Draw"}  # a tie settles the Draw outcome, not nothing


async def finalize_event(event_id: str, *, canceled: bool = False) -> None:
    """Stop entry, snapshot/settle a final, or void a provider cancellation.

    Every write is idempotent. The in-memory finalized marker and persisted
    event deletion happen only after all writes succeed, so a transient failure
    can be retried by a later terminal update or process restart.
    """
    if event_id in _finalized:
        return
    if _terminal_events.get(event_id) == "canceled":
        canceled = True
    terminal = "canceled" if canceled else "final"
    _terminal_events.setdefault(event_id, terminal)
    async with _event_lock(event_id):
        if event_id in _finalized:
            return

        # Entry callbacks for this event have drained before this lock was
        # acquired. Cancel and await the infinite feed loops before settlement.
        await _cancel_tasks(tasks.pop(event_id, []))

        event = store.events.get(event_id)
        states = store.states.get(event_id) or []
        # The close mark is the last valid observation recorded before the
        # terminal gate closed. Never synthesize a fresh consensus after suspension.
        winners = _winner_labels(event, states[-1].home_score, states[-1].away_score) \
            if (event and states and not canceled) else set()

        def _writes():
            if canceled:
                if ledger is not None:
                    ledger.void_event(event_id, status="canceled")
                if account_book is not None:
                    account_book.void_event(event_id)
                return
            if ledger is not None:
                ledger.snapshot_closing(event_id)
                if winners:
                    ledger.settle_moneyline(event_id, winners)
            if account_book is not None and event is not None and states:
                account_book.settle(event, states[-1].home_score, states[-1].away_score)
            if history_db is not None and event is not None:
                prior = _pregame.get(event_id, {})
                final_state = states[-1] if states else None
                history_db.log_outcome(event, prior.get("spread"), prior.get("total"), final_state)

        await asyncio.to_thread(_writes)
        if monitor_state is not None:
            await asyncio.to_thread(monitor_state.delete_event, event_id)
        _finalized.add(event_id)
        _terminal_events[event_id] = "canceled" if canceled else "final"


async def auto_monitor_loop():
    while True:
        try:
            if _config_state["auto_monitor"]:
                games = await polymarket_sports_events(live_statuses=_sports_status)
                for game in games:
                    if game.get("status") == "live":
                        slug = game.get("slug")
                        if slug and not any(e.polymarket_slug == slug for e in store.events.values()):
                            try:
                                await add_event(EventIn(polymarket_url=f"https://polymarket.com/event/{slug}"))
                            except Exception as exc:
                                logger.warning("Auto-monitor could not add %s: %s", slug, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Auto-monitor discovery failed: %s", exc)
        await asyncio.sleep(60)


def _start_event_feeds(event: Event) -> None:
    group = []
    if event.polymarket_slug:
        group.append(asyncio.create_task(polymarket_market_stream(event, on_quotes)))
    if event.odds_api_sport:
        group.append(asyncio.create_task(odds_api_poll(event, on_quotes)))
        if settings.enable_action_network:
            group.append(asyncio.create_task(action_network_poll(event, on_quotes)))
        if settings.enable_pinnacle_guest:
            group.append(asyncio.create_task(pinnacle_poll(
                event, on_quotes, api_key=settings.pinnacle_guest_api_key)))
    tasks[event.id] = group


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ledger, account_book, history_db, monitor_state
    sports_task: asyncio.Task | None = None
    auto_task: asyncio.Task | None = None
    try:
        ledger = Ledger()
        account_book = AccountBook()
        history_db = HistoryDB()
        monitor_state = MonitorState()
        account_book.seed(DEFAULT_STRATEGIES)
        _config_state["auto_monitor"] = monitor_state.auto_monitor(False)
        for event in monitor_state.events():
            store.add_event(event)
            _finalized.discard(event.id)
            _terminal_events.pop(event.id, None)
            _start_event_feeds(event)
        sports_task = asyncio.create_task(polymarket_sports_stream(
            lambda: list(store.events.values()), on_state, on_sports_status))
        auto_task = asyncio.create_task(auto_monitor_loop())
        yield
    finally:
        # Close the entry gate, then let any already-running record() section
        # finish before canceling feed tasks and closing database connections.
        for event_id in list(store.events):
            _terminal_events.setdefault(event_id, "shutdown")
        for lock in list(_event_locks.values()):
            async with lock:
                pass
        background = [task for group in tasks.values() for task in group]
        tasks.clear()
        if sports_task is not None:
            background.append(sports_task)
        if auto_task is not None:
            background.append(auto_task)
        background.extend(_notification_tasks)
        _notification_tasks.clear()
        await _cancel_tasks(background)

        for database_store in (ledger, account_book, history_db, monitor_state):
            if database_store is not None:
                try:
                    await asyncio.to_thread(database_store.close)
                except Exception as exc:
                    logger.warning("Could not close %s cleanly: %s",
                                   type(database_store).__name__, exc)
        ledger = None
        account_book = None
        history_db = None
        monitor_state = None
        with store.lock:
            store.events.clear()
            store.states.clear()
            store.quotes.clear()
            store.signals.clear()
        _pregame.clear()
        _finalized.clear()
        _terminal_events.clear()
        _event_locks.clear()


app = FastAPI(title="Live Sports Signal Monitor", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    client = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api/") and not api_limiter.allow(client):
        return Response(status_code=429, content="rate limit exceeded")
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self'; img-src 'self' data:; connect-src 'self' "
        "https://*.polymarket.com wss://*.polymarket.com; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    if settings.environment in {"production", "prod"}:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class EventIn(BaseModel):
    polymarket_url: str | None = None
    name: str | None = None
    sport: str | None = None
    home: str | None = None
    away: str | None = None
    league: str = ""
    polymarket_slug: str | None = None
    odds_api_sport: str | None = None
    odds_api_event_id: str | None = None
    game_start: str | None = None


class PositionIn(BaseModel):
    token_id: str
    market: str
    outcome: str
    shares: float = Field(gt=0, le=1_000_000)
    avg_entry_price: float = Field(gt=0, lt=1)


class StrategyIn(BaseModel):
    name: str
    edge_threshold: float = 0.03
    sizing: str = "kelly"
    kelly_multiplier: float = 1.0
    flat_stake: float = 100.0
    start_bankroll: float = 10000.0
    webhook_url: str = ""
    cash_out_enabled: bool = False


class StrategyUpdateIn(BaseModel):
    cash_out_enabled: bool


class ConfigIn(BaseModel):
    auto_monitor: bool


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/watch")
async def watch():
    return FileResponse(Path(__file__).parent / "static" / "watch.html")


@app.get("/api/health")
async def health():
    return {"status": "live", "version": __version__}


@app.get("/api/ready")
async def ready():
    dependencies = {
        "ledger": ledger is not None,
        "accounts": account_book is not None,
        "history": history_db is not None,
        "monitor_state": monitor_state is not None,
        "native_engine": engine is not None,
    }
    if not all(dependencies.values()):
        raise HTTPException(status_code=503, detail={"status": "not_ready",
                                                     "dependencies": dependencies})
    return {"status": "ready", "dependencies": dependencies,
            "tracked_events": len(store.events), "background_groups": len(tasks)}


@app.get("/api/runtime", dependencies=[Depends(verify_auth)])
async def runtime_status():
    return {
        "counters": runtime_telemetry.snapshot(),
        "odds_api_quota": dict(_odds_quota),
        "tracked_events": len(store.events),
        "feed_groups": {event_id: len(group) for event_id, group in tasks.items()},
        "notifications_in_flight": len(_notification_tasks),
    }


@app.post("/api/login")
async def login(request: Request, response: Response, username: str = Form(...),
                password: str = Form(...)):
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    authenticated = auth_manager.login(username, password)
    if authenticated:
        token, session = authenticated
        secure = settings.environment in {"production", "prod"}
        response.set_cookie(key=_cookie_name("session_token"), value=token, httponly=True,
                            secure=secure, samesite="strict", path="/")
        response.set_cookie(key=_cookie_name("csrf_token"), value=session.csrf_token, httponly=False,
                            secure=secure, samesite="strict", path="/")
        return {"status": "ok", "csrf_token": session.csrf_token,
                "expires_at": session.expires_at.isoformat()}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/logout", dependencies=[Depends(verify_auth)])
async def logout(request: Request, response: Response):
    auth_manager.revoke(request.cookies.get(_cookie_name("session_token")))
    response.delete_cookie(_cookie_name("session_token"), path="/")
    response.delete_cookie(_cookie_name("csrf_token"), path="/")
    return {"status": "ok"}


@app.get("/api/config", dependencies=[Depends(verify_auth)])
async def config():
    return {"confidence_threshold": engine.confidence_threshold, "edge_threshold": engine.edge_threshold,
            "max_age_seconds": engine.max_age_seconds, "auto_monitor": _config_state["auto_monitor"]}


@app.post("/api/config", dependencies=[Depends(verify_auth)])
async def update_config(payload: ConfigIn):
    _config_state["auto_monitor"] = payload.auto_monitor
    if monitor_state is not None:
        await asyncio.to_thread(monitor_state.set_auto_monitor, payload.auto_monitor)
    return await config()


_discover_cache: dict = {"at": 0.0, "data": []}


@app.get("/api/discover", dependencies=[Depends(verify_auth)])
async def discover():
    """Browse live/upcoming Polymarket sports games to add without a link."""
    now = time.monotonic()
    if _discover_cache["data"] and now - _discover_cache["at"] < 45:
        return _discover_cache["data"]  # cache so browsing doesn't hammer Gamma
    try:
        games = await polymarket_sports_events(live_statuses=_sports_status)
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Polymarket: {exc}") from exc
    _discover_cache.update(at=now, data=games)
    return games


def _sort_events_by_edge():
    events = list(store.events.values())
    def max_edge(event):
        signals = store.signals.get(event.id, [])
        return max((s.edge for s in signals if s.edge is not None), default=0.0)
    events.sort(key=max_edge, reverse=True)
    return events

@app.get("/api/events", dependencies=[Depends(verify_auth)])
async def list_events():
    return await asyncio.gather(*(event_view(event.id) for event in _sort_events_by_edge()))


async def _events_snapshot_sse() -> str:
    views = await asyncio.gather(*(event_view(event.id) for event in _sort_events_by_edge()))
    payload = json.dumps(views, default=str)
    return f"data: {payload}\n\n"


@app.get("/api/stream", dependencies=[Depends(verify_auth)])
async def stream():
    """Server-Sent Events: push the events snapshot the instant data changes."""
    async def generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        _subscribers.add(queue)
        try:
            yield await _events_snapshot_sse()  # initial state
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                    yield await _events_snapshot_sse()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # keep the connection warm
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def event_view(event_id: str):
    event = store.events.get(event_id)
    if not event:
        raise HTTPException(404, "event not found")
    signals = store.signals[event_id]
    positions = (await asyncio.to_thread(ledger.event_positions, event_id)
                 if ledger is not None else [])
    return {"event": as_json(event),
            "latest_state": as_json(store.states[event_id][-1]) if store.states[event_id] else None,
            "signals": as_json(signals),
            "edge_health": edge_health(store.quotes[event_id], signals, engine.max_age_seconds),
            "actionable_markets": market_views(store.quotes[event_id], signals, engine.edge_threshold),
            "positions": position_views(positions, store.quotes[event_id], signals,
                                          engine.confidence_threshold),
            "state_points": len(store.states[event_id]),
            "quote_points": len(store.quotes[event_id])}


@app.get("/api/events/{event_id}/history", dependencies=[Depends(verify_auth)])
async def get_event_history_api(event_id: str):
    if history_db is None:
        raise HTTPException(503, "History database not available")
    return await asyncio.to_thread(history_db.get_event_history, event_id)


@app.get("/api/events/{event_id}", dependencies=[Depends(verify_auth)])
async def get_event(event_id: str):
    return await event_view(event_id)


@app.post("/api/events", status_code=201, dependencies=[Depends(verify_auth)])
async def add_event(payload: EventIn):
    values = payload.model_dump()
    link_or_slug = payload.polymarket_url or payload.polymarket_slug
    if link_or_slug:
        try:
            slug = extract_polymarket_slug(link_or_slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not parse Polymarket link: {exc}") from exc
        _require_safe_id(slug, "polymarket slug")
        try:
            poly = await polymarket_event(slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not resolve Polymarket link: {exc}") from exc
        if not poly.get("active") or poly.get("closed"):
            raise HTTPException(400, "Polymarket event is not active")
        actionable = [market for market in poly.get("markets", [])
                      if market.get("active", True) and not market.get("closed", False)
                      and market.get("enableOrderBook", True) and market.get("acceptingOrders", False)
                      and market.get("clobTokenIds")]
        if not actionable:
            raise HTTPException(400, "This event has no markets currently accepting orders")
        inferred = infer_polymarket_event(poly)
        values.update({
            "polymarket_slug": slug,
            "polymarket_url": f"https://polymarket.com/event/{slug}",
            "polymarket_restricted": bool(poly.get("restricted", False)),
            "name": payload.name or inferred["name"],
            "sport": payload.sport or inferred["sport"],
            "home": payload.home or inferred["home"],
            "away": payload.away or inferred["away"],
            "odds_api_sport": payload.odds_api_sport or inferred["odds_api_sport"],
            "game_start": payload.game_start or inferred["game_start"],
        })
        # We now defer match_odds_api_event to the background polling task so the POST returns instantly.
    required = ("name", "sport", "home", "away")
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise HTTPException(400, f"Missing required fields: {', '.join(missing)}")
    _require_safe_id(values.get("odds_api_sport"), "odds_api_sport")
    _require_safe_id(values.get("odds_api_event_id"), "odds_api_event_id")
    tracked_slug = values.get("polymarket_slug")
    if tracked_slug and any(existing.polymarket_slug == tracked_slug
                            for existing in store.events.values()):
        raise HTTPException(409, "This Polymarket event is already being tracked")
    event = Event(**values)
    try:
        start_time = parse_provider_timestamp(event.game_start)
    except (TypeError, ValueError, OverflowError):
        start_time = None
    canonical = CanonicalEvent.create(event.sport, event.league, start_time,
                                      event.home, event.away)
    event.canonical_event_id = canonical.canonical_event_id
    if monitor_state is not None:
        await asyncio.to_thread(monitor_state.save_event, event)
    if history_db is not None:
        mapping = None
        if event.polymarket_slug:
            mapping = MappingDecision(
                "polymarket", event.polymarket_slug,
                canonical.canonical_event_id if start_time else None,
                MappingStatus.MAPPED if start_time else MappingStatus.QUARANTINED,
                1.0 if start_time else 0.5,
                "event slug and canonical participants/start" if start_time
                else "event start unavailable; provider mapping quarantined",
                orientation="direct",
            )
        await asyncio.to_thread(history_db.log_event_identity, canonical, mapping)
    store.add_event(event)
    _start_event_feeds(event)
    _notify_subscribers()
    return await event_view(event.id)


@app.put("/api/events/{event_id}/positions", dependencies=[Depends(verify_auth)])
async def save_position(event_id: str, payload: PositionIn):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
    if ledger is None:
        raise HTTPException(503, "position ledger is not ready")
    valid_tokens = {quote.token_id for quote in store.quotes[event_id]
                    if quote.source.casefold() == "polymarket" and quote.token_id}
    if payload.token_id not in valid_tokens:
        raise HTTPException(400, "That selection is not available for this event")
    await asyncio.to_thread(
        ledger.upsert_position, event_id, payload.token_id, payload.market,
        payload.outcome, payload.shares, payload.avg_entry_price,
    )
    _notify_subscribers()
    return await event_view(event_id)


@app.delete("/api/events/{event_id}/positions/{token_id}", status_code=204, dependencies=[Depends(verify_auth)])
async def remove_position(event_id: str, token_id: str):
    if ledger is None or not await asyncio.to_thread(
        ledger.delete_position, event_id, token_id
    ):
        raise HTTPException(404, "position not found")
    _notify_subscribers()


@app.get("/api/metrics", dependencies=[Depends(verify_auth)])
async def metrics():
    if ledger is None:
        return {"n_bets": 0, "n_settled": 0}
    bet_rows, decisions = await asyncio.gather(
        asyncio.to_thread(ledger.all_bets),
        asyncio.to_thread(ledger.all_decisions),
    )
    return await asyncio.to_thread(backtest.summary, bet_rows, decisions)


@app.get("/api/leaderboard", dependencies=[Depends(verify_auth)])
async def get_leaderboard():
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.leaderboard)


@app.get("/api/model-eval", dependencies=[Depends(verify_auth)])
async def model_eval(sport: str = "tennis"):
    """Calibration + market-baseline scorecard for model-backed paper bets.

    Shadow evaluation only: it reports whether the model's probabilities are
    calibrated and beat the executable price, and makes no profitability claim.
    """
    if account_book is None:
        return {}
    bets = await asyncio.to_thread(account_book.bets_for_eval, sport or None)
    return await asyncio.to_thread(shadow_eval.model_eval_report, bets, sport or None)


@app.post("/api/accounts", status_code=201, dependencies=[Depends(verify_auth)])
async def create_account(payload: StrategyIn):
    if account_book is None:
        raise HTTPException(503, "Account book is not initialized")
    from .accounts import Strategy
    strat = Strategy(
        name=payload.name,
        blurb="Custom bot created via UI.",
        edge_threshold=payload.edge_threshold,
        sizing=payload.sizing,
        kelly_multiplier=payload.kelly_multiplier,
        flat_stake=payload.flat_stake,
        start_bankroll=payload.start_bankroll,
        webhook_url=payload.webhook_url,
        cash_out_enabled=payload.cash_out_enabled,
    )
    await asyncio.to_thread(account_book.seed, [strat])
    return {"status": "ok"}


@app.get("/api/accounts/{name}/bets", dependencies=[Depends(verify_auth)])
async def get_account_bets(name: str):
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.account_bets, name)


@app.get("/api/accounts/{name}/marks", dependencies=[Depends(verify_auth)])
async def get_account_marks(name: str):
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.account_marks, name)


@app.patch("/api/accounts/{name}", dependencies=[Depends(verify_auth)])
async def update_account(name: str, payload: StrategyUpdateIn):
    if account_book is None:
        raise HTTPException(503, "Account book is not initialized")
    updated = await asyncio.to_thread(
        account_book.set_cash_out, name, payload.cash_out_enabled
    )
    if not updated:
        raise HTTPException(404, "paper bot not found")
    return {"name": name, "cash_out_enabled": payload.cash_out_enabled}


@app.get("/api/accounts/{name}/bets/{bet_id}/marks",
         dependencies=[Depends(verify_auth)])
async def get_account_bet_marks(name: str, bet_id: int):
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.bet_marks, name, bet_id)


@app.get("/api/bets", dependencies=[Depends(verify_auth)])
async def bets(event_id: str | None = None):
    if ledger is None:
        return []
    if event_id:
        return await asyncio.to_thread(ledger.event_bets, event_id)
    return await asyncio.to_thread(ledger.all_bets)


@app.delete("/api/events/{event_id}", status_code=204, dependencies=[Depends(verify_auth)])
async def delete_event(event_id: str):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
    # Manual removal is not evidence of a final result. Keep an event monitored
    # while any fake-money bot position is open so positions cannot disappear
    # into a misleading administrative void/refund.
    lock = _event_lock(event_id)
    async with lock:
        open_positions = (await asyncio.to_thread(account_book.open_count, event_id)
                          if account_book is not None else 0)
        if open_positions > 0:
            raise HTTPException(
                409,
                "This event has open paper-bot positions. Leave it monitored until "
                "they settle or cash out; only a provider cancellation may void them.",
            )
        _terminal_events.setdefault(event_id, "deleted")
        await _cancel_tasks(tasks.pop(event_id, []))
        if ledger is not None:
            await asyncio.to_thread(ledger.delete_event_positions, event_id)
        if monitor_state is not None:
            await asyncio.to_thread(monitor_state.delete_event, event_id)
        _finalized.discard(event_id)
        _pregame.pop(event_id, None)
        with store.lock:
            store.events.pop(event_id, None)
            store.states.pop(event_id, None)
            store.quotes.pop(event_id, None)
            store.signals.pop(event_id, None)
        _notify_subscribers()
    _terminal_events.pop(event_id, None)
    _event_locks.pop(event_id, None)


