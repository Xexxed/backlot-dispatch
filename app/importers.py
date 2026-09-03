"""CSV importers with strict, well-reported validation.

Files (utf-8, BOM tolerated), exported from Movie Magic / Excel:
  locations.csv : location_id,name
  travels.csv   : from_location,to_location,minutes   (one direction is enough)
  cast.csv      : cast_id,name,character
  crew.csv      : crew_id,name,department,role,contact
  schedule.csv  : scene_id,title,page_count,location,int_ext,day_night,
                  cast_ids,departments,depends_on     (';' separated lists)

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
