"""
Loads the 2026-27 master class schedule (KG 1 to Grade 7) into the timetable.

Source: "School Timetables (KG 1 - Grade 7).pdf". Times are entered in India time, the zone
School Settings runs the timetable in (the PDF gives both GST and IST):

  * KG 1 and KG 2, every weekday:   P1 10:30  P2 11:15  P3 12:15  P4 13:00  P5 14:30  (45 min)
  * Grades 1-7, Monday to Thursday: the same as KG
  * Grades 1-7, Friday (early shift): P1 09:30  P2 10:15  P3 11:15  P4 12:00  P5 12:45

Decisions taken with the school (2026-10-28):
  * GAW is GK; ARABIC-SUM is ARABIC; ART is ART & CRAFT; MS / VE is VE / MS; RHYME / STORY is
    RHYMES & STORIES.
  * A shared slot ("MUSIC / ART", "ARABIC / MAL") is two entries in the same period, one per
    subject and its teacher - the way Grade 7 was first entered.
  * Grade 7 was first entered in Gulf time, so its periods opened 1.5 hours early. Its old
    entries are ended the day before this schedule starts (kept, not deleted) and it is
    entered afresh in India time.
  * Everything applies from 2026-10-28. Each period's teacher is the class's mapped teacher
    for that subject.

    python -m scripts.import_timetable_2026_27                  # dry run: the plan and any clash
    python -m scripts.import_timetable_2026_27 --apply          # write it
    python -m scripts.import_timetable_2026_27 --undo           # what undoing would do
    python -m scripts.import_timetable_2026_27 --undo --apply   # remove it, restore Grade 7

Safe to run twice: entries this import already made are recognised by their tag and skipped.
"""

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from app.core.firebase import (
    firestore_classes, firestore_subjects, firestore_teacher_mappings, firestore_timetable, firestore_users,
)

TAG = "timetable-2026-27"
STARTS = date(2026, 10, 28)
GRADE7_OLD_ENDS = STARTS - timedelta(days=1)

STANDARD = ["10:30", "11:15", "12:15", "13:00", "14:30"]
FRIDAY_EARLY = ["09:30", "10:15", "11:15", "12:00", "12:45"]
LENGTH = timedelta(minutes=45)

# What the PDF writes -> the subjects in the LMS. A list is a shared slot.
SUBJECT = {
    "ENG": ["ENGLISH"], "MAL": ["MALAYALAM"], "MATH": ["MATHS"], "HINDI": ["HINDI"], "IT": ["IT"],
    "ABACUS": ["ABACUS"], "ARABIC": ["ARABIC"], "ARABIC-SUM": ["ARABIC"], "SCIENCE": ["SCIENCE"],
    "SST": ["SST"], "GK": ["GK"], "GAW": ["GK"], "ART": ["ART & CRAFT"], "MS/VE": ["VE / MS"],
    "RHYME/STORY": ["RHYMES & STORIES"], "MUSIC": ["MUSIC"], "MUSIC/ART": ["MUSIC", "ART & CRAFT"],
    "ARABIC/MAL": ["ARABIC", "MALAYALAM"],
}

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]

