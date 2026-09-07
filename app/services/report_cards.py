"""
Report cards: a student's exams in one class, consolidated onto one document.

Issued by the class teacher of that class, or by an admin. Two decisions shape everything
here:

* **A card is a snapshot, not a query.** It records the marks as they stood when it was
  issued. A card already handed to a parent must not quietly change because somebody
  corrected a mark last night; re-issuing is a deliberate act that overwrites the same card.
* **A missing paper is not a zero.** An exam a student never sat is listed as missed and left
  out of the totals unless the issuer explicitly asks for it to count as zero. Averaging an
  absence into a term percentage without being told to is how a report card ends up lying
  about a child who was in hospital.

The whole class is built in one pass rather than one student at a time, because ranking needs
every student's total anyway and the collections are read once either way.
"""

import logging
import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.core.concurrency import run_parallel
from app.core.enums import AttendanceStatus, ExamStatus, GradingScheme, SubmissionStatus
from app.core.firebase import (
    firestore_attendance, firestore_classes, firestore_exam_submissions, firestore_exams,
    firestore_report_cards, firestore_student_enrollments, firestore_subjects,
    firestore_users, prefetch_references, _resolve_document,
)
from app.services.exams import grade_for, now_utc, sorted_bands, to_utc

logger = logging.getLogger("report_card_service")


def report_card_id(class_id: int, student_id: int, title: str) -> str:
    """
    The document id for one card.

    Derived from the class, the student, and the card's title so that re-issuing "Term 1
    Report Card" replaces the previous version instead of leaving two cards with different
    numbers on them and no way to tell which one the parent was given.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).strip().casefold()).strip("-") or "report"
    return f"{class_id}_{student_id}_{slug[:60]}"


def _in_range(exam: dict, from_date: datetime | None, to_date: datetime | None) -> bool:
    """Whether an exam falls in the card's period, judged by when it finished."""
    conducted = to_utc(exam.get("ends_at")) or to_utc(exam.get("starts_at"))
    if conducted is None:
        return False
    if from_date and conducted < to_utc(from_date):
        return False
    if to_date and conducted > to_utc(to_date):
        return False
    return True


def _common_bands(exams: list[dict]) -> list[dict]:
    """
    The grade scale to use when the issuer did not supply one.

    Falls back to the exams' own bands only when every exam that defines a scale defines the
    *same* scale. Where they disagree there is no honest way to grade a total that mixes them,
    so the card carries percentages and no letter rather than a letter picked from one
    subject's rules and applied to another's.
    """
    seen: set[tuple] = set()
    for exam in exams:
        bands = sorted_bands(exam.get("grade_bands"))
        if bands:
            seen.add(tuple((b.get("grade"), float(b.get("min_percentage"))) for b in bands))

    if len(seen) != 1:
        return []

    return [{"grade": g, "min_percentage": p} for g, p in next(iter(seen))]


def _exam_line(exam: dict, submission: dict | None, count_missing_as_zero: bool) -> dict:
    """One exam's row for one student."""
    max_marks = float(exam.get("max_marks") or 0.0)
    evaluated = submission and submission.get("status") == SubmissionStatus.EVALUATED.value
    has_result = bool(evaluated and (
        submission.get("marks_obtained") is not None or submission.get("grade")
    ))

    line = {
        "exam_id": exam["id"],
        "title": exam.get("title"),
        "mode": exam.get("mode"),
        "conducted_on": exam.get("ends_at"),
        "grading_scheme": exam.get("grading_scheme"),
        "max_marks": max_marks,
        "marks_obtained": None,
        "percentage": None,
        "grade": None,
        "passed": None,
        "missed": not has_result,
        "remarks": None,
    }

    if has_result:
        line.update({
            "marks_obtained": submission.get("marks_obtained"),
            "percentage": submission.get("percentage"),
            "grade": submission.get("grade"),
            "passed": submission.get("passed"),
            "remarks": submission.get("evaluator_remarks"),
        })
    elif count_missing_as_zero:
        line.update({"marks_obtained": 0.0, "percentage": 0.0, "passed": False})

    return line


