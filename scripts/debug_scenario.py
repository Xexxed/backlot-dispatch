"""Debug helper: inspect one replan scenario pass-by-pass."""
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.engine import (
    _meal_index,
    _repair_blocked,
    _repair_daylight,
    _repair_dependencies,
    baseline_context,
    make_context,
    replan,
)
from app.importers import load_production
from app.models import Incident, minutes_to_hhmm
from app.rulebook import RuleBookContext, load_rulebook
from app.rules import validate_order
from app.timeline import BlockedWindow, build_timeline

production, travel = load_production(Path("seed"))
rbc = RuleBookContext(load_rulebook(), travel)

incident = Incident(
    type="LOCATION_BLOCKED",
    location_id="L-STAGE4",
    blocked_until="14:00",
    severity="high",
    free_text="Generator down",
    source="manual_form",
)
now = 570
completed = production.scene_order[:5]
pending = [s for s in production.scene_order if s not in set(completed)]

windows = [
    BlockedWindow("L-STAGE4", min(now, 840), 840)
]
ctx = make_context(production, rbc, earliest_start=max(now, production.call_time),
                   blocked_windows=windows)

order = list(pending)


def show(label):
    slots = build_timeline(order, ctx)
    viols = validate_order([s for s in order], ctx)
    print(f"\n=== {label} ===")
    for s in slots:
        tag = "LUNCH" if s.is_meal else s.item_id
        mark = " <-- BLOCKED" if (not s.is_meal and ctx.is_blocked(s.location_id, s.start, s.end)) else ""
        print(f"  {minutes_to_hhmm(s.start)}-{minutes_to_hhmm(s.end)} {tag:8s} {s.location_id or '-'}{mark}")
    print("  violations:", [(v.rule_id, v.scene_ids) for v in viols][:6])


show("baseline pending")

order, c1 = _repair_blocked(order, ctx)
show("after BLOCKED repair")
print("  changes:", [(c.scene_id, c.rule_id) for c in c1])

order, c2 = _repair_dependencies(order, production)
show("after DEPS repair")

order, c3 = _repair_daylight(order, ctx)
show("after DAYLIGHT repair")
