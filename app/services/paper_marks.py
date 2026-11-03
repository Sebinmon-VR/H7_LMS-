"""
Marks for exams held on paper.

Most of a school's term exams never pass through the LMS: the paper is printed, written in
the hall and marked by hand. The exam module has nothing to say about them, so without this
module a report card could only ever count the online tests - the small part of the term.

The class teacher keeps a register instead: one sheet per exam ("Term 1 Examination"), a
column per subject with what it is out of, a row per student. The marks are stored as rows of
the existing `exam_grades` ledger, so they also show in a student's marks. Rows mirrored from
a published LMS exam carry an `exam_id`; rows from this register carry the `conducted_on` date
instead. Rows the retired gradebook screen typed by hand carry neither, and are deliberately
left out: that screen was only ever used for trials, and its rows were never report card marks.

Two rules carry over from report cards:

* **A blank is not a zero.** A student with no mark on a sheet is absent from it, and the
  card lists the paper as not sat. Clearing a mark deletes the row rather than storing 0.
* **A sheet is its name.** Rows are grouped by exam name within the class, compared without
  case or surrounding spaces, so "Term 1" typed twice is one sheet, not two.
"""

import logging
from collections import Counter
from datetime import date
from typing import Any

from fastapi import HTTPException

from app.core.concurrency import run_parallel
from app.core.enums import GradingScheme, SubmissionStatus
from app.core.firebase import (
    firestore_classes, firestore_grades, firestore_student_enrollments, firestore_subjects,
    firestore_teacher_mappings, firestore_users, generate_id, require_document,
)
from app.services.exams import now_utc

logger = logging.getLogger("paper_marks")


def _key(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())


def _row_date(row: dict) -> date | None:
    """When the paper was written."""
    try:
        return date.fromisoformat(str(row.get("conducted_on"))[:10])
    except ValueError:
        return None


def _most_common(values: list) -> Any:
    present = [v for v in values if v is not None]
    return Counter(present).most_common(1)[0][0] if present else None


def _latest(rows: list[dict]) -> dict:
    return max(rows, key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""))


def paper_rows(class_id: int) -> list[dict]:
    """Every mark this register holds for the class."""
    return [
        g for g in firestore_grades.query_documents("class_id", "==", class_id)
        if not g.get("exam_id") and g.get("conducted_on")
    ]


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    sheets: dict[str, list[dict]] = {}
    for row in rows:
        if _key(row.get("exam_name")):
            sheets.setdefault(_key(row.get("exam_name")), []).append(row)
    return sheets


def _layout(rows: list[dict]) -> list[dict]:
    """
    The subjects the exam had, and what each was out of, as last saved.

    Kept on every row of the sheet (`sheet_subjects`) because a subject ticked before any of
    its marks are typed has no row of its own to remember it by.
    """
    layout = _latest(rows).get("sheet_subjects") or []
    return [s for s in layout if isinstance(s, dict) and s.get("subject_id") is not None]


def _sheet(rows: list[dict], names: dict[int, str]) -> dict:
    """One sheet as the register shows it."""
    by_subject: dict[int, list[dict]] = {}
    for row in rows:
        by_subject.setdefault(row.get("subject_id"), []).append(row)

    layout_max = {int(s["subject_id"]): s.get("max_marks") for s in _layout(rows)}
    order = list(layout_max) + [sid for sid in by_subject if sid is not None and sid not in layout_max]

    subjects = []
    for subject_id in order:
        subject_rows = by_subject.get(subject_id, [])
        by_student: dict[int, list[dict]] = {}
        for row in subject_rows:
            by_student.setdefault(row.get("student_id"), []).append(row)
        subjects.append({
            "subject_id": subject_id,
            "max_marks": layout_max.get(subject_id) or _most_common([r.get("max_marks") for r in subject_rows]),
            "marks": {
                str(student_id): _latest(found)["marks_obtained"]
                for student_id, found in by_student.items()
                if _latest(found).get("marks_obtained") is not None
            },
        })

    stamps = [str(r.get("updated_at") or r.get("created_at") or "") for r in rows]
    authors = {r.get("updated_by") or r.get("teacher_id") for r in rows}
    return {
        "exam_name": _latest(rows).get("exam_name"),
        "conducted_on": _most_common([_row_date(r) for r in rows]),
        "subjects": subjects,
        "entered_by": sorted(names[a] for a in authors if a in names),
        "updated_at": max(stamps) if any(stamps) else None,
    }


