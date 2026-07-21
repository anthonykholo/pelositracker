from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.domain.time import assess_provider_timestamp
from app.gameclock import game_progress, validate_state_transition
from app.models import Event
from app.settings import Settings
from app.sources import _game_state_from_sports_payload, _quote_from_book, odds_api_quotes


FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_settings_are_typed_and_undocumented_providers_default_off():
    settings = Settings.from_env({})
    assert settings.odds_poll_seconds == 45
    assert settings.enable_action_network is False
    assert settings.enable_pinnacle_guest is False
    assert settings.enable_independent_models is False
    assert settings.independent_model_artifact is None

    configured = Settings.from_env({
        "INDEPENDENT_MODEL_ARTIFACT": "artifacts/nba.json",
        "CALIBRATION_ARTIFACT": "artifacts/consensus.json",
    })
    assert configured.independent_model_artifact == Path("artifacts/nba.json")
    assert configured.calibration_artifact == Path("artifacts/consensus.json")


def test_pinnacle_feature_requires_operator_credential():
    with pytest.raises(ValueError, match="PINNACLE_GUEST_API_KEY"):
        Settings.from_env({"ENABLE_PINNACLE_GUEST": "true"})


def test_provider_timestamp_never_falls_back_to_receipt_time():
    now = datetime(2026, 7, 20, 19, 12, 35, tzinfo=timezone.utc)
    missing = assess_provider_timestamp(None, as_of=now, max_age_seconds=120)
    future = assess_provider_timestamp(now + timedelta(seconds=10), as_of=now,
                                       max_age_seconds=120)
    assert not missing.trusted and "unavailable" in missing.reason
    assert not future.trusted and "future" in future.reason


def test_polymarket_book_golden_fixture_preserves_hash_depth_and_provider_time():
    event = Event("Knicks vs Celtics", "basketball", "New York Knicks", "Boston Celtics")
    meta = {"market": "moneyline", "outcome": event.home, "accepting_orders": True}
    quote = _quote_from_book(event, "token-home", meta, fixture("polymarket_book.json"))
    assert quote is not None
    assert quote.provider_timestamp == datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)
    assert quote.book_hash == "fixture-book-hash"
    assert quote.depth_complete is True
    assert quote.ask == .56 and quote.ask_size == 80


def test_odds_api_golden_fixture_preserves_book_family_and_market_update_time():
    event = Event("Knicks vs Celtics", "basketball", "New York Knicks", "Boston Celtics",
                  odds_api_event_id="odds-event-1")
    quotes = odds_api_quotes(event, fixture("odds_api_event.json"))
    assert len(quotes) == 2
    assert {quote.source_family for quote in quotes} == {"fixturebook"}
    assert all(quote.provider_timestamp == datetime(2026, 7, 20, 19, 12, 31,
                                                    tzinfo=timezone.utc)
               for quote in quotes)


def test_sports_state_fixture_verifies_score_orientation_and_time():
    event = Event("Knicks vs Celtics", "basketball", "New York Knicks", "Boston Celtics")
    state = _game_state_from_sports_payload(
        event, fixture("polymarket_sports_state.json"))
    assert state is not None and not state.quarantined
    assert (state.home_score, state.away_score) == (61, 58)
    assert state.provider_timestamp == datetime(2026, 7, 20, 19, 12, 32,
                                                tzinfo=timezone.utc)


def test_unknown_clock_and_regressions_fail_closed():
    assert game_progress("basketball", "Q2", "", "nba") == (None, None)
    assert game_progress("soccer", "2H", "75:00", "epl")[1] == pytest.approx(1 / 6)
    invalid = validate_state_transition(
        sport="basketball", period="Q3", clock="09:00", home_score=59,
        away_score=58, previous_period="Q3", previous_clock="08:30",
        previous_home_score=61, previous_away_score=58,
    )
    assert not invalid.valid and "score regressed" in invalid.reason
