from datetime import datetime, timedelta, timezone

from app.sources import _game_window, filter_sports_games


def _ev(title, tags, orderbook=True, accepting=True, slug=None):
    return {"title": title, "slug": slug or title.lower().replace(" ", "-"),
            "enableOrderBook": orderbook, "tags": [{"label": t} for t in tags],
            "markets": [{"acceptingOrders": accepting, "gameStartTime": "2026-07-15T18:00:00Z",
                         "clobTokenIds": ["x"]}]}


def test_keeps_tradeable_sports_matchups():
    events = [
        _ev("England vs. Argentina", ["Soccer", "World Cup"]),
        _ev("Lakers vs. Celtics", ["NBA"]),
    ]
    games = filter_sports_games(events)
    assert {g["title"] for g in games} == {"England vs. Argentina", "Lakers vs. Celtics"}
    assert all(g["slug"] and g["game_start"] for g in games)


def test_drops_futures_submarkets_untradeable_and_nonsports():
    events = [
        _ev("World Cup Winner", ["Soccer"]),                       # future, no "vs"
        _ev("England vs. Argentina - Player Props", ["Soccer"]),   # sub-market
        _ev("Lakers vs. Celtics", ["NBA"], orderbook=False),       # not tradeable
        _ev("Reds vs. Cubs", ["MLB"], accepting=False),            # not accepting orders
        _ev("Trump vs. Biden debate winner", ["Politics"]),        # not sports
    ]
    assert filter_sports_games(events) == []


def test_game_window_tags_live_upcoming_and_drops_stale():
    now = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    def g(name, start):
        return {"slug": name, "title": name, "game_start": start.isoformat()}

    games = [
        g("live", now - timedelta(hours=1)),        # 1h into the game -> live
        g("soon", now + timedelta(hours=3)),        # starts in 3h -> upcoming
        g("done", now - timedelta(hours=10)),       # finished long ago -> dropped
        g("far", now + timedelta(days=9)),          # >7 days out -> dropped
    ]
    result = _game_window(games, now)
    assert [x["slug"] for x in result] == ["live", "soon"]   # live sorted first
    assert result[0]["status"] == "live"
    assert result[1]["status"] == "upcoming"