def _author_names(rows: list[dict]) -> dict[int, str]:
    ids = {r.get("updated_by") or r.get("teacher_id") for r in rows} - {None}
    users = firestore_users.get_documents(list(ids)) if ids else {}
    return {int(k): v.get("full_name") or v.get("email") or "" for k, v in users.items()}


def register(class_id: int) -> dict:
    """The class's students, its subjects, and every paper sheet kept for it."""
    class_room = require_document(firestore_classes, class_id, "Class")

    loaded = run_parallel({
        "enrollments": lambda: firestore_student_enrollments.query_documents("class_id", "==", class_id),
        "mappings": lambda: firestore_teacher_mappings.query_documents("class_id", "==", class_id),
        "rows": lambda: paper_rows(class_id),
    })
    rows = loaded.get("rows") or []

    student_ids = sorted({
        e["student_id"] for e in (loaded.get("enrollments") or []) if e.get("student_id") is not None
    })
    users = firestore_users.get_documents(student_ids) if student_ids else {}
    students = [
        {
            "id": int(sid),
            "full_name": user.get("full_name") or user.get("email") or f"Student {sid}",
            "roll_number": user.get("roll_number"),
            "admission_number": user.get("admission_number"),
        }
        for sid, user in users.items()
    ]
    students.sort(key=lambda s: (
        not s["roll_number"],
        int(s["roll_number"]) if str(s["roll_number"] or "").isdigit() else 0,
        str(s["roll_number"] or ""),
        s["full_name"].casefold(),
    ))

    # The subjects taught in the class, plus any a sheet already has: a subject whose teacher
    # was moved since must not make its marks vanish from the register.
    subject_ids = {m.get("subject_id") for m in (loaded.get("mappings") or [])}
    subject_ids |= {r.get("subject_id") for r in rows}
    subject_ids |= {s.get("subject_id") for r in rows for s in (r.get("sheet_subjects") or []) if isinstance(s, dict)}
    subject_ids.discard(None)
    subjects = [
        {"id": int(k), "name": v.get("name") or "", "code": v.get("code") or "",
         "description": v.get("description")}
        for k, v in firestore_subjects.get_documents(list(subject_ids)).items()
    ] if subject_ids else []
    subjects.sort(key=lambda s: s["name"].casefold())

    names = _author_names(rows)
    sheets = [_sheet(group, names) for group in _group(rows).values()]
    sheets.sort(key=lambda s: (s["conducted_on"] or date.min, s["exam_name"] or ""), reverse=True)

    # Every other subject in the school, for an exam in one nobody is mapped to teach here yet.
    known = {s["id"] for s in subjects}
    other_subjects = [
        {"id": int(s["id"]), "name": s.get("name") or "", "code": s.get("code") or "",
         "description": s.get("description")}
        for s in firestore_subjects.list_all() if int(s["id"]) not in known
    ]
    other_subjects.sort(key=lambda s: s["name"].casefold())

    return {
        "class_id": class_id,
        "class_room": {**class_room, "id": class_id},
        "students": students,
        "subjects": subjects,
        "other_subjects": other_subjects,
        "sheets": sheets,
    }


