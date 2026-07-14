from app.models import Event
from app.sources import odds_api_quotes, odds_api_request


def event(**overrides):
    values = {
        "name": "Celtics at Knicks",
        "sport": "basketball",
        "home": "New York Knicks",
        "away": "Boston Celtics",
        "odds_api_sport": "basketball_nba",
    }
    values.update(overrides)
    return Event(**values)


def test_v4_request_uses_sport_path_and_query_key(monkeypatch):
    monkeypatch.setenv("ODDS_REGIONS", "us")
    url, params = odds_api_request(event(), "secret")
    assert url == "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    assert params["apiKey"] == "secret"
    assert params["regions"] == "us"
    assert params["oddsFormat"] == "american"


def test_event_request_uses_event_odds_endpoint():
    url, _ = odds_api_request(event(odds_api_event_id="game-123"), "secret")
    assert url.endswith("/events/game-123/odds")


def test_quotes_filter_matchup_and_keep_line_points():
    target = event()
    payload = [
        {
            "id": "other",
            "home_team": "Other Home",
            "away_team": "Other Away",
            "bookmakers": [{"title": "Wrong Book", "markets": []}],
        },
        {
            "id": "target",
            "home_team": "New York Knicks",
            "away_team": "Boston Celtics",
            "bookmakers": [{
                "title": "Example Book",
                "markets": [
                    {"key": "h2h", "outcomes": [{"name": "New York Knicks", "price": -120}]},
                    {"key": "spreads", "outcomes": [{"name": "Boston Celtics", "price": -110,
                                                       "point": 2.5}]},
                    {"key": "totals", "outcomes": [{"name": "Over", "price": 105,
                                                      "point": 221.5}]},
                ],
            }],
        },
    ]
    quotes = odds_api_quotes(target, payload)
    assert [quote.outcome for quote in quotes] == [
        "New York Knicks", "Boston Celtics +2.5", "Over 221.5"
    ]
    assert all(quote.source == "Example Book" for quote in quotes)
