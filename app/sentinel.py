"""Sentinel agent — pure signal evaluators; agents NEVER mutate schedules.

Each signal is a function ``(production, rbc, published, now_minutes,
settings) -> Signal | None`` (optional injected context: acked ids, resolved
forecasts, unhandled crew replies — injected by the caller so every signal
stays pure and I/O-free). A signal that wants action attaches an
``Incident``; the web layer materializes incidents ONLY through the standard
pipeline (sandbox for blocking, single review plan otherwise). The sentinel
never writes plan state directly.

Signals:
  * daylight_burn_down — remaining EXT/DAY work vs minutes of sun left
    (``rules.check_daylight`` boundary: ``rbc.sunset``).
  * meal_penalty_clock — minutes past the meal deadline with no lunch in the
    live plan; headcount exposed to penalty meals.
  * ack_staleness — published plan, unacked people past threshold, weighted
    by role criticality (``CRITICAL_ROLES``).
  * weather_imminent — hazard window starting within the next hour over
    remaining EXT work (reuses ``weather.build_reports`` overlap logic).
  * slippage — unhandled crew ``running_late`` replies recomputing the day.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models import MEAL_ID, Incident, Production, minutes_to_hhmm
from app.rulebook import RuleBookContext
from app.weather import build_reports

# Roles whose absence/unavailability halts a unit; matched case-insensitively
# against CrewMember.role (exact values from the imported crew CSV).
CRITICAL_ROLES = frozenset(
    {"dp", "1st ad", "sound mixer", "key grip", "script supervisor"}
)


def is_critical_role(role: str | None) -> bool:
    """True when the role is operationally critical (DP, 1st AD, ...)."""
    if not role:
        return False
    return role.strip().lower() in CRITICAL_ROLES


WEATHER_IMMINENT_WINDOW_MIN = 60
ACK_STALE_NOTIFY_MIN = 10
ACK_STALE_ESCALATE_MIN = 20
SLIPPAGE_LATE_MIN = 30


@dataclass
class Signal:
    id: str
    severity: str  # low | medium | high
    title: str
    detail: str
    metric: str
    incident: Incident | None = None
    context: dict = field(default_factory=dict)


def _remaining_rows(production: Production, rbc: RuleBookContext, published):
    """Normalized timeline rows of the live plan, else the baseline day."""
    if published and published.get("proposed_timeline"):
        return list(published.get("proposed_timeline") or []), list(
            published.get("proposed_order") or []
        )
    from app.engine import baseline_context, baseline_day_items
    from app.timeline import build_timeline

    slots = build_timeline(
        baseline_day_items(production, rbc), baseline_context(production, rbc)
    )
    rows = [{"item_id": s.item_id, "start": s.start, "end": s.end} for s in slots]
    return rows, list(production.scene_order)


def _is_done(published: dict | None, scene_id: str) -> bool:
    return scene_id in set((published or {}).get("completed_scene_ids") or [])


def _remaining_ext_rows(production: Production, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        item_id = r.get("item_id")
        if not item_id or item_id == MEAL_ID:
            continue
        scene = production.scenes.get(item_id)
        if scene is not None and scene.int_ext.value == "EXT" and scene.day_night.value == "DAY":
            out.append(r)
    return out


def daylight_burn_down(
    production: Production, rbc: RuleBookContext, published, now_minutes: int, settings
) -> Signal | None:
    rows, _ = _remaining_rows(production, rbc, published)
    ext_rows = [r for r in _remaining_ext_rows(production, rows) if not _is_done(published, r.get("item_id"))]
    if not ext_rows:
        return None
    pages = sum(production.scenes[r["item_id"]].page_count for r in ext_rows)
    minutes_needed = sum(
        rbc.scene_duration(production.scenes[r["item_id"]].page_count) for r in ext_rows
    )
    sun_left = max(rbc.sunset - max(now_minutes, rbc.sunrise), 0)
    if minutes_needed <= sun_left:
        return None
    locations = sorted({production.scenes[r["item_id"]].location_id for r in ext_rows})
    detail = (
        f"{minutes_needed} min of EXT/DAY work ({pages:.1f} pages at "
        f"{', '.join(locations)}) but only {sun_left} min of daylight left "
        f"(sunset {minutes_to_hhmm(rbc.sunset)})."
    )
    incident = Incident(
        type="OTHER",
        severity="high" if minutes_needed > sun_left else "medium",
        free_text=(
            f"Daylight shortfall: only {sun_left} min of sun left for "
            f"{pages:.1f} pages of EXT work at {', '.join(locations)}"
        ),
        confidence=1.0,
        source="sentinel",
    )
    return Signal(
        id="daylight_burn_down",
        severity="high",
        title="Daylight burning down",
        detail=detail,
        metric=f"{pages:.1f} EXT pages vs {sun_left} min sun",
        incident=incident,
        context={"scene_ids": [r.get("item_id") for r in ext_rows]},
    )


def meal_penalty_clock(
    production: Production, rbc: RuleBookContext, published, now_minutes: int, settings
) -> Signal | None:
    """Fires only once lunch is actually late against a LIVE plan: the
    baseline day auto-places lunch, so without a published plan there is
    nothing to penalize yet."""
    if not published:
        return None
    deadline = production.call_time + int(rbc.rulebook["meal_within_minutes"])
    if now_minutes < deadline:
        return None
    rows = list(published.get("proposed_timeline") or [])
    meal_taken = any(
        r.get("item_id") == MEAL_ID and (r.get("end") or 0) <= now_minutes for r in rows
    )
    meal_pending = any(
        r.get("item_id") == MEAL_ID and (r.get("start") or 0) > now_minutes for r in rows
    )
    if meal_taken or meal_pending:
        return None
    headcount = len(production.crew) + len(production.cast)
    late = now_minutes - deadline
    detail = (
        f"Meal window expired {late} min ago (call {minutes_to_hhmm(production.call_time)} "
        f"+ {rbc.rulebook['meal_within_minutes']}min) and no lunch is scheduled in "
        f"the live plan — {headcount} people exposed to penalty meals."
    )
    incident = Incident(
        type="OTHER",
        severity="high",
        free_text=(
            f"Meal deadline passed {minutes_to_hhmm(deadline)} with no lunch in the "
            f"live plan — schedule the meal break now"
        ),
        confidence=1.0,
        source="sentinel",
    )
    return Signal(
        id="meal_penalty_clock",
        severity="high",
        title="Meal penalty clock",
        detail=detail,
        metric=f"{late} min past deadline · {headcount} people",
        incident=incident,
        context={"headcount": headcount, "deadline": deadline},
    )


def ack_staleness(
    production: Production,
    rbc: RuleBookContext,
    published,
    now_minutes: int,
    settings,
    acked_ids: set[str] | None = None,
) -> Signal | None:
    if not published:
        return None
    acked_ids = acked_ids or set()
    published_at = published.get("now_minutes")
    if not isinstance(published_at, int):
        return None
    elapsed = now_minutes - published_at
    if elapsed < ACK_STALE_NOTIFY_MIN:
        return None

    subjects = [(m.id, m.name, getattr(m, "role", "")) for m in production.crew]
    unacked = [s for s in subjects if s[0] not in acked_ids]
    critical_unacked = [s for s in unacked if is_critical_role(s[2])]

    if not unacked:
        return None

    if elapsed >= ACK_STALE_ESCALATE_MIN and critical_unacked:
        who = critical_unacked[0]
        incident = Incident(
            type="CAST_DELAY",
            unit=who[1],
            severity="high",
            free_text=(
                f"{who[1]} ({who[2]}) unacknowledged {elapsed} min after publish — "
                f"confirm availability and re-plan around them"
            ),
            confidence=1.0,
            source="sentinel",
        )
        return Signal(
            id="ack_staleness",
            severity="high",
            title="Critical crew unacknowledged",
            detail=(
                f"{len(unacked)} unacked ({elapsed} min since publish); critical: "
                f"{', '.join(s[1] for s in critical_unacked)}"
            ),
            metric=f"T+{elapsed} min · {len(critical_unacked)} critical unacked",
            incident=incident,
            context={"elapsed": elapsed, "critical_unacked": [s[0] for s in critical_unacked]},
        )
    return Signal(
        id="ack_staleness",
        severity="medium",
        title="Acknowledgments pending",
        detail=f"{len(unacked)} of {len(subjects)} crew unacked {elapsed} min after publish.",
        metric=f"T+{elapsed} min · {len(unacked)} unacked",
        incident=None,
        context={"elapsed": elapsed, "unacked": [s[0] for s in unacked]},
    )


def weather_imminent(
    production: Production,
    rbc: RuleBookContext,
    published,
    now_minutes: int,
    settings,
    forecasts=None,
) -> Signal | None:
    """Promote an advisory into an agent-raised incident when the hazard
    window starts within WEATHER_IMMINENT_WINDOW_MIN minutes."""
    if not forecasts:
        return None
    rows, _ = _remaining_rows(production, rbc, published)
    if published:
        timeline = rows
    else:
        from app.engine import baseline_context, baseline_day_items
        from app.timeline import build_timeline

        timeline = build_timeline(
            baseline_day_items(production, rbc), baseline_context(production, rbc)
        )
    try:
        reports = build_reports(production, timeline, forecasts, now_minutes, settings)
    except Exception:
        return None
    for report in reports:
        for advisory in report.advisories:
            starts_in = advisory.start - now_minutes
            if 0 <= starts_in <= WEATHER_IMMINENT_WINDOW_MIN:
                scene_ids = [s.id for s in advisory.scenes]
                incident = Incident(
                    type="WEATHER",
                    location_id=report.location_id,
                    blocked_from=minutes_to_hhmm(advisory.start),
                    blocked_until=minutes_to_hhmm(advisory.end),
                    severity="high",
                    free_text=(
                        f"Weather advisory: {report.location_name} hazardous "
                        f"{minutes_to_hhmm(advisory.start)}–{minutes_to_hhmm(advisory.end)} "
                        f"({', '.join(advisory.reasons)}); threatens {', '.join(scene_ids)}"
                    ),
                    confidence=1.0,
                    source="sentinel",
                )
                return Signal(
                    id="weather_imminent",
                    severity="high",
                    title="Weather window imminent",
                    detail=(
                        f"{report.location_name} hazard starts in {starts_in} min "
                        f"({', '.join(advisory.reasons)}), threatening {', '.join(scene_ids)}."
                    ),
                    metric=f"starts in {starts_in} min · {len(scene_ids)} EXT scene(s)",
                    incident=incident,
                    context={"scene_ids": scene_ids, "location_id": report.location_id},
                )
    return None


def slippage(
    production: Production,
    rbc: RuleBookContext,
    published,
    now_minutes: int,
    settings,
    replies: list[dict] | None = None,
) -> Signal | None:
    """Unhandled crew 'running_late' replies: recompute the rest of the day."""
    for r in replies or []:
        if r.get("kind") != "running_late" or r.get("handled"):
            continue
        eta = r.get("eta_minutes") or 0
        if eta < SLIPPAGE_LATE_MIN:
            continue
        person = next((m for m in production.crew if m.id == r.get("subject_id")), None)
        name = getattr(person, "name", r.get("subject_id", "crew member"))
        incident = Incident(
            type="CAST_DELAY",
            unit=name,
            severity="high" if r.get("critical") else "medium",
            free_text=f"{name} reported running {eta} minutes late",
            confidence=1.0,
            source="sentinel",
        )
        return Signal(
            id="slippage",
            severity="high" if r.get("critical") else "medium",
            title="Day slipping",
            detail=f"{name} running {eta} min late — the rest of the day can be recomputed.",
            metric=f"{eta} min slip reported",
            incident=incident,
            context={"subject_id": r.get("subject_id"), "eta_minutes": eta},
        )
    return None


def evaluate(
    production: Production,
    rbc: RuleBookContext,
    published,
    now_minutes: int,
    settings,
    acked_ids: set[str] | None = None,
    forecasts=None,
    replies: list[dict] | None = None,
) -> list[Signal]:
    """Run every signal; a throwing signal is skipped, never fatal."""
    out: list[Signal] = []
    runners = (
        lambda: daylight_burn_down(production, rbc, published, now_minutes, settings),
        lambda: meal_penalty_clock(production, rbc, published, now_minutes, settings),
        lambda: ack_staleness(production, rbc, published, now_minutes, settings, acked_ids),
        lambda: weather_imminent(production, rbc, published, now_minutes, settings, forecasts),
        lambda: slippage(production, rbc, published, now_minutes, settings, replies),
    )
    for run in runners:
        try:
            signal = run()
        except Exception:
            continue
        if signal is not None:
            out.append(signal)
    return out
