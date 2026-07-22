from datetime import datetime, timedelta, timezone

from app.identity import (CanonicalEvent, CanonicalMarket, MappingStatus,
                          ProviderEventCandidate, resolve_event_mapping)


START = datetime(2026, 7, 20, 19, tzinfo=timezone.utc)


def test_canonical_identity_is_stable_across_case_and_punctuation():
    first = CanonicalEvent.create("Basketball", "NBA", START,
                                  "New York Knicks", "Boston Celtics")
    second = CanonicalEvent.create("basketball", "nba", START,
                                   "new-york knicks", "BOSTON CELTICS")
    assert first.canonical_event_id == second.canonical_event_id
    assert first.home.participant_id == second.home.participant_id


def test_opposite_spread_lines_get_distinct_market_identities():
    event = "event-1"
    minus = CanonicalMarket.create(event, "spread", -6.5)
    plus = CanonicalMarket.create(event, "spread", +6.5)
    even = CanonicalMarket.create(event, "spread", 0)
    assert minus.market_id != plus.market_id      # sign must not collapse
    # +6.5 and unsigned 6.5 are the same line and should agree.
    assert plus.market_id == CanonicalMarket.create(event, "spread", "+6.5").market_id
    assert len({minus.market_id, plus.market_id, even.market_id}) == 3


def test_resolver_rejects_same_teams_at_wrong_start():
    target = CanonicalEvent.create("basketball", "nba", START,
                                   "New York Knicks", "Boston Celtics")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("wrong", "New York Knicks", "Boston Celtics",
                               START + timedelta(days=1), "basketball", "nba")
    ])
    assert decision.status is MappingStatus.QUARANTINED
    assert decision.canonical_id is None


def test_resolver_quarantines_indistinguishable_doubleheader():
    target = CanonicalEvent.create("baseball", "mlb", START,
                                   "New York Yankees", "Boston Red Sox")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("game-1", "New York Yankees", "Boston Red Sox",
                               START, "baseball", "mlb"),
        ProviderEventCandidate("game-2", "New York Yankees", "Boston Red Sox",
                               START, "baseball", "mlb"),
    ])
    assert decision.status is MappingStatus.AMBIGUOUS


def test_manchester_and_team_variants_do_not_cross_match():
    target = CanonicalEvent.create("soccer", "epl", START,
                                   "Manchester United", "Arsenal")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("wrong-city", "Manchester City", "Arsenal",
                               START, "soccer", "epl"),
        ProviderEventCandidate("wrong-women", "Manchester United Women", "Arsenal Women",
                               START, "soccer", "epl"),
        ProviderEventCandidate("wrong-u21", "Manchester United U21", "Arsenal U21",
                               START, "soccer", "epl"),
    ])
    assert decision.status is MappingStatus.QUARANTINED


def test_reversed_orientation_is_recorded_not_silently_relabelled():
    target = CanonicalEvent.create("basketball", "nba", START,
                                   "New York Knicks", "Boston Celtics")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("reversed", "Boston Celtics", "New York Knicks",
                               START, "basketball", "nba")
    ])
    assert decision.status is MappingStatus.MAPPED
    assert decision.orientation == "reversed"