def _counts_toward_total(line: dict, exam: dict, count_missing_as_zero: bool) -> bool:
    """
    Whether a row contributes to the arithmetic.

    A GRADE-scheme exam never does: letters cannot be added up, and converting one back to a
    number would invent a mark nobody awarded. Those exams still appear on the card - a
    grade is a result worth reporting - they just do not move the totals.
    """
    if exam.get("grading_scheme") == GradingScheme.GRADE.value:
        return False
    if line["missed"] and not count_missing_as_zero:
        return False
    return line["marks_obtained"] is not None and float(exam.get("max_marks") or 0.0) > 0


def _build_card(
    student: dict,
    exams: list[dict],
    submissions_by_exam: dict[int, dict[int, dict]],
    subjects: dict[int, dict],
    payload,
    bands: list[dict],
    attendance_percentage: float | None,
) -> dict:
    """Assembles one student's card from the already-loaded collections."""
    by_subject: dict[Any, dict] = {}
    total_marks = 0.0
    total_max = 0.0
    counted = 0
    missed = 0

    for exam in exams:
        submission = submissions_by_exam.get(exam["id"], {}).get(student["id"])
        line = _exam_line(exam, submission, payload.count_missing_as_zero)

        subject_id = exam.get("subject_id")
        subject = subjects.get(subject_id) or {}
        block = by_subject.setdefault(subject_id, {
            "subject_id": subject_id,
            "subject_name": subject.get("name"),
            "subject_code": subject.get("code"),
            "exams": [],
            "total_marks": 0.0,
            "total_max_marks": 0.0,
            "percentage": None,
            "grade": None,
            "exams_counted": 0,
            "exams_missed": 0,
            "teacher_remarks": None,
        })
        block["exams"].append(line)

        if line["missed"]:
            block["exams_missed"] += 1
            missed += 1

        if _counts_toward_total(line, exam, payload.count_missing_as_zero):
            marks = float(line["marks_obtained"])
            max_marks = float(exam.get("max_marks") or 0.0)
            block["total_marks"] += marks
            block["total_max_marks"] += max_marks
            block["exams_counted"] += 1
            total_marks += marks
            total_max += max_marks
            counted += 1

    for block in by_subject.values():
        block["total_marks"] = round(block["total_marks"], 2)
        block["total_max_marks"] = round(block["total_max_marks"], 2)
        if block["total_max_marks"] > 0:
            block["percentage"] = round(block["total_marks"] / block["total_max_marks"] * 100.0, 2)
            block["grade"] = grade_for(block["percentage"], bands)

    overall = round(total_marks / total_max * 100.0, 2) if total_max > 0 else 0.0

    return {
        "student_id": student["id"],
        "class_id": payload.class_id,
        "title": payload.title,
        "generated_at": now_utc().isoformat(),
        "from_date": payload.from_date.isoformat() if payload.from_date else None,
        "to_date": payload.to_date.isoformat() if payload.to_date else None,
        "subjects": sorted(by_subject.values(), key=lambda b: (b["subject_name"] or "")),
        "total_marks": round(total_marks, 2),
        "total_max_marks": round(total_max, 2),
        "overall_percentage": overall,
        "overall_grade": grade_for(overall, bands) if total_max > 0 else None,
        "exams_counted": counted,
        "exams_missed": missed,
        "attendance_percentage": attendance_percentage,
        "rank": None,
        "class_size": None,
        "remarks": payload.remarks,
    }


def _attendance_percentages(class_id: int) -> dict[int, float]:
    """Each student's attendance rate in this class, as a percentage of records marked."""
    records = firestore_attendance.query_documents("class_id", "==", class_id)
    tally: dict[int, list[int]] = {}
    for record in records:
        student_id = record.get("student_id")
        if student_id is None:
            continue
        present = 1 if record.get("status") == AttendanceStatus.PRESENT.value else 0
        seen = tally.setdefault(student_id, [0, 0])
        seen[0] += present
        seen[1] += 1

    return {
        student_id: round(present / total * 100.0, 2)
        for student_id, (present, total) in tally.items() if total
    }