def save_sheet(payload, actor) -> dict:
    """
    Writes one sheet: a mark per student per subject, only where something changed.

    The subjects sent are the whole exam. A subject the sheet had that is not sent was
    unticked, and its marks go with it. Every row left on the sheet follows a new name, date or
    set of subjects, so a sheet is never split in two by an edit.
    """
    class_id = payload.class_id
    require_document(firestore_classes, class_id, "Class")
    name = _clean_name(payload.exam_name)
    previous = _clean_name(payload.previous_exam_name) if payload.previous_exam_name else None

    loaded = run_parallel({
        "enrollments": lambda: firestore_student_enrollments.query_documents("class_id", "==", class_id),
        "rows": lambda: paper_rows(class_id),
    })
    enrolled = {e.get("student_id") for e in (loaded.get("enrollments") or [])}
    sheets = _group(loaded.get("rows") or [])

    if _key(name) != _key(previous) and _key(name) in sheets:
        raise HTTPException(
            status_code=409,
            detail=f"This class already has marks for “{name}”. Open that exam to change them, or use a different name.",
        )

    subject_ids = [s.subject_id for s in payload.subjects]
    if len(set(subject_ids)) != len(subject_ids):
        raise HTTPException(status_code=400, detail="Each subject can appear only once.")
    known = firestore_subjects.get_documents(subject_ids)
    if len(known) != len(set(subject_ids)):
        raise HTTPException(status_code=404, detail="One of these subjects no longer exists.")

    strangers = {m.student_id for s in payload.subjects for m in s.marks} - enrolled
    if strangers:
        raise HTTPException(
            status_code=400,
            detail="Some of these students are no longer in this class. Reload the page and try again.",
        )

    existing = sheets.get(_key(previous or name), [])
    by_cell: dict[tuple, list[dict]] = {}
    for row in existing:
        by_cell.setdefault((row.get("subject_id"), row.get("student_id")), []).append(row)

    stamp = now_utc().isoformat()
    conducted_on = payload.conducted_on.isoformat()
    layout = [{"subject_id": s.subject_id, "max_marks": float(s.max_marks)} for s in payload.subjects]
    writes: dict[str, Any] = {}
    deletes: list[str] = []
    saved = cleared = unchanged = 0
    handled: set[tuple] = set()

    for subject in payload.subjects:
        max_marks = float(subject.max_marks)
        for entry in subject.marks:
            cell = (subject.subject_id, entry.student_id)
            if cell in handled:
                continue
            handled.add(cell)
            found = by_cell.get(cell, [])

            if entry.marks_obtained is None:
                deletes.extend(str(r["id"]) for r in found)
                cleared += 1 if found else 0
                continue

            keep = _latest(found) if found else None
            # A second row for the same cell (two saves racing) would be counted twice on a card.
            deletes.extend(str(r["id"]) for r in found if r is not keep)
            marks = float(entry.marks_obtained)
            if keep and (
                float(keep.get("marks_obtained") or 0) == marks and keep.get("marks_obtained") is not None
                and float(keep.get("max_marks") or 0) == max_marks
                and keep.get("exam_name") == name
                and keep.get("conducted_on") == conducted_on
            ):
                unchanged += 1
                if keep.get("sheet_subjects") != layout:
                    writes[str(keep["id"])] = {"sheet_subjects": layout}
                continue

            row_id = str(keep["id"]) if keep else str(generate_id())
            data = {
                "marks_obtained": marks,
                "max_marks": max_marks,
                "exam_name": name,
                "conducted_on": conducted_on,
                "sheet_subjects": layout,
                "updated_at": stamp,
                "updated_by": actor.id,
            }
            if not keep:
                data.update({
                    "student_id": entry.student_id,
                    "class_id": class_id,
                    "subject_id": subject.subject_id,
                    "teacher_id": actor.id,
                    "grade": None,
                    "remarks": None,
                    "created_at": stamp,
                })
            writes[row_id] = data
            saved += 1

    # The rest of the sheet: an unticked subject's marks go; anything else (a student who has
    # since left the class) follows the new name, date and subjects.
    kept_subjects = set(subject_ids)
    for cell, found in by_cell.items():
        if cell in handled:
            continue
        if cell[0] not in kept_subjects:
            deletes.extend(str(r["id"]) for r in found)
            cleared += 1
            continue
        for row in found:
            if (row.get("exam_name"), row.get("conducted_on"), row.get("sheet_subjects")) != (name, conducted_on, layout):
                writes[str(row["id"])] = {
                    "exam_name": name, "conducted_on": conducted_on, "sheet_subjects": layout, "updated_at": stamp,
                }

    def _write(row_id: str, data: dict):
        firestore_grades.add_document(row_id, data)
        return True

    def _delete(row_id: str):
        firestore_grades.delete_document(row_id)
        return True

    tasks = {f"w:{row_id}": (lambda r=row_id, d=data: _write(r, d)) for row_id, data in writes.items()}
    tasks.update({f"d:{row_id}": (lambda r=row_id: _delete(r)) for row_id in set(deletes)})
    results = run_parallel(tasks)
    failed = [key for key, ok in results.items() if not ok]
    if failed:
        logger.error("Paper marks for class %s ('%s'): %d of %d writes failed.", class_id, name, len(failed), len(tasks))
        raise HTTPException(
            status_code=503,
            detail="Some marks could not be saved. Please press Save again; the marks already saved are kept.",
        )

    logger.info(
        "Paper marks for class %s ('%s') by %s: %d saved, %d cleared, %d unchanged.",
        class_id, name, actor.id, saved, cleared, unchanged,
    )
    rows = sheets_for_name(class_id, name)
    return {
        "sheet": _sheet(rows, _author_names(rows)) if rows else {
            "exam_name": name, "conducted_on": payload.conducted_on, "subjects": [], "entered_by": [], "updated_at": None,
        },
        "saved": saved,
        "cleared": cleared,
        "unchanged": unchanged,
    }


