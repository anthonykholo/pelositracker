from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from .domain.time import ensure_utc
from .matching import start_timestamp, team_match_score


def canonical_text(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", raw.casefold()))


def stable_id(kind: str, *parts: object) -> str:
    payload = ":".join([kind, *(canonical_text(part) for part in parts)])
    return str(uuid5(NAMESPACE_URL, f"pelositracker:{payload}"))


def canonical_line(line_value: object | None) -> str | None:
    """Alnum-safe token for a market line that PRESERVES the sign and decimal.

    ``canonical_text`` keeps only ``[a-z0-9]``, so it collapses ``-6.5`` and
    ``+6.5`` (and ``6.5``) to the same ``"6 5"`` -- which would make opposite
    spread lines share a market identity. Encoding the sign as a word and the
    point as ``p`` keeps the token alnum-safe (so it survives ``stable_id``'s own
    ``canonical_text``) while remaining distinct: ``neg6p5`` vs ``pos6p5``."""
    if line_value is None:
        return None
    try:
        value = float(line_value)
    except (TypeError, ValueError):
        return canonical_text(line_value) or None
    return f"{'neg' if value < 0 else 'pos'}{abs(value):g}".replace(".", "p")


@dataclass(frozen=True, slots=True)
class CanonicalParticipant:
    participant_id: str
    sport: str
    canonical_name: str

    @classmethod
    def create(cls, sport: str, name: str) -> "CanonicalParticipant":
        normalized_sport = canonical_text(sport)
        normalized_name = canonical_text(name)
        return cls(stable_id("participant", normalized_sport, normalized_name),
                   normalized_sport, normalized_name)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    canonical_event_id: str
    sport: str
    league: str
    starts_at: datetime | None
    home: CanonicalParticipant
    away: CanonicalParticipant

    @classmethod
    def create(cls, sport: str, league: str, starts_at: datetime | None,
               home: str, away: str) -> "CanonicalEvent":
        home_participant = CanonicalParticipant.create(sport, home)
        away_participant = CanonicalParticipant.create(sport, away)
        utc_start = ensure_utc(starts_at) if starts_at else None
        start_key = utc_start.isoformat() if utc_start else "unknown-start"
        return cls(
            stable_id("event", sport, league, start_key,
                      home_participant.participant_id, away_participant.participant_id),
            canonical_text(sport), canonical_text(league), utc_start,
            home_participant, away_participant,
        )


@dataclass(frozen=True, slots=True)
class CanonicalMarket:
    market_id: str
    canonical_event_id: str
    market_type: str
    line_value: str | None
    period_scope: str

    @classmethod
    def create(cls, event_id: str, market_type: str, line_value: object | None,
               period_scope: str = "full_game") -> "CanonicalMarket":
        line = canonical_line(line_value)
        kind = canonical_text(market_type)
        scope = canonical_text(period_scope)
        return cls(stable_id("market", event_id, kind, line or "none", scope),
                   event_id, kind, line, scope)


class MappingStatus(str, Enum):
    MAPPED = "mapped"
    AMBIGUOUS = "ambiguous"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class ProviderEventCandidate:
    provider_object_id: str
    home: str
    away: str
    starts_at: object
    sport: str
    league: str = ""


@dataclass(frozen=True, slots=True)
class MappingDecision:
    provider: str
    provider_object_id: str
    canonical_id: str | None
    status: MappingStatus
    confidence: float
    reason: str
    orientation: str = "unknown"
    algorithm_version: str = "fellegi-sunter-inspired-v1"
    threshold: float = 0.70
    human_override: bool = False
    evidence_json: str = "{}"


def resolve_event_mapping(provider: str, target: CanonicalEvent,
                          candidates: list[ProviderEventCandidate], *,
                          tolerance_seconds: float = 4 * 3600) -> MappingDecision:
    target_start = target.starts_at.timestamp() if target.starts_at else None
    eligible: list[tuple[float, ProviderEventCandidate, str, dict[str, float]]] = []
    for candidate in candidates:
        if canonical_text(candidate.sport) != target.sport:
            continue
        if target.league and canonical_text(candidate.league) not in {"", target.league}:
            continue
        direct = (team_match_score(target.home.canonical_name, candidate.home),
                  team_match_score(target.away.canonical_name, candidate.away))
        reverse = (team_match_score(target.home.canonical_name, candidate.away),
                   team_match_score(target.away.canonical_name, candidate.home))
        valid: list[tuple[str, tuple[int, int]]] = []
        if direct[0] is not None and direct[1] is not None:
            valid.append(("direct", (direct[0], direct[1])))
        if reverse[0] is not None and reverse[1] is not None:
            valid.append(("reversed", (reverse[0], reverse[1])))
        if not valid:
            continue
        orientation, scores = max(valid, key=lambda item: sum(item[1]))
        candidate_start = start_timestamp(candidate.starts_at)
        if target_start is not None:
            if candidate_start is None:
                continue
            delta = abs(candidate_start - target_start)
            if delta > tolerance_seconds:
                continue
        else:
            delta = tolerance_seconds
        name_score = (scores[0] + scores[1]) / 204.0
        time_score = 1.0 - min(delta / tolerance_seconds, 1.0)
        components = {"participant_score": name_score, "start_time_score": time_score}
        eligible.append((0.75 * name_score + 0.25 * time_score,
                         candidate, orientation, components))

    if not eligible:
        return MappingDecision(provider, "", None, MappingStatus.QUARANTINED, 0.0,
                               "no candidate matched sport, both participants, and start time")
    eligible.sort(key=lambda item: (-item[0], item[1].provider_object_id))
    best_score, best, orientation, components = eligible[0]
    evidence = json.dumps({
        "candidate_ids": [item[1].provider_object_id for item in eligible],
        "best_components": components,
    }, sort_keys=True, separators=(",", ":"))
    if len(eligible) > 1 and abs(best_score - eligible[1][0]) < 0.03:
        return MappingDecision(provider, best.provider_object_id, None,
                               MappingStatus.AMBIGUOUS, best_score,
                               "multiple provider events have indistinguishable identity evidence",
                               orientation=orientation, evidence_json=evidence)
    return MappingDecision(provider, best.provider_object_id, target.canonical_event_id,
                           MappingStatus.MAPPED, min(best_score, 1.0),
                           "matched sport, league, both participants, and start time",
                           orientation=orientation, evidence_json=evidence)
