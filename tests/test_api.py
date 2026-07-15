from fastapi.testclient import TestClient

from app.main import app, store
from app.models import Quote


def test_registered_event_can_be_removed():
    with TestClient(app) as client:
        created = client.post("/api/demo")
        assert created.status_code == 201
        event_id = created.json()["event"]["id"]

        removed = client.delete(f"/api/events/{event_id}")
        assert removed.status_code == 204
        assert event_id not in store.events
        assert event_id not in store.states
        assert event_id not in store.quotes
        assert event_id not in store.signals


def test_dashboard_contains_merged_ui_behaviors():
    with TestClient(app) as client:
        html = client.get("/").text
        assert "data-remove-event" in html
        assert "details[open][data-detail-key]" in html
        assert "Paste Polymarket link" in html
        assert "data-save-position" in html
        assert "Entry margin" in html


def test_position_can_be_saved_and_removed_for_a_visible_selection():
    with TestClient(app) as client:
        created = client.post("/api/demo").json()
        event_id = created["event"]["id"]
        store.add_quotes([Quote(event_id, "moneyline", "home", .52, "Polymarket",
                                bid=.51, ask=.53, token_id="token-1")])
        saved = client.put(f"/api/events/{event_id}/positions", json={
            "token_id": "token-1", "market": "moneyline", "outcome": "home",
            "shares": 20, "avg_entry_price": .48,
        })
        assert saved.status_code == 200
        assert saved.json()["positions"][0]["advice"] in {
            "HOLD", "HOLD / MONITOR", "CONSIDER CASH", "EXIT WATCH"
        }
        removed = client.delete(f"/api/events/{event_id}/positions/token-1")
        assert removed.status_code == 204