def sheets_for_name(class_id: int, exam_name: str) -> list[dict]:
    return _group(paper_rows(class_id)).get(_key(exam_name), [])


def delete_sheet(class_id: int, exam_name: str) -> int:
    """Removes every mark on one sheet. The report cards already issued keep their snapshot."""
    rows = sheets_for_name(class_id, exam_name)
    if not rows:
        raise HTTPException(status_code=404, detail=f"There are no marks for “{exam_name}” in this class.")
    for row in rows:
        firestore_grades.delete_document(str(row["id"]))
    logger.info("Deleted the paper marks sheet '%s' of class %s (%d marks).", exam_name, class_id, len(rows))
    return len(rows)


def exams_for_cards(class_id: int) -> tuple[list[dict], dict[int, dict[int, dict]]]:
    """
    The class's paper sheets in the shape report cards read: one exam per subject per sheet,
    and one valued script per student who has a mark.

    The ids are negative so they can never meet a real exam's. There is no release step for
    paper marks - the report card is how they reach the student - so the exams come back with
    their results published.
    """
    exams: list[dict] = []
    scripts: dict[int, dict[int, dict]] = {}

    for rows in _group(paper_rows(class_id)).values():
        by_subject: dict[int, list[dict]] = {}
        for row in rows:
            by_subject.setdefault(row.get("subject_id"), []).append(row)

        for subject_id, subject_rows in by_subject.items():
            if subject_id is None:
                continue
            max_marks = float(_most_common([r.get("max_marks") for r in subject_rows]) or 0.0)
            written = _most_common([_row_date(r) for r in subject_rows])
            exam_id = -min(int(r["id"]) for r in subject_rows)
            exams.append({
                "id": exam_id,
                "title": _latest(subject_rows).get("exam_name"),
                "mode": "PAPER",
                "subject_id": subject_id,
                "class_id": class_id,
                # Noon in school time, so the day survives any timezone the date range is in.
                "starts_at": f"{written.isoformat()}T12:00:00" if written else None,
                "ends_at": f"{written.isoformat()}T12:00:00" if written else None,
                "grading_scheme": GradingScheme.MARKS.value,
                "max_marks": max_marks,
                "grade_bands": [],
                "results_published": True,
            })

            by_student: dict[int, list[dict]] = {}
            for row in subject_rows:
                by_student.setdefault(row.get("student_id"), []).append(row)
            scripts[exam_id] = {}
            for student_id, found in by_student.items():
                row = _latest(found)
                if student_id is None or row.get("marks_obtained") is None:
                    continue
                marks = float(row["marks_obtained"])
                scripts[exam_id][student_id] = {
                    "status": SubmissionStatus.EVALUATED.value,
                    "marks_obtained": marks,
                    "percentage": round(marks / max_marks * 100.0, 2) if max_marks > 0 else None,
                    "grade": None,
                    "passed": None,
                    "evaluator_remarks": row.get("remarks"),
                }

    return exams, scripts
