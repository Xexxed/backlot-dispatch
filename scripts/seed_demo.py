"""Generate the committed seed production (deterministic, idempotent).

Writes locations/travels/cast/crew/schedule CSVs into seed/. The baseline day
is authored to be fully compliant with DEFAULT_RULEBOOK so any later
violation is provably caused by the injected disruption, not bad seed data.

Run:  .venv\\Scripts\\python.exe scripts\\seed_demo.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed"

LOCATIONS = """\
location_id,name,lat,lng
L-STAGE4,Stage 4 · KMI Lot,34.4329,-118.5716
L-RANCH,Harper Ranch — Exterior Yard,34.4199,-118.5301
L-MAINST,Main Street · Town Square,34.4208,-118.5550
"""

TRAVELS = """\
from_location,to_location,minutes
L-STAGE4,L-RANCH,40
L-STAGE4,L-MAINST,25
L-RANCH,L-MAINST,35
"""

CAST = """\
cast_id,name,character
C-01,Maya Chen,Delia Harper
C-02,Tom Okafor,Sheriff Reed
C-03,Lena Voss,June Harper
C-04,Ray Delgado,Holt Marrow
C-05,Priya Nair,Mrs. Alvarez
C-06,Sam Whitfield,Young Delia
C-07,Ana Petrov,Dot the Bartender
C-08,Gus Lindqvist,Farmhand Eli
"""

_CREW_ROWS = [
    # Camera (7)
    ("CR-01", "Yusuf Adeyemi", "Camera", "DP"),
    ("CR-02", "Hana Kobayashi", "Camera", "Operator A"),
    ("CR-03", "Peter Novak", "Camera", "Operator B"),
    ("CR-04", "Grace Mbeki", "Camera", "1st AC"),
    ("CR-05", "Leo Fontaine", "Camera", "2nd AC"),
    ("CR-06", "Ruth Okello", "Camera", "DIT"),
    ("CR-07", "Marco Silveira", "Camera", "Loader"),
    # G&E (9)
    ("GE-01", "Dmitri Volkov", "G&E", "Gaffer"),
    ("GE-02", "Aisha Karim", "G&E", "Best Boy Electric"),
    ("GE-03", "Carl Jensen", "G&E", "Electrician"),
    ("GE-04", "Nina Petrova", "G&E", "Electrician"),
    ("GE-05", "Owen Brady", "G&E", "Key Grip"),
    ("GE-06", "Fatima Zahra", "G&E", "Best Boy Grip"),
    ("GE-07", "Paul Osei", "G&E", "Grip"),
    ("GE-08", "Ingrid Larsen", "G&E", "Grip"),
    ("GE-09", "Tomas Ruiz", "G&E", "Generator Tech"),
    # Sound (6)
    ("SO-01", "Evelyn Cho", "Sound", "Sound Mixer"),
    ("SO-02", "Marcus Reid", "Sound", "Boom Operator"),
    ("SO-03", "Sara Haddad", "Sound", "Utility Sound"),
    ("SO-04", "Jonas Weber", "Sound", "Playback"),
    ("SO-05", "Mei-Ling Tan", "Sound", "Sound Utility"),
    ("SO-06", "Kofi Mensah", "Sound", "RF Tech"),
    # Art (8)
    ("AR-01", "Vivienne Laurent", "Art", "Production Designer"),
    ("AR-02", "Diego Fuentes", "Art", "Set Decorator"),
    ("AR-03", "Amara Diallo", "Art", "Set Dresser"),
    ("AR-04", "Nils Andersen", "Art", "Props Master"),
    ("AR-05", "Zoe Papadopoulos", "Art", "Props Assistant"),
    ("AR-06", "Hassan Ali", "Art", "Greensman"),
    ("AR-07", "Clara Bello", "Art", "Art PA"),
    ("AR-08", "Viktor Marek", "Art", "Swing"),
    # AD / Production (10)
    ("AD-01", "Frank Morales", "AD/Production", "1st AD"),
    ("AD-02", "Imani Brooks", "AD/Production", "2nd AD"),
    ("AD-03", "Sean Gallagher", "AD/Production", "2nd 2nd AD"),
    ("AD-04", "Rosa Delacruz", "AD/Production", "Production Coordinator"),
    ("AD-05", "Ken Watanabe-Ito", "AD/Production", "Production Manager"),
    ("AD-06", "Olivia Grant", "AD/Production", "Script Supervisor"),
    ("AD-07", "Andre Botha", "AD/Production", "Location Manager"),
    ("AD-08", "Chloe Martin", "AD/Production", "Set PA"),
    ("AD-09", "Jamal Wright", "AD/Production", "Set PA"),
    ("AD-10", "Petra Kovacs", "AD/Production", "Transportation Captain"),
]

CREW_HEADER = "crew_id,name,department,role\n"


# (id, title, pages, location, int_ext, day_night, cast, deps)
_SCENES = [
    ("SC-101", "Sunrise over the fields", 0.5, "L-RANCH", "EXT", "DAY", ["C-06"], []),
    ("SC-102", "Delia feeds the horses", 1.5, "L-RANCH", "EXT", "DAY", ["C-01", "C-08"], []),
    ("SC-103", "Reed arrives with news", 2.0, "L-RANCH", "EXT", "DAY", ["C-02", "C-01"], []),
    ("SC-104", "Argument at the fence", 2.5, "L-RANCH", "EXT", "DAY", ["C-01", "C-04"], ["SC-103"]),
    ("SC-105", "June watches from porch", 1.0, "L-RANCH", "EXT", "DAY", ["C-03"], []),
    ("SC-106", "Sheriff's office briefing", 2.0, "L-STAGE4", "INT", "DAY", ["C-02", "C-05"], []),
    ("SC-107", "Interrogation begins", 3.0, "L-STAGE4", "INT", "DAY", ["C-02", "C-04"], ["SC-106"]),
    ("SC-108", "Mrs. Alvarez pleads", 1.5, "L-STAGE4", "INT", "DAY", ["C-05", "C-02"], []),
    ("SC-109", "Evidence board closeups", 0.5, "L-STAGE4", "INT", "DAY", [], []),
    ("SC-110", "Delia storms into office", 2.0, "L-STAGE4", "INT", "DAY", ["C-01", "C-02"], ["SC-107"]),
    ("SC-111", "Phone call to the capital", 1.0, "L-STAGE4", "INT", "DAY", ["C-03"], []),
    ("SC-112", "Holt signs the confession", 1.5, "L-STAGE4", "INT", "NIGHT", ["C-04", "C-02"], ["SC-110"]),
    ("SC-113", "Town square gathering", 2.0, "L-MAINST", "EXT", "DAY", ["C-01", "C-02", "C-07"], []),
    ("SC-114", "Speech at the fountain", 2.5, "L-MAINST", "EXT", "DAY", ["C-01"], ["SC-113"]),
    ("SC-115", "Crowd reactions", 1.0, "L-MAINST", "EXT", "DAY", [], []),
    ("SC-116", "Holt is escorted out", 2.0, "L-MAINST", "EXT", "DAY", ["C-04", "C-02"], ["SC-112"]),
    ("SC-117", "Golden hour goodbye", 1.5, "L-MAINST", "EXT", "DAY", ["C-01", "C-03"], []),
    ("SC-118", "Neon diner exterior", 1.0, "L-MAINST", "EXT", "NIGHT", ["C-07"], []),
    ("SC-119", "Diner interior confession", 3.0, "L-STAGE4", "INT", "NIGHT", ["C-01", "C-07"], ["SC-118"]),
    ("SC-120", "Getaway car peels out", 1.0, "L-MAINST", "EXT", "NIGHT", ["C-04", "C-08"], []),
    ("SC-121", "Ranch house at night", 1.5, "L-RANCH", "EXT", "NIGHT", ["C-01", "C-03"], []),
    ("SC-122", "Lantern in the barn", 1.0, "L-RANCH", "INT", "NIGHT", ["C-08"], []),
    ("SC-123", "Delia's letter voiceover", 0.5, "L-STAGE4", "INT", "NIGHT", ["C-01"], []),
    ("SC-124", "Main street empty at dawn", 0.5, "L-MAINST", "EXT", "DAY", [], []),
    ("SC-125", "Final sunrise", 1.0, "L-RANCH", "EXT", "DAY", ["C-01", "C-06"], ["SC-124"]),
]

_BASE_DEPTS = ["Camera", "G&E", "Sound", "AD/Production"]
_ART_DEPTS = _BASE_DEPTS + ["Art"]


def _schedule_csv() -> str:
    lines = [
        "scene_id,title,page_count,location,int_ext,day_night,"
        "cast_ids,departments,depends_on"
    ]
    for sid, title, pages, loc, ie, dn, cast, deps in _SCENES:
        depts = _ART_DEPTS if loc == "L-STAGE4" else _BASE_DEPTS
        title_safe = title.replace(",", ";")
        lines.append(
            f"{sid},{title_safe},{pages},{loc},{ie},{dn},"
            f"{';'.join(cast)},{';'.join(depts)},{';'.join(deps)}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    SEED.mkdir(exist_ok=True)
    (SEED / "locations.csv").write_text(LOCATIONS, encoding="utf-8")
    (SEED / "travels.csv").write_text(TRAVELS, encoding="utf-8")
    (SEED / "cast.csv").write_text(CAST, encoding="utf-8")
    (SEED / "crew.csv").write_text(CREW_HEADER + "".join(
        f"{cid},{name},{dept},{role.replace(',', ';')}\n"
        for cid, name, dept, role in _CREW_ROWS
    ), encoding="utf-8")
    (SEED / "schedule.csv").write_text(_schedule_csv(), encoding="utf-8")
    print(f"Seed written to {SEED} ({len(_SCENES)} scenes, {len(_CREW_ROWS)} crew)")


if __name__ == "__main__":
    main()
