from fastapi.testclient import TestClient

from app.main import app, store


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