# Class name in the LMS -> (Friday early shift?, Mon..Fri, each P1..P5; None = no period).
SCHEDULE = {
    "KG 1": (False, [
        [None, "MATH", "ENG", "RHYME/STORY", None],
        [None, "GAW", "MAL", "ARABIC", None],
        [None, "MATH", "MAL", "ENG", None],
        [None, "ART", "RHYME/STORY", "ENG", None],
        [None, "ENG", "GAW", "ARABIC", None],
    ]),
    "KG2": (False, [
        ["ENG", "MAL", "MATH", "ARABIC-SUM", "MUSIC"],
        ["ENG", "ART", "GAW", "ARABIC-SUM", "MAL"],
        ["ENG", "RHYME/STORY", "MATH", "ARABIC-SUM", "GAW"],
        ["ENG", "MATH", "RHYME/STORY", "ARABIC-SUM", "MAL"],
        ["ENG", "GAW", "RHYME/STORY", "MATH", None],
    ]),
    "GRADE 1": (True, [
        ["ENG", "MAL", "HINDI", "IT", "ABACUS"],
        ["MAL", "ENG", "ARABIC", "HINDI", "MUSIC/ART"],
        ["MATH", "ARABIC", "MAL", "SCIENCE", "SST"],
        ["IT", "HINDI", "MATH", "ENG", "ARABIC"],
        ["SST", "MS/VE", "ENG", "GK", "SCIENCE"],
    ]),
    "GRADE 2": (True, [
        ["SCIENCE", "ARABIC", "ENG", "MS/VE", "ARABIC"],
        ["ENG", "MAL", "IT", "MATH", "GK"],
        ["HINDI", "ENG", "SCIENCE", "ABACUS", "HINDI"],
        ["MATH", "ARABIC", "ENG", "SST", "IT"],
        ["MAL", "HINDI", "SST", "MAL", "MUSIC/ART"],
    ]),
    "GRADE 3": (True, [
        ["MATH", "MS/VE", "IT", "SST", "MAL"],
        ["MUSIC/ART", "ARABIC", "HINDI", "IT", "MAL"],
        ["ENG", "MAL", "ENG", "MATH", "GK"],
        ["HINDI", "ENG", "ARABIC", "SCIENCE", "ABACUS"],
        ["ENG", "ARABIC", "SCIENCE", "HINDI", "SST"],
    ]),
    "GRADE 4": (True, [
        ["IT", "MATH", "MATH", "HINDI", "SCIENCE"],
        ["MATH", "ENG", "ENG", "SST", "ARABIC"],
        ["MAL", "MATH", "MS/VE", "SCIENCE", "ABACUS"],
        ["ENG", "MAL", "SST", "SCIENCE", "GK"],
        ["HINDI", "ENG", "ARABIC", "MUSIC/ART", "IT"],
    ]),
    "GRADE 5": (True, [
        ["SST", "HINDI", "ARABIC", "MATH", "MATH"],
        ["IT", "MATH", "GK", "SCIENCE", "IT"],
        ["ENG", "SST", "HINDI", "IT", "ARABIC"],
        ["MAL", "ENG", "MS/VE", "MUSIC/ART", "SCIENCE"],
        ["MATH", "MAL", "MATH", "SCIENCE", "SCIENCE"],
    ]),
    "GRADE 6": (True, [
        ["ENG", "GK", "MAL", "SCIENCE", "SST"],
        ["SST", "HINDI", "MATH", "IT", "SCIENCE"],
        ["ARABIC", "ENG", "SCIENCE", "MUSIC/ART", "MATH"],
        ["SST", "MATH", "HINDI", "IT", "MATH"],
        ["SCIENCE", "SST", "MAL", "MATH", "ARABIC"],
    ]),
    "GRADE 7": (True, [
        ["ARABIC/MAL", "ENG", "SCIENCE", "IT", "GK"],
        ["ENG", "SST", "SST", "MUSIC/ART", "MATH"],
        ["IT", "HINDI", "MATH", "HINDI", "SCIENCE"],
        ["IT", "SST", "SCIENCE", "MATH", "MAL"],
        ["ARABIC", "MATH", "SCIENCE", "SST", "MATH"],
    ]),
}


def _end(start: str) -> str:
    return (datetime.combine(STARTS, time.fromisoformat(start)) + LENGTH).strftime("%H:%M")


def _minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def plan() -> tuple[list[dict], list[str]]:
    """The entries to create, and anything that stops one being made."""
    classes = {c.get("name"): c for c in firestore_classes.list_all()}
    subjects = {s.get("name"): s for s in firestore_subjects.list_all()}
    users = {str(u["id"]): u.get("full_name") for u in firestore_users.list_all()}
    mappings = defaultdict(list)
    for m in firestore_teacher_mappings.list_all():
        mappings[(m.get("class_id"), m.get("subject_id"))].append(m.get("teacher_id"))

    entries, problems = [], []
    # Where each teacher already is, so a subject with two mapped teachers goes to one who is
    # free then (KG 2's Rhymes & Stories, which Rimsha and Sheeja both teach).
    busy: dict[tuple, list[tuple[int, int]]] = defaultdict(list)

    def free_teacher(teachers: list, day: str, start: str):
        span = (_minutes(start), _minutes(start) + 45)
        for teacher_id in teachers:
            if all(span[1] <= s or e <= span[0] for s, e in busy[(teacher_id, day)]):
                return teacher_id
        return teachers[0] if teachers else None

    for class_name, (friday_early, week) in SCHEDULE.items():
        class_room = classes.get(class_name)
        if not class_room:
            problems.append(f"no class named {class_name!r}")
            continue
        for day, row in zip(DAYS, week):
            times = FRIDAY_EARLY if (day == "FRIDAY" and friday_early) else STANDARD
            for period, code in enumerate(row):
                if code is None:
                    continue
                for subject_name in SUBJECT[code]:
                    subject = subjects.get(subject_name)
                    if not subject:
                        problems.append(f"no subject named {subject_name!r} ({class_name} {day} P{period + 1})")
                        continue
                    teachers = mappings.get((class_room["id"], subject["id"])) or []
                    teacher_id = free_teacher(teachers, day, times[period])
                    if teacher_id is not None:
                        busy[(teacher_id, day)].append((_minutes(times[period]), _minutes(times[period]) + 45))
                    entries.append({
                        "class_id": class_room["id"],
                        "class_name": class_name,
                        "subject_id": subject["id"],
                        "subject_name": subject_name,
                        "teacher_id": teacher_id,
                        "teacher_name": users.get(str(teacher_id)) if teacher_id else None,
                        "other_teachers": [users.get(str(t)) for t in teachers if t != teacher_id],
                        "day_of_week": day,
                        "start_time": times[period],
                        "end_time": _end(times[period]),
                        "period_label": f"P{period + 1}",
                    })
    return entries, problems


