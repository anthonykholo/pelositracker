from __future__ import annotations

from collections import defaultdict, deque
from threading import RLock

from .models import Event, GameState, Quote, Signal

# Retention windows for the live buffers. Bounded ``deque`` objects discard the
# oldest entries automatically as new ones arrive, so we keep only the most
# recent activity per event without re-copying the whole list on every append
# (the old ``list[-N:]`` trim allocated a fresh list on each state/quote).
_MAX_STATES = 500
_MAX_QUOTES = 2000


class Store:
    def __init__(self):
        self.events: dict[str, Event] = {}
        self.states: dict[str, deque[GameState]] = defaultdict(
            lambda: deque(maxlen=_MAX_STATES)
        )
        self.quotes: dict[str, deque[Quote]] = defaultdict(
            lambda: deque(maxlen=_MAX_QUOTES)
        )
        self.signals: dict[str, list[Signal]] = defaultdict(list)
        self.lock = RLock()

    def add_event(self, event: Event) -> Event:
        with self.lock:
            self.events[event.id] = event
        return event

    def add_state(self, value: GameState) -> None:
        with self.lock:
            self.states[value.event_id].append(value)

    def add_quotes(self, values: list[Quote]) -> None:
        with self.lock:
            for value in values:
                self.quotes[value.event_id].append(value)

    def set_signals(self, event_id: str, values: list[Signal]) -> None:
        with self.lock:
            self.signals[event_id] = values