def _assign_ranks(cards: list[dict]) -> None:
    """
    Positions by overall percentage, ties sharing a place.

    Standard competition ranking: two students on 91% are both 2nd and the next is 4th.
    Students with nothing counted are left unranked rather than placed last, because "sat no
    exams" is not the same as "came bottom".
    """
    rankable = [c for c in cards if c["exams_counted"] > 0]
    rankable.sort(key=lambda c: c["overall_percentage"], reverse=True)

    previous: float | None = None
    position = 0
    for index, card in enumerate(rankable, start=1):
        if previous is None or card["overall_percentage"] < previous:
            position = index
            previous = card["overall_percentage"]
        card["rank"] = position
        card["class_size"] = len(rankable)


def generate(payload, actor) -> dict:
    """
    Issues report cards for a class.

    Every collection is read once and the whole class is assembled from memory: a term card
    for thirty students across six subjects would otherwise be hundreds of round trips, and
    the ranking needs all of them computed anyway.
    """
    class_room = firestore_classes.get_document(str(payload.class_id))
    if not class_room:
        raise HTTPException(status_code=404, detail=f"ClassRoom {payload.class_id} not found")

    loaded = run_parallel({
        "enrollments": lambda: firestore_student_enrollments.query_documents(
            "class_id", "==", payload.class_id
        ),
        "exams": lambda: firestore_exams.query_documents("class_id", "==", payload.class_id),
        "attendance": (
            (lambda: _attendance_percentages(payload.class_id))
            if payload.include_attendance else (lambda: {})
        ),
    })

    enrollments = loaded.get("enrollments") or []
    all_exams = loaded.get("exams") or []
    attendance = loaded.get("attendance") or {}

    student_ids = [e["student_id"] for e in enrollments if e.get("student_id") is not None]
    if payload.student_ids:
        wanted = set(payload.student_ids)
        missing = wanted - set(student_ids)
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Student(s) {sorted(missing)} are not enrolled in class {payload.class_id}.",
            )
        student_ids = [s for s in student_ids if s in wanted]

    if not student_ids:
        raise HTTPException(
            status_code=400,
            detail=f"No students are enrolled in class {payload.class_id}, so there is nothing to report on.",
        )

    warnings: list[str] = []

    exams = [e for e in all_exams if e.get("status") != ExamStatus.CANCELLED.value]
    if payload.exam_ids:
        wanted_exams = set(payload.exam_ids)
        unknown = wanted_exams - {e["id"] for e in exams}
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=f"Exam(s) {sorted(unknown)} are not exams of class {payload.class_id}.",
            )
        exams = [e for e in exams if e["id"] in wanted_exams]

    if payload.published_results_only:
        withheld = [e for e in exams if not e.get("results_published")]
        exams = [e for e in exams if e.get("results_published")]
        if withheld:
            warnings.append(
                f"{len(withheld)} exam(s) were left off because their results are not published yet: "
                + ", ".join(str(e.get("title")) for e in withheld[:5])
                + ("..." if len(withheld) > 5 else "")
            )

    exams = [e for e in exams if _in_range(e, payload.from_date, payload.to_date)]
    if not exams:
        raise HTTPException(
            status_code=400,
            detail=(
                "No exams match this report card's filters, so every card would be empty. "
                + (warnings[0] if warnings else
                   "Check the date range, and whether the exams' results have been published.")
            ),
        )

    # One query per exam, issued together. Submissions are keyed by exam id, not class id,
    # so there is no single query that fetches them all.
    fetched = run_parallel({
        str(exam["id"]): (lambda e=exam: firestore_exam_submissions.query_documents("exam_id", "==", e["id"]))
        for exam in exams
    })
    submissions_by_exam: dict[int, dict[int, dict]] = {
        exam["id"]: {
            s["student_id"]: s
            for s in (fetched.get(str(exam["id"])) or []) if s.get("student_id") is not None
        }
        for exam in exams
    }

    students = firestore_users.get_documents(student_ids)
    subject_ids = {e.get("subject_id") for e in exams if e.get("subject_id") is not None}
    subjects = {
        int(k): v for k, v in firestore_subjects.get_documents(list(subject_ids)).items()
    }

    bands = (
        [b.model_dump() for b in sorted(payload.grade_bands, key=lambda b: b.min_percentage, reverse=True)]
        if payload.grade_bands else _common_bands(exams)
    )
    if not bands:
        warnings.append(
            "No grade scale was supplied and the exams do not share one, so the cards carry "
            "percentages without letter grades."
        )

    cards: list[dict] = []
    for student_id in student_ids:
        student = students.get(str(student_id))
        if not student:
            warnings.append(f"Student {student_id} has no profile and was skipped.")
            continue
        cards.append(_build_card(
            student=student,
            exams=exams,
            submissions_by_exam=submissions_by_exam,
            subjects=subjects,
            payload=payload,
            bands=bands,
            attendance_percentage=attendance.get(student_id),
        ))

    if payload.include_rank:
        _assign_ranks(cards)

    published_at = now_utc().isoformat() if payload.publish else None
    saved: list[dict] = []

    for card in cards:
        card["generated_by"] = actor.id
        card["is_published"] = bool(payload.publish)
        card["published_at"] = published_at

        doc_id = report_card_id(payload.class_id, card["student_id"], payload.title)
        firestore_report_cards.add_document(doc_id, card)
        saved.append({**card, "id": doc_id})

    logger.info(
        "Issued %d report card(s) for class %s ('%s') across %d exam(s).",
        len(saved), payload.class_id, payload.title, len(exams),
    )

    return {
        "class_id": payload.class_id,
        "generated": len(saved),
        "skipped": len(student_ids) - len(saved),
        "published": bool(payload.publish),
        "cards": [hydrate_report_card(c) for c in saved],
        "warnings": warnings,
    }


