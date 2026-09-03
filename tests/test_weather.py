"""Weather-aware schedule suggestions: hazard policy, fixtures, live fetch,
forecast resolution, blocked_from engine semantics, and the advisory E2E flow."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app import weather
from app.engine import replan
from app.models import Incident
from app.timeline import build_timeline

SEED = "seed"


# ------------------------------------------------------------- hazard policy
def test_precip_threshold_boundary(settings):
    hours = [
        weather.Hour(840, 900, 59, 10, False, "ok"),
        weather.Hour(900, 960, 60, 10, False, "at threshold"),
    ]
    windows = weather.hazard_windows(hours, settings)
    assert len(windows) == 1
    assert (windows[0].start, windows[0].end) == (900, 960)
    assert windows[0].reasons == ["precip 60%"]


def test_wind_threshold_boundary(settings):
    hours = [weather.Hour(0, 60, 0, 39.9, False, ""), weather.Hour(60, 120, 0, 40, False, "")]
    windows = weather.hazard_windows(hours, settings)
    assert len(windows) == 1
    assert (windows[0].start, windows[0].end) == (60, 120)
    assert windows[0].reasons == ["wind 40 km/h"]


def test_thunderstorm_alone_is_hazardous(settings):
    hours = [weather.Hour(600, 660, 5, 9, True, "dry lightning")]
    windows = weather.hazard_windows(hours, settings)
    assert len(windows) == 1
    assert windows[0].reasons == ["thunderstorm"]


def test_benign_hours_produce_no_windows(settings):
    hours = [
        weather.Hour(420, 480, 5, 8, False, "Clear"),
        weather.Hour(480, 540, 10, 12, False, "Sunny"),
    ]
    assert weather.hazard_windows(hours, settings) == []


def test_thresholds_are_settings_tunable(settings):
    settings.weather_precip_pct = 10
    settings.weather_wind_kmh = 5
    hours = [weather.Hour(420, 480, 15, 6, False, "")]
    windows = weather.hazard_windows(hours, settings)
    assert len(windows) == 1
    assert set(windows[0].reasons) == {"precip 15%", "wind 6 km/h"}


def test_contiguous_hours_merge_and_gaps_split(settings):
    hours = [
        weather.Hour(840, 870, 65, 10, False, "rain"),
        weather.Hour(870, 900, 70, 10, True, "storm"),
        weather.Hour(900, 930, 5, 10, False, "clear gap"),  # splits the window
        weather.Hour(930, 960, 80, 45, False, "rain again"),
    ]
    windows = weather.hazard_windows(hours, settings)
    assert [(w.start, w.end) for w in windows] == [(840, 900), (930, 960)]
    assert windows[0].reasons == ["precip 65%", "precip 70%", "thunderstorm"]
    assert windows[1].reasons == ["precip 80%", "wind 45 km/h"]


def test_unsorted_hours_are_merged_in_time_order(settings):
    hours = [
        weather.Hour(900, 930, 70, 10, False, ""),
        weather.Hour(840, 900, 60, 10, False, ""),
    ]
    windows = weather.hazard_windows(hours, settings)
    assert [(w.start, w.end) for w in windows] == [(840, 930)]


# ------------------------------------------------------------------ fixtures
def test_ranch_fixture_merges_to_single_afternoon_window(settings):
    hours = weather.load_fixture(SEED, "L-RANCH")
    assert hours, "committed demo fixture must exist"
    windows = weather.hazard_windows(hours, settings)
    assert [(w.start, w.end) for w in windows] == [(840, 990)]  # 14:00–16:30
    assert any("thunderstorm" in r for r in windows[0].reasons)


def test_other_demo_locations_are_benign(settings):
    for loc in ("L-STAGE4", "L-MAINST"):
        hours = weather.load_fixture(SEED, loc)
        assert hours
        assert weather.hazard_windows(hours, settings) == []


def test_load_fixture_missing_file_returns_none():
    assert weather.load_fixture(SEED, "L-NOWHERE") is None


def test_load_fixture_malformed_json_returns_none(tmp_path, capsys):
    bad = tmp_path / "weather" / "L-X.json"
    bad.parent.mkdir()
    bad.write_text("{not json", encoding="utf-8")
    assert weather.load_fixture(tmp_path, "L-X") is None
    assert "malformed fixture" in capsys.readouterr().err


def test_load_fixture_bad_field_skips_location(tmp_path, capsys):
    bad = tmp_path / "weather" / "L-Y.json"
    bad.parent.mkdir()
    bad.write_text(
        json.dumps({"hours": [{"start": "07:00", "end": "08:00", "precip_prob": 150}]}),
        encoding="utf-8",
    )
    assert weather.load_fixture(tmp_path, "L-Y") is None
    assert capsys.readouterr().err


def test_load_fixture_rejects_inverted_hour(tmp_path):
    bad = tmp_path / "weather" / "L-Z.json"
    bad.parent.mkdir()
    bad.write_text(
        json.dumps({"hours": [{"start": "09:00", "end": "08:00", "precip_prob": 10}]}),
        encoding="utf-8",
    )
    assert weather.load_fixture(tmp_path, "L-Z") is None


# ------------------------------------------------------- google normalization
GOOGLE_PAYLOAD = {
    "timeZone": {"id": "America/Los_Angeles"},
    "forecastHours": [
        {
            "interval": {
                "startTime": "2026-09-01T14:00:00-07:00",
                "endTime": "2026-09-01T15:00:00-07:00",
            },
            "isDaytime": True,
            "weatherCondition": {"type": "RAIN", "description": {"text": "Rain"}},
            "precipitation": {"probability": {"type": "RAIN", "percent": 75}},
            "wind": {"speed": {"value": 18, "unit": "KILOMETERS_PER_HOUR"}},
        },
        {
            "interval": {
                "startTime": "2026-09-01T15:00:00-07:00",
                "endTime": "2026-09-01T16:00:00-07:00",
            },
            "weatherCondition": {"type": "THUNDERSTORM"},
            "precipitation": {"probability": {"type": "RAIN", "percent": 85}},
            "wind": {"speed": {"value": 25, "unit": "MILES_PER_HOUR"}},
        },
        {
            # malformed entry: skipped, never fatal for live data
            "interval": {},
            "weatherCondition": {"type": "CLEAR"},
        },
    ],
}


def test_normalize_google_hours_recorded_payload():
    hours = weather.normalize_google_hours(GOOGLE_PAYLOAD)
    assert len(hours) == 2
    first, second = hours
    assert (first.start, first.end) == (840, 900)
    assert first.precip_prob == 75.0
    assert first.wind_kmh == 18.0
    assert first.thunder is False
    assert first.summary == "Rain"
    assert (second.start, second.end) == (900, 960)
    assert second.thunder is True
    assert second.wind_kmh == round(25 * 1.60934, 1)


def test_normalize_rejects_non_dict_payload():
    assert weather.normalize_google_hours(None) == []
    assert weather.normalize_google_hours([]) == []
    assert weather.normalize_google_hours({}) == []


# ------------------------------------------------------------------ fetch_live
class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, status=200, exc=None):
        self._payload = payload
        self._status = status
        self._exc = exc
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if self._exc:
            raise self._exc
        return _FakeResponse(self._payload, self._status)


def test_fetch_live_success_path(production):
    client = _FakeClient(GOOGLE_PAYLOAD)
    loc = production.locations["L-RANCH"]
    hours = weather.fetch_live(loc, "test-key", client=client)
    assert hours is not None and len(hours) == 2
    url, params, timeout = client.calls[0]
    assert url == weather.WEATHER_API_URL
    assert params["key"] == "test-key"
    assert params["location.latitude"] == loc.lat
    assert params["location.longitude"] == loc.lng


def test_fetch_live_never_raises(production):
    loc = production.locations["L-RANCH"]
    boom = _FakeClient(exc=RuntimeError("connection refused"))
    assert weather.fetch_live(loc, "test-key", client=boom) is None
    assert weather.fetch_live(loc, "test-key", client=_FakeClient(status=500)) is None
    assert weather.fetch_live(loc, "test-key", client=_FakeClient({})) is None
    # no coords / no key -> never even a network attempt
    no_coords = type("L", (), {"lat": None, "lng": None})()
    assert weather.fetch_live(no_coords, "test-key", client=_FakeClient()) is None
    assert weather.fetch_live(loc, "", client=_FakeClient()) is None


# ------------------------------------------------------------ forecast cache
def test_save_and_read_cache_roundtrip(tmp_path):
    cache = tmp_path / "weather_cache.json"
    hours = [weather.Hour(840, 870, 70, 20, False, "Rain")]
    fetched = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    weather.save_live_forecasts(cache, {"L-RANCH": hours}, fetched)
    data = weather.read_cache(cache)
    assert data["L-RANCH"]["fetched_at"] == "2026-09-02T10:00:00+00:00"
    assert data["L-RANCH"]["hours"][0]["start"] == "14:00"
    assert weather.read_cache(tmp_path / "missing.json") == {}


def test_resolve_forecast_prefers_fresh_live_cache(tmp_path, settings):
    cache = tmp_path / "weather_cache.json"
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    live = [weather.Hour(840, 990, 90, 50, True, "Storm")]
    weather.save_live_forecasts(cache, {"L-RANCH": live}, now - timedelta(minutes=5))
    fc = weather.resolve_forecast("L-RANCH", SEED, cache, settings, now=now)
    assert fc is not None and fc.source == "live"
    assert fc.hours == live


def test_resolve_forecast_stale_cache_falls_back_to_fixture(tmp_path, settings):
    cache = tmp_path / "weather_cache.json"
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    stale = [weather.Hour(300, 330, 90, 50, True, "Old storm")]
    weather.save_live_forecasts(cache, {"L-RANCH": stale}, now - timedelta(minutes=61))
    fc = weather.resolve_forecast("L-RANCH", SEED, cache, settings, now=now)
    assert fc is not None and fc.source == "fixture"
    assert fc.updated == "06:30"


def test_resolve_forecast_cache_max_age_is_settings_tunable(tmp_path, settings):
    settings.weather_cache_max_age_min = 120
    cache = tmp_path / "weather_cache.json"
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    weather.save_live_forecasts(
        cache,
        {"L-RANCH": [weather.Hour(840, 870, 70, 10, False, "")]},
        now - timedelta(minutes=90),
    )
    fc = weather.resolve_forecast("L-RANCH", SEED, cache, settings, now=now)
    assert fc is not None and fc.source == "live"


def test_resolve_forecast_corrupt_cache_entry_falls_back(tmp_path, settings):
    cache = tmp_path / "weather_cache.json"
    cache.write_text(
        json.dumps({"L-RANCH": {"fetched_at": "garbage", "hours": "nope"}}), encoding="utf-8"
    )
    fc = weather.resolve_forecast("L-RANCH", SEED, cache, settings)
    assert fc is not None and fc.source == "fixture"


def test_resolve_forecast_none_without_fixture_or_cache(tmp_path, settings):
    assert weather.resolve_forecast("L-NOWHERE", SEED, tmp_path / "none.json", settings) is None


# ------------------------------------------------------------- affected scenes
@pytest.fixture()
def baseline_timeline(production, rbc):
    from app.engine import baseline_context

    return build_timeline(list(production.scene_order), baseline_context(production, rbc))


RANCH_STORM = weather.HazardWindow(840, 990, ["precip 70%"])


def test_affected_scenes_hits_intersecting_ext(baseline_timeline, production):
    scenes = weather.affected_scenes(production, baseline_timeline, "L-RANCH", RANCH_STORM)
    ids = [s.id for s in scenes]
    assert "SC-125" in ids  # 15:52–16:03 baseline
    assert all(s.location_id == "L-RANCH" and s.int_ext.value == "EXT" for s in scenes)


def test_affected_scenes_excludes_int_scenes(baseline_timeline, production):
    # L-RANCH's only INT scene (SC-122) must never trigger an advisory
    scenes = weather.affected_scenes(production, baseline_timeline, "L-RANCH", RANCH_STORM)
    assert all(s.id != "SC-122" for s in scenes)


def test_affected_scenes_window_intersection_edges(baseline_timeline, production):
    # half-open semantics: a window that opens exactly when SC-121 starts
    # (807) or starts exactly when SC-125 ends (963) touches no scene
    assert weather.affected_scenes(
        production, baseline_timeline, "L-RANCH", weather.HazardWindow(800, 807, [])
    ) == []
    assert weather.affected_scenes(
        production, baseline_timeline, "L-RANCH", weather.HazardWindow(963, 990, [])
    ) == []
    # a window overlapping both scenes hits both, in timeline order
    both = weather.affected_scenes(
        production, baseline_timeline, "L-RANCH", weather.HazardWindow(807, 990, [])
    )
    assert [s.id for s in both] == ["SC-121", "SC-125"]


def test_affected_scenes_accepts_serialized_rows(production, baseline_timeline):
    rows = [
        {"item_id": s.item_id, "start": s.start, "end": s.end, "location_id": s.location_id}
        for s in baseline_timeline
    ]
    from_slots = weather.affected_scenes(production, baseline_timeline, "L-RANCH", RANCH_STORM)
    from_dicts = weather.affected_scenes(production, rows, "L-RANCH", RANCH_STORM)
    assert [s.id for s in from_slots] == [s.id for s in from_dicts]


# --------------------------------------------------------------- build_reports
def test_reports_drop_fully_past_windows(baseline_timeline, production, settings):
    forecasts = {
        "L-RANCH": weather.Forecast(
            weather.load_fixture(SEED, "L-RANCH"), "fixture", "06:30"
        )
    }
    reports = weather.build_reports(production, baseline_timeline, forecasts, 1020, settings)
    ranch = next(r for r in reports if r.location_id == "L-RANCH")
    assert ranch.advisories == []


def test_reports_clamp_to_last_scene_end(baseline_timeline, production, settings):
    forecasts = {
        "L-RANCH": weather.Forecast(
            weather.load_fixture(SEED, "L-RANCH"), "fixture", "06:30"
        )
    }
    reports = weather.build_reports(production, baseline_timeline, forecasts, 540, settings)
    ranch = next(r for r in reports if r.location_id == "L-RANCH")
    assert len(ranch.advisories) == 1
    advisory = ranch.advisories[0]
    assert (advisory.start, advisory.end) == (840, 963)  # clamped to day end 16:03
    assert advisory.scene_spans["SC-125"] == (952, 963)


def test_reports_use_meal_inclusive_baseline_day(production, rbc, settings):
    """The no-publish baseline must include lunch: it shifts the afternoon
    into the storm window, so SC-121 and SC-125 are both threatened."""
    from app.engine import baseline_context, baseline_day_items

    timeline = build_timeline(
        baseline_day_items(production, rbc), baseline_context(production, rbc)
    )
    forecasts = {
        "L-RANCH": weather.Forecast(
            weather.load_fixture(SEED, "L-RANCH"), "fixture", "06:30"
        )
    }
    reports = weather.build_reports(production, timeline, forecasts, 540, settings)
    ranch = next(r for r in reports if r.location_id == "L-RANCH")
    assert len(ranch.advisories) == 1
    advisory = ranch.advisories[0]
    assert (advisory.start, advisory.end) == (840, 990)  # 14:00–16:30, unclamped
    assert [s.id for s in advisory.scenes] == ["SC-121", "SC-125"]


def test_reports_skip_window_without_remaining_ext(baseline_timeline, production, settings):
    # all-day hazard at the stage location still yields nothing (all INT there)
    hours = [weather.Hour(s, s + 60, 95, 60, True, "Awful") for s in range(420, 1020, 60)]
    forecasts = {"L-STAGE4": weather.Forecast(hours, "fixture", "06:30")}
    reports = weather.build_reports(production, baseline_timeline, forecasts, 540, settings)
    stage = next(r for r in reports if r.location_id == "L-STAGE4")
    assert stage.advisories == []


def test_reports_locations_without_forecast_are_omitted(production, baseline_timeline, settings):
    assert weather.build_reports(production, baseline_timeline, {}, 540, settings) == []


# --------------------------------------------------------- blocked_from engine
def _weather_incident(**kw):
    base = dict(
        type="WEATHER",
        location_id="L-RANCH",
        blocked_until="16:30",
        severity="high",
        free_text="storm rolling in",
        source="weather_advisor",
    )
    base.update(kw)
    return Incident(**base)


def test_blocked_from_future_window_does_not_overblock_morning(production, rbc):
    # now 08:00, storm 14:00–16:30: hold strategy keeps the original order and
    # ranch work that fits before the storm shoots normally (no morning void).
    incident = _weather_incident(blocked_from="14:00")
    p = replan(
        production, rbc, [], incident, 480, "w1", "2026-09-01T08:00:00+00:00", strategy="hold"
    )
    slots = {s.item_id: s for s in p.proposed_timeline}
    assert slots["SC-101"].start == 480  # shoots at 'now', NOT delayed to 16:30
    assert slots["SC-121"].start >= 990  # intersecting scene cannot start inside the storm
    info = next(c for c in p.changes if c.kind == "INFO")
    assert "14:00–16:30" in info.reason


def test_blocked_from_default_behaviour_unchanged(production, rbc):
    # no blocked_from -> the window starts at 'now' (08:00): pre-8g behavior
    incident = _weather_incident()
    p = replan(
        production, rbc, [], incident, 480, "w2", "2026-09-01T08:00:00+00:00", strategy="hold"
    )
    slots = {s.item_id: s for s in p.proposed_timeline}
    assert slots["SC-101"].start == 990  # whole morning at the location waits
    info = next(c for c in p.changes if c.kind == "INFO")
    assert "08:00–16:30" in info.reason


def test_blocked_from_nonsensical_falls_back_to_now(production, rbc):
    incident = _weather_incident(blocked_from="17:00")  # >= blocked_until 16:30
    p = replan(
        production, rbc, [], incident, 480, "w3", "2026-09-01T08:00:00+00:00", strategy="hold"
    )
    slots = {s.item_id: s for s in p.proposed_timeline}
    assert slots["SC-101"].start == 990
    info = next(c for c in p.changes if c.kind == "INFO")
    assert "08:00–16:30" in info.reason


def test_blocked_from_before_now_still_blocks_remaining_day(production, rbc):
    # storm began before 'now': start is in the past, remaining work still waits
    incident = _weather_incident(blocked_from="07:30")
    p = replan(
        production, rbc, [], incident, 480, "w4", "2026-09-01T08:00:00+00:00", strategy="hold"
    )
    slots = {s.item_id: s for s in p.proposed_timeline}
    assert slots["SC-101"].start == 990


def test_incident_serialization_roundtrips_blocked_from():
    incident = _weather_incident(blocked_from="14:00")
    dumped = incident.model_dump()
    assert dumped["blocked_from"] == "14:00"
    again = Incident(**dumped)
    assert again.blocked_from_minutes() == 840
    assert again.blocked_until_minutes() == 990


def test_proposal_payload_carries_blocked_from(production, rbc):
    from app.serialize import proposal_to_dict

    p = replan(
        production,
        rbc,
        [],
        _weather_incident(blocked_from="14:00"),
        480,
        "w5",
        "2026-09-01T08:00:00+00:00",
    )
    payload = proposal_to_dict(p)
    assert payload["incident"]["blocked_from"] == "14:00"
    assert payload["incident"]["source"] == "weather_advisor"


def test_wait_decision_includes_travel_buffer(production, rbc):
    """Reviews: the travel lead-in is part of the occupied span.

    SC-121 arrives from L-MAINST: pre-travel clock fits before the storm but
    the post-travel span [837, 849) crosses 14:00, so it must wait for the
    window — and the hold strategy must produce no R-BLOCKED-LOCATION error.
    """
    incident = _weather_incident(blocked_from="14:00")
    p = replan(
        production, rbc, [], incident, 540, "w6", "2026-09-01T09:00:00+00:00", strategy="hold"
    )
    slots = {s.item_id: s for s in p.proposed_timeline}
    assert slots["SC-121"].start >= 990  # waits out the block, travel applied after
    block_errors = [
        d for d in p.diagnostics if d.rule_id == "R-BLOCKED-LOCATION" and d.severity == "ERROR"
    ]
    assert block_errors == [], "engine must never schedule inside its own blocked window"


def test_change_reasons_describe_future_windows_factually(production, rbc):
    """A future-window block must not produce legacy 'unavailable until' /
    'reopens' wording for scenes that actually shoot before the block."""
    incident = _weather_incident(blocked_from="14:00")
    cover = replan(
        production, rbc, [], incident, 480, "w7", "2026-09-01T08:00:00+00:00", strategy="cover_set"
    )
    cover_reasons = [c.reason for c in cover.changes if c.rule_id == "R-COVER-SET"]
    assert cover_reasons
    assert all("shoots before" in r for r in cover_reasons), cover_reasons
    assert not any("reopens" in r for r in cover_reasons)

    minimal = replan(
        production, rbc, [], incident, 540, "w8", "2026-09-01T09:00:00+00:00"
    )
    blocked_reasons = [c.reason for c in minimal.changes if c.rule_id == "R-BLOCKED-LOCATION"]
    assert blocked_reasons
    assert not any("unavailable until" in r for r in blocked_reasons)
    assert "14:00–16:30" in blocked_reasons[-1]


# ------------------------------------------------------- config + demo clock
def test_weather_env_vars_fall_back_instead_of_crashing_boot(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("WEATHER_PRECIP_PCT", "banana")
    monkeypatch.setenv("WEATHER_WIND_KMH", "")
    monkeypatch.setenv("WEATHER_CACHE_MAX_AGE_MIN", "{}",)
    s = Settings()
    assert (s.weather_precip_pct, s.weather_wind_kmh, s.weather_cache_max_age_min) == (
        60.0,
        40.0,
        60,
    )


def test_now_override_env_pins_demo_clock(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("NOW_OVERRIDE", "09:00")
    s = Settings()
    assert s.demo_clock == "09:00"
    monkeypatch.setenv("NOW_OVERRIDE", "99:99")
    assert Settings().demo_clock == ""  # invalid input ignored


def test_now_minutes_prefers_override_then_demo_clock(settings):
    from app.web import routes_ad

    settings.demo_clock = "09:00"
    assert routes_ad._now_minutes(None, settings) == 540
    assert routes_ad._now_minutes("10:30", settings) == 630  # form override wins


def test_dashboard_uses_demo_clock_on_any_server_tz(settings, production, rbc):
    """No monkeypatching: NOW_OVERRIDE pins the advisory clock end-to-end."""
    settings.demo_clock = "09:00"
    with _client_for(settings, production, rbc) as c:
        home = c.get("/")
        assert home.status_code == 200
        assert "now 09:00" in home.text
        assert "Generate weather pivot plan" in home.text  # advisory always live


# ------------------------------------------------------------------- E2E flow
@contextmanager
def _client_for(settings, production, rbc):
    from app.store import Store
    from app.web import create_app
    from fastapi.testclient import TestClient
    from conftest import basic_auth_header

    store = Store(settings.db_path)
    app = create_app(settings=settings, production=production, rulebook_ctx=rbc, store=store)
    try:
        with TestClient(app) as c:
            c.headers["Authorization"] = basic_auth_header(
                settings.ad_username, settings.ad_password
            )
            yield c
    finally:
        store.close()


@pytest.fixture()
def morning_clock(monkeypatch):
    """Pin the demo clock to 09:00 so fixture windows are always in the future."""
    from app.web import routes_ad

    monkeypatch.setattr(routes_ad, "_now_minutes", lambda override=None, settings=None: 540)
    return 540


def test_dashboard_advisory_adopt_sandbox_publish_roundtrip(client, morning_clock):
    # 1. advisory visible on the dashboard (fixture-first)
    home = client.get("/")
    assert home.status_code == 200
    assert "Weather watch" in home.text
    assert "Harper Ranch" in home.text
    assert "14:00" in home.text and "16:30" in home.text
    assert "SC-121" in home.text and "SC-125" in home.text
    assert "Generate weather pivot plan" in home.text
    assert "source: fixture" in home.text

    # 2. one-click adopt -> blocking WEATHER incident -> strategy sandbox
    adopt = client.post(
        "/weather/adopt",
        data={
            "location_id": "L-RANCH",
            "from": "14:00",
            "until": "16:30",
            "summary": "precip 85%, thunderstorm",
        },
        follow_redirects=False,
    )
    assert adopt.status_code == 303, adopt.text
    assert adopt.headers["location"].startswith("/sandbox/")
    gid = adopt.headers["location"].rsplit("/", 1)[-1]

    plans = client.app.state.store.plans_in_group(gid)
    assert len(plans) == 3
    incident = plans[0]["incident"]
    assert incident["type"] == "WEATHER"
    assert incident["source"] == "weather_advisor"
    assert incident["location_id"] == "L-RANCH"
    assert incident["blocked_from"] == "14:00"
    assert incident["blocked_until"] == "16:30"

    sandbox = client.get(adopt.headers["location"])
    assert sandbox.status_code == 200
    assert "blocked 14:00–16:30" in sandbox.text

    # 3. pick a feasible option and publish it
    chosen = next((p for p in plans if p["is_feasible"]), plans[0])
    client.post(f"/plans/{chosen['id']}/select", follow_redirects=False)
    published = client.post(f"/plans/{chosen['id']}/publish", follow_redirects=False)
    assert published.status_code == 303

    # 4. crew link reflects the published pivot
    from app.tokens import subject_token

    token = subject_token("test-secret", "crew", "CR-01")
    card = client.get(f"/c/{token}")
    assert card.status_code == 200
    assert "What changed" in card.text


def test_dashboard_hides_past_window(client, monkeypatch):
    from app.web import routes_ad

    monkeypatch.setattr(
        routes_ad, "_now_minutes", lambda override=None, settings=None: 1020
    )  # 17:00
    home = client.get("/")
    assert home.status_code == 200
    assert "Weather watch" in home.text  # forecasts still listed...
    assert "Generate weather pivot plan" not in home.text  # ...but nothing to act on


def test_adopt_rejects_invalid_window(client):
    resp = client.post(
        "/weather/adopt",
        data={"location_id": "L-RANCH", "from": "17:00", "until": "16:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "rejected" in resp.headers["location"]


def test_adopt_rejects_unknown_location(client):
    resp = client.post(
        "/weather/adopt",
        data={"location_id": "L-NOPE", "from": "14:00", "until": "16:00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "rejected" in resp.headers["location"]


def test_refresh_without_key_explains_not_configured(client):
    assert client.app.state.settings.google_maps_api_key == ""
    resp = client.post("/weather/refresh", follow_redirects=False)
    assert resp.status_code == 303
    assert "not+configured" in resp.headers["location"]


def test_refresh_with_key_writes_cache_and_dashboard_turns_live(
    settings, production, rbc, tmp_path, monkeypatch, morning_clock
):
    import app.weather as weather_mod
    from app.web import routes_ad

    settings.google_maps_api_key = "test-key"
    settings.db_path = tmp_path / "t.db"  # isolates the runtime cache per test
    recorded = {
        "forecastHours": [
            {
                "interval": {
                    "startTime": "2026-09-01T14:00:00-07:00",
                    "endTime": "2026-09-01T15:00:00-07:00",
                },
                "weatherCondition": {"type": "RAIN", "description": {"text": "Rain"}},
                "precipitation": {"probability": {"type": "RAIN", "percent": 90}},
                "wind": {"speed": {"value": 20, "unit": "KILOMETERS_PER_HOUR"}},
            }
        ]
    }

    def fake_fetch(location, api_key, client=None, timeout=8.0):
        return weather_mod.normalize_google_hours(recorded)

    monkeypatch.setattr(routes_ad, "fetch_live", fake_fetch)
    with _client_for(settings, production, rbc) as c:
        resp = c.post("/weather/refresh", follow_redirects=False)
        assert resp.status_code == 303
        assert "Forecast+refreshed" in resp.headers["location"]

        cache_path = settings.db_path.parent / "weather_cache.json"
        data = weather_mod.read_cache(cache_path)
        assert set(data) == {"L-STAGE4", "L-RANCH", "L-MAINST"}

        home = c.get("/")
        assert "source: live" in home.text
        assert "precip 90%" in home.text  # live storm data beats the fixture
        assert "live via Google Weather" in home.text


def test_refresh_failure_keeps_fixture_label(settings, production, rbc, tmp_path, monkeypatch):
    from app.web import routes_ad

    settings.google_maps_api_key = "test-key"
    settings.db_path = tmp_path / "t.db"

    def failing_fetch(location, api_key, client=None, timeout=8.0):
        return None

    monkeypatch.setattr(routes_ad, "fetch_live", failing_fetch)
    with _client_for(settings, production, rbc) as c:
        resp = c.post("/weather/refresh", follow_redirects=False)
        assert resp.status_code == 303
        assert "failed" in resp.headers["location"]
        home = c.get("/")
        assert "source: fixture" in home.text  # demo keeps running offline


def test_refresh_cache_write_failure_keeps_previous_forecast(
    settings, production, rbc, tmp_path, monkeypatch
):
    """A failing cache write (e.g. Windows replace race) must not 500 the route."""
    from app.web import routes_ad

    settings.google_maps_api_key = "test-key"
    settings.db_path = tmp_path / "t.db"

    def fake_fetch(location, api_key, client=None, timeout=8.0):
        return [weather.Hour(840, 900, 90, 20, False, "Rain")]

    def broken_save(*args, **kwargs):
        raise OSError("[WinError 5] Access is denied")

    monkeypatch.setattr(routes_ad, "fetch_live", fake_fetch)
    monkeypatch.setattr(routes_ad, "save_live_forecasts", broken_save)
    with _client_for(settings, production, rbc) as c:
        resp = c.post("/weather/refresh", follow_redirects=False)
        assert resp.status_code == 303  # never an unhandled 500
        assert "failed" in resp.headers["location"]
        home = c.get("/")
        assert home.status_code == 200
        assert "source: fixture" in home.text  # previous forecast data intact


def test_manual_incident_rejects_invalid_blocked_window(client):
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "manual_blocked_from": "25:99",
        },
    )
    assert resp.status_code == 200
    assert "Invalid blocked window" in resp.text


def test_manual_incident_rejects_inverted_window(client):
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "manual_blocked_from": "14:00",
        },
    )
    assert resp.status_code == 200
    assert "Invalid blocked window" in resp.text


def test_weather_dashboard_defends_against_broken_module(client, monkeypatch):
    from app.web import routes_ad

    def boom(*args, **kwargs):
        raise RuntimeError("forecast provider down")

    monkeypatch.setattr(routes_ad, "resolve_forecast", boom)
    home = client.get("/")
    assert home.status_code == 200
    assert "Weather watch" not in home.text  # card omitted, page still renders
    assert "Report a disruption" in home.text
