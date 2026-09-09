"""CSV importers with strict, well-reported validation.

Files (utf-8, BOM tolerated), exported from Movie Magic / Excel:
  locations.csv : location_id,name
  travels.csv   : from_location,to_location,minutes   (one direction is enough)
  cast.csv      : cast_id,name,character
  crew.csv      : crew_id,name,department,role,contact
  schedule.csv  : scene_id,title,page_count,location,int_ext,day_night,
                  cast_ids,departments,depends_on     (';' separated lists)
  rates.csv     : department,hourly_rate,ot_multiplier,ot_threshold_hours,
                  meal_penalty_per_person,company_move_cost   (optional; the
                  cost ledger is enabled only when this file exists; a `*`
                  wildcard department prices unknown departments)

All cross-references are validated; every problem is collected and reported
with its row number — imports either succeed completely or fail loudly.
"""
from __future__ import annotations

import csv
from pathlib import Path

from app.models import (
    CastMember,
    CrewMember,
    DayNight,
    IntExt,
    Location,
    Production,
    Scene,
    hhmm_to_minutes,
)


class ImportValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        summary = "; ".join(errors[:5]) + (" …" if len(errors) > 5 else "")
        super().__init__(f"{len(errors)} import error(s): {summary}")


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = [f.strip() for f in (reader.fieldnames or [])]
        for i, raw in enumerate(reader, start=2):  # header is line 1
            row = {
                (k.strip() if isinstance(k, str) else k): (v.strip() if isinstance(v, str) else v)
                for k, v in raw.items()
            }
            if not any(row.values()):
                continue
            yield i, row, required


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def read_locations(path: Path, errors: list[str]) -> dict[str, Location]:
    out: dict[str, Location] = {}
    for line, row, _ in _rows(path):
        lid = row.get("location_id", "")
        name = row.get("name", "")
        if not lid or not name:
            errors.append(f"{path.name}:{line}: location_id and name are required")
            continue
        if lid in out:
            errors.append(f"{path.name}:{line}: duplicate location_id {lid!r}")
            continue
        try:
            lat = float(row["lat"]) if row.get("lat") else None
            lng = float(row["lng"]) if row.get("lng") else None
        except ValueError:
            errors.append(f"{path.name}:{line}: lat/lng must be numeric when present")
            continue
        out[lid] = Location(id=lid, name=name, lat=lat, lng=lng)
    return out