def apply_update(card: dict, payload) -> dict:
    """Edits the human-owned parts of an issued card: its title, remarks, and whether it is out."""
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    subject_remarks = updates.pop("subject_remarks", None)
    if subject_remarks:
        subjects = [dict(s) for s in (card.get("subjects") or [])]
        known = {str(s.get("subject_id")) for s in subjects}
        unknown = set(subject_remarks) - known
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=f"Subject(s) {sorted(unknown)} are not on this report card.",
            )
        for subject in subjects:
            remark = subject_remarks.get(str(subject.get("subject_id")))
            if remark is not None:
                subject["teacher_remarks"] = remark
        updates["subjects"] = subjects

    if "is_published" in updates:
        updates["published_at"] = now_utc().isoformat() if updates["is_published"] else None

    firestore_report_cards.add_document(str(card["id"]), updates)
    return firestore_report_cards.get_document(str(card["id"]))


def prefetch_cards(cards: list[dict]) -> None:
    prefetch_references(
        cards,
        ("student_id", firestore_users),
        ("class_id", firestore_classes),
    )


def hydrate_report_card(card: dict) -> dict:
    return {
        "id": card["id"],
        "student_id": card.get("student_id"),
        "student": _resolve_document(firestore_users, card.get("student_id")),
        "class_id": card.get("class_id"),
        "class_room": _resolve_document(firestore_classes, card.get("class_id")),

        "title": card.get("title"),
        "generated_by": card.get("generated_by"),
        "generated_at": card.get("generated_at"),
        "from_date": card.get("from_date"),
        "to_date": card.get("to_date"),

        "subjects": card.get("subjects") or [],
        "total_marks": card.get("total_marks") or 0.0,
        "total_max_marks": card.get("total_max_marks") or 0.0,
        "overall_percentage": card.get("overall_percentage") or 0.0,
        "overall_grade": card.get("overall_grade"),
        "exams_counted": card.get("exams_counted") or 0,
        "exams_missed": card.get("exams_missed") or 0,
        "attendance_percentage": card.get("attendance_percentage"),
        "rank": card.get("rank"),
        "class_size": card.get("class_size"),

        "remarks": card.get("remarks"),
        "is_published": bool(card.get("is_published")),
        "published_at": card.get("published_at"),
    }
