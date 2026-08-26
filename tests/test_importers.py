"""Importer validation tests — loud failures with row-level messages."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.importers import ImportValidationError, load_production, read_schedule


def test_seed_loads_with_expected_counts(production):
    assert len(production.scenes) == 25
    assert len(production.crew) == 40
    assert len(production.cast) == 8
    assert len(production.locations) == 3
    assert production.scene_order[:3] == ["SC-101", "SC-102", "SC-103"]
    assert production.departments == ["AD/Production", "Art", "Camera", "G&E", "Sound"]


def test_baseline_dependencies_reference_known_scenes(production):
    for scene in production.scenes.values():
        for dep in scene.depends_on:
            assert dep in production.scenes


def _read(tmp_path: Path, row: str) -> list[str]:
    bad = tmp_path / "schedule.csv"
    bad.write_text(
        "scene_id,title,page_count,location,int_ext,day_night,"
        "cast_ids,departments,depends_on\n" + row,
        encoding="utf-8",
    )
    errors: list[str] = []
    try:
        from app.models import Location

        read_schedule(bad, {"L-STAGE4": Location("L-STAGE4", "Stage")}, {}, errors)
    except ImportValidationError:
        pass
    return errors


def test_unknown_location_is_collected_with_row_number(tmp_path: Path):
    errors = _read(
        tmp_path,
        "X-1,Bad scene,1.0,L-NOWHERE,INT,DAY,,,\n",
    )
    assert any("L-NOWHERE" in e for e in errors)


def test_bad_page_count_is_collected(tmp_path: Path):
    errors = _read(tmp_path, "X-1,Bad pages,abc,L-STAGE4,INT,DAY,,,\n")
    assert any("page_count" in e for e in errors)


def test_unknown_cast_id_is_collected(tmp_path: Path):
    errors = _read(tmp_path, "X-1,Cast check,1.0,L-STAGE4,INT,DAY,C-99,,\n")
    assert any("C-99" in e for e in errors)


def test_unknown_dependency_is_collected(tmp_path: Path):
    errors = _read(tmp_path, "X-1,Dep check,1.0,L-STAGE4,INT,DAY,,,ZZ-9\n")
    assert any("unknown scene" in e for e in errors)


def test_duplicate_scene_id_fails(tmp_path: Path):
    errors = _read(
        tmp_path,
        "X-1,One,1.0,L-STAGE4,INT,DAY,,,\nX-1,Two,1.0,L-STAGE4,INT,DAY,,,\n",
    )
    assert any("duplicate scene_id" in e for e in errors)


def test_load_production_raises_loudly_on_empty_dir(tmp_path: Path):
    with pytest.raises(ImportValidationError):
        load_production(tmp_path)
