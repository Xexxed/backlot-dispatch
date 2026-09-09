"""Critic agent: recommend-only contract + deterministic fallback parity.

Invariants under test (plan §2):
  * fallback output is schema-identical to the Gemini path (parity contract);
  * critic output can NEVER change is_feasible or any stored plan state;
  * offline / transport failure → deterministic cheapest-feasible fallback;
  * every path is logged to /debug/gcp as kind `critic`;
  * sandbox page renders the recommendation without changing option order.
"""
from __future__ import annotations

from app.agents.critic import CriticRecommendation, manual_recommendation, recommend
from app.serialize import option_stats


def _stats(strategy: str, *, feasible=True, moves=2, wrap_delta=60, cost=None):
    stats = option_stats(
        {
            "strategy": strategy,
            "status": "proposed",
            "is_feasible": feasible,
            "changes": [{"kind": "MOVE"}] * moves,
            "diagnostics": [],
            "baseline_timeline": [],
            "proposed_timeline": [
                {"item_id": "s", "start": 0, "end": wrap_delta, "location_id": None, "label": ""}
            ],
            "now_minutes": 0,
        }
    )
    if cost is not None:  # option_stats passes cost through when callers supply it
        stats["cost_total"] = cost
        stats["penalty_meals"] = 0
    return stats


# ------------------------------------------------------------- fallback core
def test_fallback_prefers_cheapest_feasible():
    options = [
        _stats("minimal", cost=2500),
        _stats("cover_set", cost=1180),
        _stats("hold", feasible=False, cost=7600),
    ]
    rec = manual_recommendation(options)
    assert rec.recommended_strategy == "cover_set"
    assert rec.source == "fallback"
    assert rec.confidence == 1.0
    assert any("feasib" in r for r in rec.reasons)


def test_fallback_without_cost_uses_moves_then_wrap():
    options = [
        _stats("minimal", moves=5, wrap_delta=90),
        _stats("cover_set", moves=1, wrap_delta=120),
    ]
    rec = manual_recommendation(options)
    assert rec.recommended_strategy == "cover_set"  # fewer moves wins


def test_fallback_when_nothing_feasible_picks_best_effort():
    options = [_stats("a", feasible=False, moves=1), _stats("b", feasible=False, moves=4)]
    rec = manual_recommendation(options)
    assert rec.recommended_strategy == "a"
    assert any("infeasible" in r for r in rec.reasons)


def test_fallback_empty_input_is_safe():
    rec = manual_recommendation([])
    assert rec.recommended_strategy == ""


# ------------------------------------------------------------- parity/inert
def test_fallback_matches_gemini_schema_shape():
    """Parity contract (mirrors test_intake_schema.py): the fallback and the
    model path produce the same recommendation object shape."""
    gemini_shaped = CriticRecommendation(
        "cover_set", 0.9, ["cheapest feasible"], "gemini"
    )
    fallback = manual_recommendation([_stats("cover_set", cost=1)])
    assert list(gemini_shaped.__dict__) == list(fallback.__dict__)
    for field in ("recommended_strategy", "confidence", "reasons", "source"):
        assert isinstance(getattr(fallback, field), type(getattr(gemini_shaped, field)))


def test_recommend_never_raises_and_needs_two_options(settings):
    settings.project_id = ""
    settings.api_key = ""
    options = [_stats("minimal")]
    rec = recommend(options, settings, None)  # <2 options → instant fallback
    assert rec.recommended_strategy == "minimal"
    assert rec.source == "fallback"


def test_recommend_transport_failure_falls_back_and_logs(settings, tmp_path):
    from app.store import Store

    settings.project_id = ""  # unconfigured → fallback without any SDK import
    settings.api_key = ""
    store = Store(tmp_path / "c.db")
    rec = recommend([_stats("minimal"), _stats("hold")], settings, store)
    assert rec.source == "fallback"
    calls = store.recent_gcp_calls()
    assert calls and calls[0]["kind"] == "critic"
    assert calls[0]["ok"] == 0
    store.close()


def test_recommend_offline_unconfigured_logs_not_configured(settings, tmp_path):
    from app.store import Store

    store = Store(tmp_path / "c2.db")
    recommend([_stats("minimal"), _stats("hold")], settings, store)
    call = store.recent_gcp_calls()[0]
    assert call["kind"] == "critic" and call["ok"] == 0
    assert "not_configured" in call["meta_json"]
    store.close()


# ----------------------------------------------------------------- e2e page
def test_sandbox_renders_critic_pick_and_keeps_order(client):
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
    assert resp.status_code == 303
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    resp = client.get(f"/sandbox/{gid}")
    assert resp.status_code == 200
    assert "Critic recommends" in resp.text
    assert "can never reorder scenes" in resp.text
    assert "critic pick" in resp.text
    # Option cards still render in strategy order (critic cannot reorder).
    pos = {
        s: resp.text.index(label)
        for s, label in (
            ("minimal", "Minimal change"),
            ("cover_set", "Cover-set pivot"),
            ("hold", "Hold &amp; wait"),
        )
    }
    assert pos["minimal"] < pos["cover_set"] < pos["hold"]


def test_critic_never_changes_feasibility_or_state(client):
    """Pin the invariant: rendering the sandbox (critic included) must not
    mutate any plan payload — is_feasible, statuses, order all identical."""
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
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    before = {
        p["id"]: (dict(p), p["is_feasible"], p["status"], tuple(p["proposed_order"]))
        for p in client.app.state.store.plans_in_group(gid)
    }
    client.get(f"/sandbox/{gid}")  # critic runs here
    after = {
        p["id"]: (dict(p), p["is_feasible"], p["status"], tuple(p["proposed_order"]))
        for p in client.app.state.store.plans_in_group(gid)
    }
    assert before == after


def test_critic_call_logged_in_gcp_evidence(client):
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
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    client.get(f"/sandbox/{gid}")
    calls = client.app.state.store.recent_gcp_calls()
    assert any(c["kind"] == "critic" for c in calls)