def read_travel(path: Path, errors: list[str]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for line, row, _ in _rows(path):
        a, b = row.get("from_location", ""), row.get("to_location", "")
        try:
            minutes = int(row.get("minutes", ""))
        except ValueError:
            errors.append(f"{path.name}:{line}: minutes must be an integer")
            continue
        if not a or not b:
            errors.append(f"{path.name}:{line}: from_location and to_location are required")
            continue
        out[(a, b)] = minutes
    return out


def read_cast(path: Path, errors: list[str]) -> dict[str, CastMember]:
    out: dict[str, CastMember] = {}
    for line, row, _ in _rows(path):
        cid = row.get("cast_id", "")
        if not cid:
            errors.append(f"{path.name}:{line}: cast_id is required")
            continue
        if cid in out:
            errors.append(f"{path.name}:{line}: duplicate cast_id {cid!r}")
            continue
        out[cid] = CastMember(
            id=cid, name=row.get("name", ""), character=row.get("character", "")
        )
    return out


def read_crew(path: Path, errors: list[str]) -> list[CrewMember]:
    out: list[CrewMember] = []
    seen: set[str] = set()
    for line, row, _ in _rows(path):
        cid = row.get("crew_id", "")
        department = row.get("department", "")
        if not cid or not department:
            errors.append(f"{path.name}:{line}: crew_id and department are required")
            continue
        if cid in seen:
            errors.append(f"{path.name}:{line}: duplicate crew_id {cid!r}")
            continue
        seen.add(cid)
        out.append(
            CrewMember(
                id=cid,
                name=row.get("name", ""),
                department=department,
                role=row.get("role", ""),
                contact=row.get("contact", ""),
            )
        )
    return out


def read_schedule(
    path: Path,
    locations: dict[str, Location],
    cast: dict[str, CastMember],
    errors: list[str],
) -> list[Scene]:
    scenes: list[Scene] = []
    ids_in_order: list[str] = []
    for line, row, _ in _rows(path):
        sid = row.get("scene_id", "")
        prefix = f"{path.name}:{line}"
        if not sid:
            errors.append(f"{prefix}: scene_id is required")
            continue
        if sid in ids_in_order:
            errors.append(f"{prefix}: duplicate scene_id {sid!r}")
            continue
        try:
            page_count = float(row.get("page_count", ""))
        except ValueError:
            errors.append(f"{prefix}: page_count must be numeric")
            continue
        if page_count <= 0:
            errors.append(f"{prefix}: page_count must be positive")
            continue
        location_id = row.get("location", "")
        if location_id not in locations:
            errors.append(f"{prefix}: unknown location {location_id!r}")
            continue
        try:
            int_ext = IntExt(row.get("int_ext", "").upper())
            day_night = DayNight(row.get("day_night", "").upper())
        except ValueError:
            errors.append(f"{prefix}: int_ext must be INT/EXT; day_night must be DAY/NIGHT")
            continue
        cast_ids = _split_list(row.get("cast_ids"))
        unknown_cast = [c for c in cast_ids if c not in cast]
        if unknown_cast:
            errors.append(f"{prefix}: unknown cast id(s) {', '.join(unknown_cast)}")
            continue
        depends_on = _split_list(row.get("depends_on"))
        ids_in_order.append(sid)
        scenes.append(
            Scene(
                id=sid,
                title=row.get("title", ""),
                page_count=page_count,
                location_id=location_id,
                int_ext=int_ext,
                day_night=day_night,
                cast_ids=cast_ids,
                departments=_split_list(row.get("departments")),
                depends_on=depends_on,  # existence checked after full pass
            )
        )
    known = set(ids_in_order)
    for scene in scenes:
        for dep in scene.depends_on:
            if dep not in known:
                errors.append(f"schedule.csv: scene {scene.id} depends on unknown scene {dep!r}")
    return scenes


def load_production(
    seed_dir: Path,
    production_id: str = "P-001",
    title: str = "Untitled Production",
    shoot_date: str = "2026-09-01",
    call_time: str = "07:00",
) -> tuple[Production, dict[tuple[str, str], int]]:
    """Load and cross-validate a full production from a seed directory."""
    errors: list[str] = []
    required = ("locations.csv", "travels.csv", "cast.csv", "crew.csv", "schedule.csv")
    for name in required:
        if not (seed_dir / name).exists():
            errors.append(f"{name}: file not found in {seed_dir}")
    if errors:
        raise ImportValidationError(errors)
    locations = read_locations(seed_dir / "locations.csv", errors)
    travel = read_travel(seed_dir / "travels.csv", errors)
    cast = read_cast(seed_dir / "cast.csv", errors)
    crew = read_crew(seed_dir / "crew.csv", errors)
    scenes = read_schedule(seed_dir / "schedule.csv", locations, cast, errors)
    if not scenes:
        errors.append("schedule.csv: no scenes imported")
    if not crew:
        errors.append("crew.csv: no crew imported")
    if errors:
        raise ImportValidationError(errors)
    return Production(
        id=production_id,
        title=title,
        shoot_date=shoot_date,
        call_time=hhmm_to_minutes(call_time),
        scenes={s.id: s for s in scenes},
        scene_order=[s.id for s in scenes],
        crew=crew,
        cast=cast,
        locations=locations,
    ), travel


# ------------------------------------------------------------- rates card
_RATE_COLUMNS = (
    "department",
    "hourly_rate",
    "ot_multiplier",
    "ot_threshold_hours",
    "meal_penalty_per_person",
    "company_move_cost",
)


def read_rates(path: Path, errors: list[str]) -> list[dict] | None:
    """Parse rates.csv rows with the same loud, row-numbered validation style
    as the other importers. Returns the raw rows (or None when malformed)."""
    if not path.exists():
        return None
    rows: list[dict] = []
    for line, row, required in _rows(path):
        missing = [col for col in _RATE_COLUMNS if col not in required]
        if missing:
            errors.append(f"{path.name}: missing column(s) {', '.join(missing)}")
            return None
        prefix = f"{path.name}:{line}"
        department = row.get("department", "").strip()
        if not department:
            errors.append(f"{prefix}: department is required")
            continue
        values: dict[str, float] = {}
        for col in ("hourly_rate", "ot_multiplier", "ot_threshold_hours",
                    "meal_penalty_per_person", "company_move_cost"):
            try:
                values[col] = float(row.get(col, ""))
            except ValueError:
                errors.append(f"{prefix}: {col} must be numeric")
                values = {}
                break
        if not values:
            continue
        if values["hourly_rate"] <= 0:
            errors.append(f"{prefix}: hourly_rate must be positive")
            continue
        if values["ot_multiplier"] < 1:
            errors.append(f"{prefix}: ot_multiplier must be >= 1")
            continue
        if values["ot_threshold_hours"] < 1:
            errors.append(f"{prefix}: ot_threshold_hours must be >= 1")
            continue
        if values["meal_penalty_per_person"] < 0 or values["company_move_cost"] < 0:
            errors.append(f"{prefix}: penalty/move cost must be >= 0")
            continue
        values["department"] = department
        rows.append(values)
    return rows


def load_rates(seed_dir: Path):
    """Load the demo rate card; None only when rates.csv does not exist
    (cost ledger off). Malformed files raise ImportValidationError loudly —
    a bad rate card must never silently default a rate."""
    from app.costs import RateCard, RateTier

    path = Path(seed_dir) / "rates.csv"
    if not path.exists():
        return None
    errors: list[str] = []
    rows = read_rates(path, errors)
    if rows is None:
        raise ImportValidationError(errors or [f"{path.name}: unreadable"])
    if errors:
        raise ImportValidationError(errors)
    if not rows:
        raise ImportValidationError([f"{path.name}: no rate rows imported"])

    departments: dict[str, float] = {}
    tiers: dict[str, list[RateTier]] = {}
    penalty: float | None = None
    move_cost: float | None = None
    wildcard_seen = False
    for row in rows:
        department = row["department"]
        if department == "*":
            wildcard_seen = True
        if department in departments and departments[department] != row["hourly_rate"]:
            raise ImportValidationError(
                [f"{path.name}: conflicting hourly_rate for {department!r}"]
            )
        departments.setdefault(department, row["hourly_rate"])
        tier = RateTier(row["ot_threshold_hours"], row["ot_multiplier"])
        existing = tiers.setdefault(department, [])
        if any(t.threshold_hours == tier.threshold_hours for t in existing):
            raise ImportValidationError(
                [f"{path.name}: duplicate ot_threshold_hours {tier.threshold_hours} for {department!r}"]
            )
        existing.append(tier)
        if penalty is None:
            penalty = row["meal_penalty_per_person"]
        elif penalty != row["meal_penalty_per_person"]:
            raise ImportValidationError(
                [f"{path.name}: meal_penalty_per_person must be one global value"]
            )
        if move_cost is None:
            move_cost = row["company_move_cost"]
        elif move_cost != row["company_move_cost"]:
            raise ImportValidationError(
                [f"{path.name}: company_move_cost must be one global value"]
            )
    if not wildcard_seen:
        raise ImportValidationError(
            [f"{path.name}: a '*' wildcard department row is required"]
        )
    return RateCard(
        departments=departments,
        ot_tiers=tiers,
        meal_penalty_per_person=penalty or 0.0,
        company_move_cost=move_cost or 0.0,
    )
