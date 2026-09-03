"""Weather-aware schedule suggestions — pure forecast logic, zero FastAPI.

Fixture-first: the demo runs offline from committed fixtures under
``seed/weather/<LOCATION_ID>.json``. A manual "Refresh forecast" on the AD
dashboard pulls the Google Weather API into a runtime cache
(``instance/weather_cache.json``); dashboards never block on the network.

Hazard policy (Settings-tunable): an hour is hazardous when precipitation
probability >= WEATHER_PRECIP_PCT, a thunderstorm is forecast, or wind
>= WEATHER_WIND_KMH. Contiguous hazardous hours merge into one window; the
window end becomes the incident's ``blocked_until`` and its start becomes
``blocked_from`` so an afternoon storm never over-blocks the morning.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import MEAL_ID, Location, Production, Scene, hhmm_to_minutes
from app.timeline import spans_intersect

WEATHER_API_URL = "https://weather.googleapis.com/v1/forecast/hours:lookup"


# ------------------------------------------------------------------- models
@dataclass
class Hour:
    """One forecast slice in minutes-from-midnight (shoot-day wall clock)."""

    start: int
    end: int
    precip_prob: float  # percent 0-100
    wind_kmh: float
    thunder: bool
    summary: str = ""


@dataclass
class HazardWindow:
    """One merged run of contiguous hazardous hours."""

    start: int
    end: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class Forecast:
    """Resolved per-location forecast with provenance for the UI label."""

    hours: list[Hour]
    source: str  # "fixture" | "live"
    updated: str  # display HH:MM ("" when unknown)


@dataclass
class Advisory:
    """One actionable hazard: window + the remaining EXT scenes it hits."""

    start: int  # -> Incident.blocked_from
    end: int  # -> Incident.blocked_until (clamped to last scene end)
    reasons: list[str]
    scenes: list[Scene]
    scene_spans: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class LocationReport:
    location_id: str
    location_name: str
    source: str
    updated: str
    advisories: list[Advisory] = field(default_factory=list)


# ---------------------------------------------------------------- thresholds
def _thresholds(settings) -> tuple[float, float]:
    return (
        float(getattr(settings, "weather_precip_pct", 60)),
        float(getattr(settings, "weather_wind_kmh", 40)),
    )


def hazard_reasons(hour: Hour, precip_pct: float = 60, wind_kmh: float = 40) -> list[str]:
    """Why this hour is hazardous (empty list == safe to shoot)."""
    reasons: list[str] = []
    if hour.precip_prob >= precip_pct:
        reasons.append(f"precip {hour.precip_prob:.0f}%")
    if hour.thunder:
        reasons.append("thunderstorm")
    if hour.wind_kmh >= wind_kmh:
        reasons.append(f"wind {hour.wind_kmh:.0f} km/h")
    return reasons


def hazard_windows(hours: list[Hour], settings) -> list[HazardWindow]:
    """Threshold filter + contiguity merge into hazard windows."""
    precip_pct, wind_limit = _thresholds(settings)
    windows: list[HazardWindow] = []
    for h in sorted(hours, key=lambda x: x.start):
        reasons = hazard_reasons(h, precip_pct, wind_limit)
        if not reasons:
            continue
        if windows and h.start <= windows[-1].end:
            current = windows[-1]
            current.end = max(current.end, h.end)
            for reason in reasons:
                if reason not in current.reasons:
                    current.reasons.append(reason)
        else:
            windows.append(HazardWindow(h.start, h.end, list(reasons)))
    return windows


def affected_scenes(
    production: Production, timeline, location_id: str, window: HazardWindow
) -> list[Scene]:
    """Remaining EXT scenes at the location intersecting the window.

    Accepts TimelineSlot objects or serialized slot dicts; completed scenes
    never appear because the caller passes the live plan's proposed timeline
    (completed scenes are excluded at replan time).
    """
    out: list[Scene] = []
    for slot in timeline:
        if hasattr(slot, "item_id"):
            item_id, start, end = slot.item_id, slot.start, slot.end
        else:
            item_id, start, end = slot.get("item_id"), slot.get("start"), slot.get("end")
        if item_id == MEAL_ID or start is None or end is None:
            continue
        scene = production.scenes.get(item_id)
        if scene is None or scene.location_id != location_id:
            continue
        if scene.int_ext.value != "EXT":
            continue
        if spans_intersect(start, end, window.start, window.end):
            out.append(scene)
    return out


# ------------------------------------------------------- advisory assembly
def build_reports(
    production: Production,
    timeline,
    forecasts: dict[str, Forecast],
    now_minutes: int,
    settings,
) -> list[LocationReport]:
    """Per-location weather card rows: resolved forecast + actionable windows.

    Past windows and windows touching no remaining EXT scene are dropped; a
    window running past the last scene is clamped to the end of day (engine
    diagnostics stay the source of truth for the actual plan).
    """
    spans: dict[str, tuple[int, int]] = {}
    for slot in timeline:
        if hasattr(slot, "item_id"):
            item_id, start, end = slot.item_id, slot.start, slot.end
        else:
            item_id, start, end = slot.get("item_id"), slot.get("start"), slot.get("end")
        if item_id and item_id != MEAL_ID and start is not None and end is not None:
            spans[item_id] = (start, end)
    day_end = max((end for _, end in spans.values()), default=24 * 60)

    reports: list[LocationReport] = []
    for loc_id, loc in production.locations.items():
        forecast = forecasts.get(loc_id)
        if forecast is None:
            continue
        advisories: list[Advisory] = []
        for window in hazard_windows(forecast.hours, settings):
            if window.end <= now_minutes:
                continue  # hazard fully in the past
            scenes = affected_scenes(production, timeline, loc_id, window)
            if not scenes:
                continue
            # Clamp to the end of the shooting day; with scenes affected, the
            # clamped window is always non-degenerate (end > start).
            advisories.append(
                Advisory(
                    start=window.start,
                    end=min(window.end, day_end),
                    reasons=list(window.reasons),
                    scenes=scenes,
                    scene_spans={s.id: spans[s.id] for s in scenes if s.id in spans},
                )
            )
        reports.append(
            LocationReport(
                location_id=loc_id,
                location_name=loc.name,
                source=forecast.source,
                updated=forecast.updated,
                advisories=advisories,
            )
        )
    return reports


# ------------------------------------------------------------- serialization
def _hour_to_dict(h: Hour) -> dict:
    return {
        "start": f"{h.start // 60:02d}:{h.start % 60:02d}",
        "end": f"{h.end // 60:02d}:{h.end % 60:02d}",
        "precip_prob": h.precip_prob,
        "wind_kmh": h.wind_kmh,
        "thunder": h.thunder,
        "summary": h.summary,
    }


def _parse_hours(value) -> list[Hour] | None:
    """Strict all-or-nothing parse of normalized hour dicts (HH:MM edges)."""
    if not isinstance(value, list) or not value:
        return None
    out: list[Hour] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        try:
            start = hhmm_to_minutes(str(entry["start"]))
            end = hhmm_to_minutes(str(entry["end"]))
            precip = float(entry["precip_prob"])
            wind = float(entry["wind_kmh"])
            thunder = bool(entry.get("thunder", False))
            summary = str(entry.get("summary", ""))
        except (KeyError, TypeError, ValueError):
            return None
        if end <= start or not 0.0 <= precip <= 100.0 or wind < 0.0:
            return None
        out.append(Hour(start, end, precip, wind, thunder, summary))
    return out


# ------------------------------------------------------------------ fixtures
def _read_fixture_doc(seed_dir, location_id: str) -> tuple[list[Hour], datetime | None] | None:
    path = Path(seed_dir) / "weather" / f"{location_id}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # simply no fixture for this location
    except (OSError, ValueError) as exc:
        print(f"[weather] malformed fixture {path}: {exc}", file=sys.stderr)
        return None
    hours = _parse_hours(doc.get("hours") if isinstance(doc, dict) else None)
    if hours is None:
        print(f"[weather] malformed fixture {path}: bad hours", file=sys.stderr)
        return None
    generated = None
    if doc.get("generated_at"):
        try:
            generated = datetime.fromisoformat(str(doc["generated_at"]))
        except ValueError:
            generated = None
    return hours, generated


def load_fixture(seed_dir, location_id: str) -> list[Hour] | None:
    """Committed offline forecast for one location (None when absent/bad)."""
    doc = _read_fixture_doc(seed_dir, location_id)
    return doc[0] if doc else None


# -------------------------------------------------------- Google Weather API
def _iso_to_minutes(value) -> int | None:
    """RFC3339 timestamp -> wall-clock minutes-from-midnight in its own offset."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute


