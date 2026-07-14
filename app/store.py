from __future__ import annotations

from collections import defaultdict
from threading import RLock

from .models import Event, GameState, Quote, Signal


class Store:
    def __init__(self):
        self.events: dict[str, Event] = {}
        self.states: dict[str, list[GameState]] = defaultdict(list)
        self.quotes: dict[str, list[Quote]] = defaultdict(list)
        self.signals: dict[str, list[Signal]] = defaultdict(list)
        self.lock = RLock()

    def add_event(self, event: Event) -> Event:
        with self.lock:
            self.events[event.id] = event
        return event

    def add_state(self, value: GameState) -> None:
        with self.lock:
            self.states[value.event_id].append(value)
            self.states[value.event_id] = self.states[value.event_id][-500:]

    def add_quotes(self, values: list[Quote]) -> None:
        with self.lock:
            for value in values:
                self.quotes[value.event_id].append(value)
            for event_id in {v.event_id for v in values}:
                self.quotes[event_id] = self.quotes[event_id][-2000:]

    def set_signals(self, event_id: str, values: list[Signal]) -> None:
        with self.lock:
            self.signals[event_id] = values