def teacher_clashes(entries: list[dict]) -> list[str]:
    """A teacher timetabled in two places at once by this schedule."""
    by_teacher = defaultdict(list)
    for e in entries:
        if e["teacher_id"] is not None:
            by_teacher[(e["teacher_id"], e["day_of_week"])].append(e)
    clashes = []
    for (_, day), rows in by_teacher.items():
        rows.sort(key=lambda e: e["start_time"])
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if _minutes(b["start_time"]) < _minutes(a["end_time"]) and a["class_id"] != b["class_id"]:
                    clashes.append(
                        f"{a['teacher_name']}: {day.title()} {a['start_time']} has {a['class_name']} {a['subject_name']} "
                        f"and {b['class_name']} {b['subject_name']}"
                    )
    return clashes


def already_imported() -> list[dict]:
    return [e for e in firestore_timetable.list_all() if e.get("import_tag") == TAG]


def grade7_old() -> list[dict]:
    grade7 = next((c for c in firestore_classes.list_all() if c.get("name") == "GRADE 7"), None)
    if not grade7:
        return []
    return [
        e for e in firestore_timetable.query_documents("class_id", "==", grade7["id"])
        if e.get("import_tag") != TAG
    ]


def run(apply: bool) -> None:
    entries, problems = plan()
    print(f"Planned entries: {len(entries)} across {len({e['class_id'] for e in entries})} classes")
    for class_name in SCHEDULE:
        n = sum(1 for e in entries if e["class_name"] == class_name)
        print(f"  {class_name}: {n}")
    no_teacher = sorted({f"{e['class_name']} {e['subject_name']}" for e in entries if e["teacher_id"] is None})
    if no_teacher:
        print("Periods with no mapped teacher (entered without one):")
        for item in no_teacher:
            print("  -", item)
    shared = sorted({
        f"{e['class_name']} {e['subject_name']}: {e['teacher_name']} (also mapped: {', '.join(t for t in e['other_teachers'] if t)})"
        for e in entries if e["other_teachers"]
    })
    if shared:
        print("Subjects with two mapped teachers (the one free at the time is used):")
        for item in shared:
            print("  -", item)
    clashes = teacher_clashes(entries)
    print(f"Teacher clashes within the schedule: {len(clashes)}")
    for c in clashes:
        print("  !", c)
    if problems:
        print("Problems:")
        for p in problems:
            print("  x", p)

    done = {(e.get("class_id"), e.get("subject_id"), e.get("day_of_week"), e.get("start_time")) for e in already_imported()}
    todo = [e for e in entries if (e["class_id"], e["subject_id"], e["day_of_week"], e["start_time"]) not in done]
    old = [e for e in grade7_old() if not e.get("effective_to")]
    print(f"Already imported: {len(done)}; to create now: {len(todo)}")
    print(f"Grade 7 entries to end on {GRADE7_OLD_ENDS}: {len(old)}")

    if not apply:
        print("\nDry run - nothing written. Re-run with --apply to write it.")
        return
    if problems:
        raise SystemExit("Not writing: fix the problems above first.")

    now = datetime.utcnow().isoformat()
    for e in old:
        firestore_timetable.add_document(str(e["id"]), {
            "effective_to": GRADE7_OLD_ENDS.isoformat(),
            "ended_by_import": TAG,
            "updated_at": now,
        })
    for e in todo:
        entry_id = firestore_timetable.get_next_numeric_id()
        firestore_timetable.add_document(str(entry_id), {
            "class_id": e["class_id"],
            "subject_id": e["subject_id"],
            "teacher_id": e["teacher_id"],
            "day_of_week": e["day_of_week"],
            "start_time": e["start_time"],
            "end_time": e["end_time"],
            "room": None,
            "period_label": e["period_label"],
            "effective_from": STARTS.isoformat(),
            "effective_to": None,
            "is_active": True,
            "created_at": now,
            "import_tag": TAG,
        })
    print(f"\nWritten: {len(todo)} entries created, {len(old)} old Grade 7 entries ended.")


def undo(apply: bool) -> None:
    made = already_imported()
    ended = [e for e in grade7_old() if e.get("ended_by_import") == TAG]
    print(f"Entries this import made: {len(made)}; Grade 7 entries it ended: {len(ended)}")
    if not apply:
        print("Dry run - nothing removed. Re-run with --undo --apply to remove them.")
        return
    for e in made:
        firestore_timetable.delete_document(str(e["id"]))
    now = datetime.utcnow().isoformat()
    for e in ended:
        firestore_timetable.add_document(str(e["id"]), {"effective_to": None, "ended_by_import": None, "updated_at": now})
    print(f"Removed {len(made)} entries and restored {len(ended)} Grade 7 entries.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--undo", action="store_true", help="remove what this import made")
    args = parser.parse_args()
    undo(args.apply) if args.undo else run(args.apply)