def normalize_google_hours(payload: dict) -> list[Hour]:
    """Google Weather ``forecast/hours:lookup`` payload -> Hour list.

    Malformed entries are skipped (live data is best-effort; the fixture path
    is the strict one).
    """
    out: list[Hour] = []
    entries = payload.get("forecastHours") if isinstance(payload, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        interval = entry.get("interval") or {}
        start = _iso_to_minutes(interval.get("startTime"))
        end = _iso_to_minutes(interval.get("endTime"))
        if start is None or end is None:
            continue
        if end <= start:
            end += 24 * 60  # entry straddles midnight
        condition = entry.get("weatherCondition") or {}
        cond_type = str(condition.get("type") or "").upper()
        description = (condition.get("description") or {}).get("text") or ""
        precip = ((entry.get("precipitation") or {}).get("probability") or {}).get("percent")
        try:
            precip_prob = float(precip) if precip is not None else 0.0
        except (TypeError, ValueError):
            precip_prob = 0.0
        speed = ((entry.get("wind") or {}).get("speed") or {})
        try:
            wind = float(speed.get("value") or 0.0)
        except (TypeError, ValueError):
            wind = 0.0
        if str(speed.get("unit") or "").upper() == "MILES_PER_HOUR":
            wind *= 1.60934
        out.append(
            Hour(
                start,
                end,
                precip_prob,
                round(wind, 1),
                "THUNDERSTORM" in cond_type,
                str(description),
            )
        )
    return out


def fetch_live(location: Location, api_key: str, client=None, timeout: float = 8.0):
    """Google Weather lookup for one location. Never raises into callers:
    any failure (no coords, no key, HTTP error, timeout, bad payload) -> None."""
    if location.lat is None or location.lng is None or not api_key:
        return None
    if client is None:
        import httpx

        client = httpx
    try:
        resp = client.get(
            WEATHER_API_URL,
            params={
                "key": api_key,
                "location.latitude": location.lat,
                "location.longitude": location.lng,
                "hours": 12,
                "unitsSystem": "METRIC",
            },
            timeout=timeout,
        )
        if getattr(resp, "status_code", 200) != 200:
            return None
        hours = normalize_google_hours(resp.json())
    except Exception:
        return None
    return hours or None


# -------------------------------------------------------------- runtime cache
def read_cache(cache_path) -> dict:
    """Tolerant read of the runtime live-forecast cache ({} on any problem)."""
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_live_forecasts(cache_path, results: dict[str, list[Hour]], fetched_at: datetime) -> None:
    """Merge successful live fetches into the cache file (atomic replace)."""
    cache = read_cache(cache_path)
    stamp = fetched_at.isoformat(timespec="seconds")
    for loc_id, hours in results.items():
        cache[loc_id] = {"fetched_at": stamp, "hours": [_hour_to_dict(h) for h in hours]}
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-process tmp name: two concurrent refreshes must not clobber the
    # same temp file before their os.replace.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _display_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # local wall clock, like every other HH:MM here
    return f"{dt.hour:02d}:{dt.minute:02d}"


def resolve_forecast(
    location_id: str,
    seed_dir,
    cache_path,
    settings,
    now: datetime | None = None,
    cache: dict | None = None,
) -> Forecast | None:
    """Fresh live cache entry first, then the committed fixture, else None.

    ``cache`` lets callers that resolve many locations pass one already-read
    cache dict instead of re-reading the file per location.
    """
    now = now or datetime.now(timezone.utc)
    max_age = timedelta(minutes=float(getattr(settings, "weather_cache_max_age_min", 60)))
    entry = (cache if cache is not None else read_cache(cache_path)).get(location_id)
    if isinstance(entry, dict):
        try:
            fetched_at = datetime.fromisoformat(str(entry["fetched_at"]))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            hours = _parse_hours(entry.get("hours"))
            if hours is not None and now - fetched_at <= max_age:
                return Forecast(hours, "live", _display_time(fetched_at))
        except (KeyError, TypeError, ValueError):
            pass  # corrupt entry -> fall through to fixture
    doc = _read_fixture_doc(seed_dir, location_id)
    if doc is None:
        return None
    hours, generated = doc
    return Forecast(hours, "fixture", _display_time(generated))
