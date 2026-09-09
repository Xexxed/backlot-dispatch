"""Agent trace: RunRecorder contract + /agents Control Room rendering.

Invariants under test (plan §1):
  * a run records ordered steps with monotonic seq and a terminal status;
  * recorder failures NEVER change pipeline behavior (steps/runs swallow);
  * body exceptions inside a step are recorded then re-raised unchanged;
  * old databases without the agent tables migrate on boot;
  * /agents renders the trace behind AD auth; /debug/gcp stays untouched.
"""
from __future__ import annotations

import pytest

from app.trace import RunRecorder


# --------------------------------------------------------------- recorder
def test_run_records_ordered_steps_with_monotonic_seq(tmp_path):
    from app.store import Store

    store = Store(tmp_path / "t.db")
    rec = RunRecorder(store, "text").start()
    with rec.step("intake", agent="intake", kind="llm", model="m") as stp:
        stp.verdict = "LOCATION_BLOCKED / high"
    with rec.step("options", agent="engine") as stp:
        stp.verdict = "3 options"
    rec.finish("awaiting_human", {"target": "/sandbox/x"})

    run = store.get_run(rec.run_id)
    assert run is not None
    assert run["status"] == "awaiting_human"
    steps = store.steps_for_run(rec.run_id)
    assert [s["seq"] for s in steps] == [1, 2]
    assert [s["name"] for s in steps] == ["intake", "options"]
    assert steps[0]["kind"] == "llm"
    assert steps[0]["model"] == "m"
    assert steps[1]["verdict"] == "3 options"
    assert all(s["status"] == "ok" for s in steps)
    store.close()


def test_step_durations_recorded_and_sane(tmp_path):
    from app.store import Store

    store = Store(tmp_path / "t.db")
    rec = RunRecorder(store, "text").start()
    with rec.step("options", agent="engine") as stp:
        stp.summary = "x"
    rec.finish("ok")
    step = store.steps_for_run(rec.run_id)[0]
    assert step["duration_ms"] is not None and step["duration_ms"] >= 0
    run = store.get_run(rec.run_id)
    assert run["duration_ms"] is not None and run["duration_ms"] >= 0
    store.close()


def test_recorder_failure_never_propagates(tmp_path):
    from app.store import Store

    class BrokenStore:
        def start_run(self, *a, **k):
            raise RuntimeError("store down")

        def add_step(self, *a, **k):
            raise RuntimeError("store down")

        def finish_run(self, *a, **k):
            raise RuntimeError("store down")

    rec = RunRecorder(BrokenStore(), "text").start()  # must not raise
    with rec.step("intake"):  # must not raise
        pass
    rec.finish("ok")  # must not raise


def test_step_body_exception_recorded_then_reraised(tmp_path):
    from app.store import Store

    store = Store(tmp_path / "t.db")
    rec = RunRecorder(store, "text").start()
    with pytest.raises(ValueError, match="boom"):
        with rec.step("intake", agent="intake", kind="llm"):
            raise ValueError("boom")
    # Run left open (the route's own fallback finishes it); the failed step
    # must exist with the exception captured.
    steps = store.steps_for_run(rec.run_id)
    assert steps[0]["status"] == "failed"
    assert "boom" in steps[0]["summary"]
    assert store.get_run(rec.run_id)["status"] == "running"
    rec.finish("failed")
    assert store.get_run(rec.run_id)["status"] == "failed"
    store.close()


def test_next_start_marks_stale_running_rows_failed(tmp_path):
    """Crash mid-request leaves a 'running' row; the next request's start_run
    quarantines it (after the staleness window) — the orphan is visible as
    failed, and a FRESH concurrent run is left untouched."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from app.store import STALE_RUNNING_MIN, Store

    store = Store(tmp_path / "t.db")
    rec1 = RunRecorder(store, "text").start()
    # simulate the crash: backdate rec1's started_at past the staleness
    # window, then open a new run (no finish_run happened for rec1)
    old = (datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_MIN + 1)
           ).isoformat(timespec="seconds")
    store._conn.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                        (old, rec1.run_id))
    store._conn.commit()
    RunRecorder(store, "text").start()
    runs = store.recent_runs(limit=10)
    by_id = {r["id"]: r for r in runs}
    assert by_id[rec1.run_id]["status"] == "failed"
    assert len([r for r in runs if r["status"] == "running"]) == 1
    store.close()


def test_next_start_leaves_fresh_concurrent_run_alone(tmp_path):
    """A 'running' row from a concurrently executing pipeline (recent
    started_at) is NOT quarantined by another run's start_run."""
    from datetime import datetime, timedelta, timezone

    from app.store import Store

    store = Store(tmp_path / "t.db")
    rec1 = RunRecorder(store, "text").start()
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)
              ).isoformat(timespec="seconds")
    store._conn.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                        (recent, rec1.run_id))
    store._conn.commit()
    RunRecorder(store, "text").start()
    assert store.get_run(rec1.run_id)["status"] == "running"
    store.close()


def test_finish_rejects_unknown_status(tmp_path):
    from app.store import Store

    store = Store(tmp_path / "t.db")
    rec = RunRecorder(store, "text").start()
    rec.finish("weird-status")  # coerced to 'failed', never stored verbatim
    assert store.get_run(rec.run_id)["status"] == "failed"
    store.close()


def test_invalid_kind_coerced_to_deterministic(tmp_path):
    from app.store import Store

    store = Store(tmp_path / "t.db")
    rec = RunRecorder(store, "text").start()
    with rec.step("x", kind="not-a-kind"):
        pass
    assert store.steps_for_run(rec.run_id)[0]["kind"] == "deterministic"
    store.close()


# ------------------------------------------------------------- store / e2e
def test_store_migrates_old_db_without_agent_tables(tmp_path):
    """A DB created before this feature gains the new tables on boot —
    existing rows (plans, acks, token_meta) are untouched."""
    import sqlite3

    from app.store import Store

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE plans (id TEXT PRIMARY KEY, created_at TEXT, status TEXT, "
        "incident_json TEXT, payload_json TEXT)"
    )
    conn.execute(
        "INSERT INTO plans VALUES ('x','2026-09-01T09:00:00+00:00','proposed','{}','{}')"
    )
    conn.commit()
    conn.close()
    store = Store(db)
    assert store.get_plan("x") is not None  # pre-existing data survives
    RunRecorder(store, "text").start().finish("ok")
    assert len(store.recent_runs()) == 1  # new tables usable
    store.close()


def test_incident_pipeline_writes_a_visible_trace(client):
    """E2E: the manual intake path opens a run with the full step chain —
    intake (human) → replan → narrate → awaiting_human."""
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "manual_severity": "high",
            "free_text": "Generator down",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    runs = client.app.state.store.recent_runs(limit=5)
    assert runs and runs[0]["status"] == "awaiting_human"
    steps = client.app.state.store.steps_for_run(runs[0]["id"])
    names = [s["name"] for s in steps]
    assert names == ["intake", "options", "options", "options"]
    assert steps[0]["kind"] == "human"
    assert [s["seq"] for s in steps] == [1, 2, 3, 4]


def test_publish_pipeline_records_human_gate_and_deploy(client):
    # Manual blocking incident → sandbox → select option 0 → publish.
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "free_text": "Generator down",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    sandbox_url = resp.headers["location"]  # /sandbox/{gid}
    plans = client.app.state.store.plans_in_group(sandbox_url.rsplit("/", 1)[-1])
    client.post(f"/plans/{plans[0]['id']}/select", follow_redirects=False)
    resp = client.post(f"/plans/{plans[0]['id']}/publish", follow_redirects=False)
    assert resp.status_code == 303

    publish_runs = [r for r in client.app.state.store.recent_runs(limit=10)
                    if r["trigger"] == "publish"]
    assert publish_runs and publish_runs[0]["status"] == "published"
    steps = client.app.state.store.steps_for_run(publish_runs[0]["id"])
    kinds = [s["kind"] for s in steps]
    assert "human" in kinds and "deterministic" in kinds
    approve = next(s for s in steps if s["kind"] == "human")
    assert approve["name"] == "approve"


def test_agents_page_renders_trace(client):
    client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "CAST_DELAY",
            "free_text": "Lead stuck in traffic",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    resp = client.get("/agents")
    assert resp.status_code == 200
    body = resp.text
    assert "Agent runs" in body
    assert "awaiting_human" in body
    assert "intake" in body and "narrate" in body


def test_intake_fallback_records_fallback_run(client):
    """No credentials in the test settings: the Gemini intake falls back to
    the manual form, and the trace must show fallback (not a crash)."""
    resp = client.post(
        "/incident",
        data={"free_text": "Generator down at Stage 4 until 14:00", "now_override": "11:40"},
        follow_redirects=False,
    )
    assert resp.status_code == 200  # fallback form re-render
    runs = client.app.state.store.recent_runs(limit=5)
    assert runs and runs[0]["status"] == "fallback"
    steps = client.app.state.store.steps_for_run(runs[0]["id"])
    assert steps[0]["status"] == "fallback"
    assert steps[0]["name"] == "intake"
