"""
Exam setting, sitting, and valuation.

Shared by the teacher/admin router and the student router so that an admin acting for a
teacher and a teacher acting for themselves get byte-identical behaviour, in the same spirit
as `app.services.content`.

Three things in here are worth reading before changing anything:

* **Time is resolved per student, not per exam.** A concession granted to one student moves
  their deadline and nobody else's, and the deadline is fixed when they start so that a
  concession edited mid-paper can never shorten a paper already under way. `deadlines_for`
  is the only place that arithmetic happens.
* **The answer key never leaves this module by accident.** `hydrate_exam_for_student` is the
  only student-facing serializer and it strips the key until results are published. Routers
  must not build a student's copy of an exam by hand.
* **Objective marking is all-or-nothing.** A key can settle whether an option matches; it
  cannot judge a half-right essay, and inventing partial credit would put marks on scripts no
  human has read. Everything auto-marked is flagged `auto`, so a teacher can see exactly what
  they are being asked to trust and override any of it.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException, UploadFile

from app.core.enums import (
    CHOICE_QUESTION_VALUES, ExamMode, ExamStatus, ExamWindowState, GradingScheme,
    OBJECTIVE_QUESTION_VALUES, QuestionType, SubmissionStatus, UserRole,
)
from app.core.firebase import (
    firestore_classes, firestore_exam_submissions, firestore_exams, firestore_grades,
    firestore_student_enrollments, firestore_subjects, firestore_users, generate_id,
    prefetch_references, require_document, _resolve_document,
)
from app.core.gcp_services import StorageError, storage_service
from app.services.content import assert_class_and_subject_exist, resolve_teacher
from app.services.timetable import school_timezone

logger = logging.getLogger("exam_service")


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------
#
# Exam timing decides whether a student's work counts, so it is held to a stricter standard
# than the rest of the codebase's timestamps: everything is stored as an ISO string carrying
# a UTC offset, and every comparison is between aware datetimes. A naive value arriving from
# a client is read in the school's timezone, because that is what a teacher typing "09:00"
# into a form means.

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime | str | None) -> datetime | None:
    """Normalizes any accepted datetime to an aware UTC datetime."""
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"'{value}' is not a valid ISO-8601 datetime."
            )

    if value.tzinfo is None:
        value = value.replace(tzinfo=school_timezone())
    return value.astimezone(timezone.utc)


def store_dt(value: datetime | str | None) -> str | None:
    """Serializes a datetime for Firestore, always with an explicit UTC offset."""
    normalized = to_utc(value)
    return normalized.isoformat() if normalized else None


def _minutes_between(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds() / 60.0, 2)


def concession_for(exam: dict, student_id: int | None) -> int:
    """Extra minutes granted to one student, or zero."""
    if student_id is None:
        return 0
    concessions = exam.get("time_concessions") or {}
    try:
        return int(concessions.get(str(student_id), 0) or 0)
    except (TypeError, ValueError):
        return 0


def deadlines_for(exam: dict, student_id: int | None = None) -> dict[str, Any]:
    """
    Resolves an exam's time rules for one student.

    Returns `opens_at`, `ends_at` (their personal deadline once a concession is applied),
    `closes_at` (the last moment an upload is still accepted, deadline plus the grace), and
    the `extra_minutes` they were granted. Every timing decision in this module reads these
    four values and does no arithmetic of its own.
    """
    extra = concession_for(exam, student_id)
    opens_at = to_utc(exam.get("starts_at"))
    ends_at = to_utc(exam.get("ends_at"))

    if ends_at is not None and extra:
        ends_at = ends_at + timedelta(minutes=extra)

    grace = int(exam.get("upload_grace_minutes") or 0)
    closes_at = ends_at + timedelta(minutes=grace) if ends_at else None

    return {
        "opens_at": opens_at,
        "ends_at": ends_at,
        "closes_at": closes_at,
        "extra_minutes": extra,
        "grace_minutes": grace,
    }


def window_state(exam: dict, student_id: int | None = None, at: datetime | None = None) -> ExamWindowState:
    """Where the clock sits for this student. Derived, never stored - see `ExamWindowState`."""
    times = deadlines_for(exam, student_id)
    moment = at or now_utc()

    opens_at, ends_at, closes_at = times["opens_at"], times["ends_at"], times["closes_at"]
    if opens_at is None or ends_at is None:
        return ExamWindowState.CLOSED

    if moment < opens_at:
        return ExamWindowState.NOT_OPEN
    if moment <= ends_at:
        return ExamWindowState.OPEN
    if closes_at is not None and moment <= closes_at:
        return ExamWindowState.GRACE
    return ExamWindowState.CLOSED


# --------------------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------------------

def sorted_bands(bands: Iterable[dict] | None) -> list[dict]:
    """Grade bands, highest floor first, which is the order `grade_for` relies on."""
    return sorted(
        [b for b in (bands or []) if b.get("min_percentage") is not None],
        key=lambda b: float(b["min_percentage"]),
        reverse=True,
    )


def grade_for(percentage: float | None, bands: Iterable[dict] | None) -> str | None:
    """
    The highest band the percentage reaches, or None when no scale was defined.

    Returns None rather than the bottom band for a percentage below every floor: a scale that
    starts at 35 is saying "below this is ungraded", not "below this is the lowest grade".
    """
    if percentage is None:
        return None

    for band in sorted_bands(bands):
        if percentage >= float(band["min_percentage"]):
            return band.get("grade")
    return None


def _normalize_text(value: Any) -> str:
    """Casefolds and collapses whitespace so 'Photo  Synthesis' matches 'photosynthesis '."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _choice_set(value: Any) -> set[str]:
    """Any shape a client might send a choice answer in, as a comparable set of option keys."""
    if value is None:
        return set()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return {str(v).strip().upper() for v in values if str(v).strip()}


def _matches_key(question: dict, answer: Any) -> bool | None:
    """
    Whether an answer matches the key.

    `None` means "cannot be decided here" - no key is set, or the type is one no key can
    settle - and the caller must leave the question for a human.
    """
    key = question.get("correct_answer")
    if key is None or (isinstance(key, (list, str)) and len(key) == 0):
        return None

    kind = question.get("question_type")
    if kind not in OBJECTIVE_QUESTION_VALUES:
        return None

    if kind in CHOICE_QUESTION_VALUES:
        return bool(_choice_set(answer)) and _choice_set(answer) == _choice_set(key)

    if kind == QuestionType.NUMERIC.value:
        try:
            given = float(str(answer).strip())
            expected = float(key)
        except (TypeError, ValueError):
            return False
        tolerance = float(question.get("tolerance") or 0.0)
        return abs(given - expected) <= tolerance

    # SHORT_ANSWER. A list of keys is a list of accepted wordings, not a multi-part answer.
    accepted = key if isinstance(key, (list, tuple)) else [key]
    normalized = _normalize_text(answer)
    return bool(normalized) and normalized in {_normalize_text(a) for a in accepted}


def auto_grade(exam: dict, submission: dict) -> tuple[list[dict], float]:
    """
    Marks every objective answer against the key.

    Returns the per-question sheet and the total it comes to. Questions the key cannot settle
    are omitted entirely rather than scored zero, so an unmarked essay is visibly unmarked
    instead of looking like a script that scored nothing.
    """
    answers = {a.get("question_id"): a.get("answer") for a in (submission.get("answers") or [])}
    scores: list[dict] = []
    total = 0.0

    for question in exam.get("questions") or []:
        verdict = _matches_key(question, answers.get(question.get("id")))
        if verdict is None:
            continue

        marks = float(question.get("marks") or 0.0) if verdict else 0.0
        total += marks
        scores.append({
            "question_id": question.get("id"),
            "marks_awarded": marks,
            "max_marks": float(question.get("marks") or 0.0),
            "auto": True,
            "remarks": None,
        })

    return scores, round(total, 2)


def _merge_scores(auto_scores: list[dict], manual_scores: list[dict]) -> list[dict]:
    """
    Combines the key's marks with the teacher's, the teacher winning every collision.

    A teacher overriding an auto-marked question is the whole point of showing them the key's
    working; the override must survive, and it is recorded with `auto` false so the mark sheet
    says who decided each line.
    """
    merged = {s["question_id"]: dict(s) for s in auto_scores}
    for score in manual_scores:
        merged[score["question_id"]] = {**score, "auto": False}
    return list(merged.values())


def summarize_marks(exam: dict, marks_obtained: float | None) -> dict[str, Any]:
    """Percentage, grade, and pass/fail for a total, against the exam's own rules."""
    max_marks = float(exam.get("max_marks") or 0.0)
    if marks_obtained is None or max_marks <= 0:
        return {"percentage": None, "grade": None, "passed": None}

    percentage = round(marks_obtained / max_marks * 100.0, 2)
    pass_marks = exam.get("pass_marks")

    return {
        "percentage": percentage,
        "grade": grade_for(percentage, exam.get("grade_bands")),
        "passed": (marks_obtained >= float(pass_marks)) if pass_marks is not None else None,
    }


# --------------------------------------------------------------------------------------
# Building an exam
# --------------------------------------------------------------------------------------

def build_questions(questions_in: list, existing: list[dict] | None = None) -> list[dict]:
    """
    Turns submitted questions into stored ones, numbering them in the order given.

    A question that arrives carrying an `id` already on the exam keeps that id, so answers
    students have already filed against it stay attached. Anything without a recognised id is
    a new question and gets a fresh one - which means removing a question and re-adding it
    detaches its answers, and that is the honest outcome rather than silently re-binding
    answers to a question whose text has changed.
    """
    known = {q.get("id") for q in (existing or [])}
    built: list[dict] = []
    used: set[int] = set()

    for order, item in enumerate(questions_in, start=1):
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)

        question_id = payload.get("id")
        if question_id is None or question_id not in known or question_id in used:
            question_id = generate_id()
        used.add(question_id)

        kind = payload["question_type"]
        built.append({
            "id": question_id,
            "order": payload.get("order") or order,
            "question_type": kind.value if hasattr(kind, "value") else str(kind),
            "text": payload["text"],
            "marks": float(payload.get("marks") or 1.0),
            "options": [dict(o) for o in (payload.get("options") or [])],
            "correct_answer": payload.get("correct_answer"),
            "tolerance": payload.get("tolerance"),
            "answer_explanation": payload.get("answer_explanation"),
            "required": bool(payload.get("required", True)),
            "allow_attachments": bool(payload.get("allow_attachments", False)),
        })

    built.sort(key=lambda q: q["order"])
    return built


def questions_total(questions: list[dict] | None) -> float:
    return round(sum(float(q.get("marks") or 0.0) for q in (questions or [])), 2)


def answer_key_complete(questions: list[dict] | None) -> bool:
    """
    Whether every question a key *can* settle has one.

    Subjective questions are excluded: an exam of nothing but essays is not missing its key,
    it simply does not have one to miss.
    """
    objective = [q for q in (questions or []) if q.get("question_type") in OBJECTIVE_QUESTION_VALUES]
    if not objective:
        return False
    return all(q.get("correct_answer") not in (None, "", []) for q in objective)


def create_exam(payload, actor) -> dict:
    """
    Records a new exam.

    `max_marks` falls back to what the form actually adds up to, so a teacher who writes ten
    five-mark questions is not also required to type 50 and cannot mistype it.
    """
    assert_class_and_subject_exist(payload.class_id, payload.subject_id)

    teacher_id = payload.teacher_id or actor.id
    if payload.teacher_id and payload.teacher_id != actor.id:
        # Filing under somebody else is an admin action; a teacher may only set their own.
        if getattr(actor, "role", None) != UserRole.ADMIN:
            raise HTTPException(
                status_code=403,
                detail="Only an administrator can set an exam on another teacher's behalf.",
            )
        resolve_teacher(payload.teacher_id)

    questions = build_questions(payload.questions)
    max_marks = payload.max_marks or questions_total(questions) or 100.0

    if payload.pass_marks is not None and payload.pass_marks > max_marks:
        raise HTTPException(
            status_code=400,
            detail=f"pass_marks ({payload.pass_marks}) cannot exceed max_marks ({max_marks}).",
        )

    exam_id = generate_id()
    document = {
        "class_id": payload.class_id,
        "subject_id": payload.subject_id,
        "teacher_id": teacher_id,
        "created_by": actor.id,
        "title": payload.title,
        "description": payload.description,
        "instructions": payload.instructions,
        "mode": payload.mode.value,
        "status": payload.status.value,

        "grading_scheme": payload.grading_scheme.value,
        "max_marks": float(max_marks),
        "pass_marks": payload.pass_marks,
        "grade_bands": [b.model_dump() for b in sorted(
            payload.grade_bands, key=lambda b: b.min_percentage, reverse=True
        )],

        "starts_at": store_dt(payload.starts_at),
        "ends_at": store_dt(payload.ends_at),
        "duration_minutes": payload.duration_minutes,
        "upload_grace_minutes": payload.upload_grace_minutes,
        "late_submission_allowed": payload.late_submission_allowed,
        "time_concessions": {},

        "questions": questions,
        "shuffle_questions": payload.shuffle_questions,
        "auto_grade_objective": payload.auto_grade_objective,

        "question_paper_url": None,
        "question_paper_provider": None,
        "question_paper_warning": None,
        "max_upload_files": payload.max_upload_files,

        "results_published": False,
        "results_published_at": None,
        "created_at": now_utc().isoformat(),
        "updated_at": None,
    }

    firestore_exams.add_document(str(exam_id), document)
    document["id"] = exam_id
    return document


def apply_exam_update(exam: dict, payload) -> dict:
    """
    Applies a partial update, refusing the changes that would invalidate work already done.

    The rules that decide what a script is worth are frozen once scripts exist. Re-scaling an
    exam from 50 to 100 marks after half the class has been valued would silently halve their
    percentages, and nothing in the record would say why.
    """
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    submitted = [
        s for s in submissions_for_exam(exam["id"])
        if s.get("status") in (SubmissionStatus.SUBMITTED.value, SubmissionStatus.EVALUATED.value)
    ]
    if submitted:
        frozen = {"grading_scheme", "max_marks", "pass_marks"} & set(updates)
        if frozen:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot change {sorted(frozen)} once {len(submitted)} script(s) have been "
                    "handed in - it would change what work already valued is worth. Correct the "
                    "marks on the submissions instead, or delete the exam and set it again."
                ),
            )

    if "grade_bands" in updates:
        updates["grade_bands"] = sorted(
            [b if isinstance(b, dict) else b.model_dump() for b in updates["grade_bands"]],
            key=lambda b: b["min_percentage"],
            reverse=True,
        )

    for key in ("status", "grading_scheme"):
        if key in updates and hasattr(updates[key], "value"):
            updates[key] = updates[key].value

    for key in ("starts_at", "ends_at"):
        if key in updates:
            updates[key] = store_dt(updates[key])

    if "teacher_id" in updates:
        resolve_teacher(updates["teacher_id"])

    starts_at = to_utc(updates.get("starts_at", exam.get("starts_at")))
    ends_at = to_utc(updates.get("ends_at", exam.get("ends_at")))
    if starts_at and ends_at and ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="`ends_at` must be after `starts_at`.")

    duration = updates.get("duration_minutes", exam.get("duration_minutes"))
    if duration and starts_at and ends_at:
        window = (ends_at - starts_at).total_seconds() / 60
        if duration > window:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"duration_minutes ({duration}) exceeds the {window:.0f}-minute window "
                    "between starts_at and ends_at."
                ),
            )

    scheme = updates.get("grading_scheme", exam.get("grading_scheme"))
    bands = updates.get("grade_bands", exam.get("grade_bands"))
    if scheme == GradingScheme.GRADE.value and not bands:
        raise HTTPException(
            status_code=400,
            detail="A GRADE exam needs grade_bands, otherwise there is no scale to award from.",
        )

    max_marks = float(updates.get("max_marks", exam.get("max_marks") or 0.0))
    pass_marks = updates.get("pass_marks", exam.get("pass_marks"))
    if pass_marks is not None and max_marks and float(pass_marks) > max_marks:
        raise HTTPException(
            status_code=400,
            detail=f"pass_marks ({pass_marks}) cannot exceed max_marks ({max_marks}).",
        )

    if updates.get("status") == ExamStatus.PUBLISHED.value:
        assert_publishable(exam)

    updates["updated_at"] = now_utc().isoformat()
    firestore_exams.add_document(str(exam["id"]), updates)
    return firestore_exams.get_document(str(exam["id"]))


def assert_publishable(exam: dict) -> None:
    """An exam students can be sent to must actually have a paper behind it."""
    if exam.get("mode") == ExamMode.ONLINE.value and not exam.get("questions"):
        raise HTTPException(
            status_code=400,
            detail="An ONLINE exam needs at least one question before it can be released to students.",
        )
    if exam.get("mode") == ExamMode.OFFLINE.value and not (
        exam.get("questions") or exam.get("question_paper_url")
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "An OFFLINE exam needs either a question form or an uploaded question paper "
                "before it can be released - otherwise students have nothing to answer."
            ),
        )


def set_answer_key(exam: dict, payload) -> dict:
    """
    Attaches or corrects the answer key, optionally re-marking what is already in.

    Setting a key after the scripts are in is a first-class flow, not a repair: a teacher who
    values by hand may only decide the accepted wording once they have read what the class
    actually wrote. `regrade` is what makes that useful - without it a late key would change
    nothing about the marks.
    """
    questions = {q["id"]: q for q in (exam.get("questions") or [])}
    unknown = [item.question_id for item in payload.answers if item.question_id not in questions]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"No question(s) {unknown} on this exam. Question ids come from the exam's `questions`.",
        )

    for item in payload.answers:
        question = questions[item.question_id]
        kind = question.get("question_type")

        if item.correct_answer is not None and kind in CHOICE_QUESTION_VALUES:
            valid = {str(o.get("key")).upper() for o in (question.get("options") or [])}
            given = _choice_set(item.correct_answer)
            if not given <= valid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Answer key {sorted(given - valid)} for question {item.question_id} "
                        f"matches no option ({sorted(valid)})."
                    ),
                )
            if kind != QuestionType.MULTI_SELECT.value and len(given) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {item.question_id} is a {kind} and has exactly one correct option.",
                )

        if item.correct_answer is not None and kind == QuestionType.NUMERIC.value:
            try:
                float(item.correct_answer)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"The key for numeric question {item.question_id} must be a number.",
                )

        question["correct_answer"] = item.correct_answer
        if item.tolerance is not None:
            question["tolerance"] = item.tolerance
        if item.answer_explanation is not None:
            question["answer_explanation"] = item.answer_explanation

    updated_questions = sorted(questions.values(), key=lambda q: q.get("order") or 0)
    firestore_exams.add_document(str(exam["id"]), {
        "questions": updated_questions,
        "updated_at": now_utc().isoformat(),
    })
    exam = firestore_exams.get_document(str(exam["id"]))

    if payload.regrade:
        regrade_submissions(exam)
    return exam


def regrade_submissions(exam: dict) -> int:
    """
    Re-marks every handed-in script against the current key.

    Marks a teacher awarded by hand are preserved - `_merge_scores` lets the human win - so a
    corrected key fixes the objective section without discarding a morning of valuation. A
    script whose total changes is re-summarized; one already published keeps its published
    `exam_grades` row in step via `publish_results`, which the caller runs again.
    """
    changed = 0
    for submission in submissions_for_exam(exam["id"]):
        if submission.get("status") not in (
            SubmissionStatus.SUBMITTED.value, SubmissionStatus.EVALUATED.value
        ):
            continue

        auto_scores, auto_total = auto_grade(exam, submission)
        manual = [s for s in (submission.get("question_scores") or []) if not s.get("auto")]
        merged = _merge_scores(auto_scores, manual)

        total = round(sum(float(s.get("marks_awarded") or 0.0) for s in merged), 2)
        summary = summarize_marks(exam, total)

        firestore_exam_submissions.add_document(str(submission["id"]), {
            "question_scores": merged,
            "auto_graded_marks": auto_total,
            "marks_obtained": total,
            "percentage": summary["percentage"],
            "grade": summary["grade"] if exam.get("grading_scheme") == GradingScheme.MARKS.value
                     else submission.get("grade"),
            "passed": summary["passed"],
            "updated_at": now_utc().isoformat(),
        })
        changed += 1

    if changed:
        logger.info("Re-marked %d script(s) for exam %s against the updated key.", changed, exam["id"])
    return changed


def grant_concession(exam: dict, payload) -> dict:
    """
    Grants or withdraws one student's extra time.

    Stored on the exam rather than on the submission because a concession is usually arranged
    before the exam, when there is no submission yet. A student already sitting keeps the
    deadline fixed at their start - see `deadlines_for` - so this affects their next attempt
    window, not a paper in progress.
    """
    assert_enrolled(exam, payload.student_id)

    concessions = dict(exam.get("time_concessions") or {})
    if payload.extra_minutes:
        concessions[str(payload.student_id)] = payload.extra_minutes
    else:
        concessions.pop(str(payload.student_id), None)

    firestore_exams.add_document(str(exam["id"]), {
        "time_concessions": concessions,
        "updated_at": now_utc().isoformat(),
    })
    logger.info(
        "Exam %s: %s minutes concession for student %s (%s).",
        exam["id"], payload.extra_minutes, payload.student_id, payload.reason or "no reason given",
    )
    return firestore_exams.get_document(str(exam["id"]))


async def store_question_paper(exam: dict, file: UploadFile) -> dict:
    """Uploads the offline question paper and attaches it to the exam."""
    try:
        stored = await storage_service.save_file_detailed(
            file=file, folder=f"class_{exam['class_id']}/exams/{exam['id']}"
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    firestore_exams.add_document(str(exam["id"]), {
        "question_paper_url": stored["url"],
        "question_paper_provider": stored["provider"],
        "question_paper_warning": stored.get("warning"),
        "updated_at": now_utc().isoformat(),
    })
    return firestore_exams.get_document(str(exam["id"]))


# --------------------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------------------

def enrolled_student_ids(class_id: int) -> set[int]:
    return {
        e["student_id"]
        for e in firestore_student_enrollments.query_documents("class_id", "==", class_id)
        if e.get("student_id") is not None
    }


def assert_enrolled(exam: dict, student_id: int) -> None:
    if student_id not in enrolled_student_ids(exam["class_id"]):
        raise HTTPException(
            status_code=403,
            detail=f"Student {student_id} is not enrolled in the class this exam is set for.",
        )


def assert_student_may_sit(exam: dict, student_id: int) -> None:
    """
    The gate every student-facing write passes through.

    A draft or cancelled exam is not merely hidden from listings - it must also refuse a
    student who guessed its id, or the id is the only thing standing between a class and
    tomorrow's paper.
    """
    if exam.get("status") != ExamStatus.PUBLISHED.value:
        raise HTTPException(status_code=404, detail="Exam not found")
    assert_enrolled(exam, student_id)


# --------------------------------------------------------------------------------------
# Submissions
# --------------------------------------------------------------------------------------

def submission_id(exam_id: int | str, student_id: int | str) -> str:
    """
    The document id for one student's script.

    Derived from the pair rather than generated, which makes "one script per student per
    exam" a property of the database instead of something every write has to check. Two tabs
    submitting at once update the same document; neither creates a second script.
    """
    return f"{exam_id}_{student_id}"


def get_submission(exam_id: int, student_id: int) -> dict | None:
    return firestore_exam_submissions.get_document(submission_id(exam_id, student_id))


def submissions_for_exam(exam_id: int) -> list[dict]:
    return firestore_exam_submissions.query_documents("exam_id", "==", exam_id)


def _personal_deadline(exam: dict, started_at: datetime, student_id: int) -> datetime:
    """
    When this student's time runs out: the earlier of their duration expiring and the exam's
    own deadline. A concession lengthens both.
    """
    times = deadlines_for(exam, student_id)
    hard_deadline = times["ends_at"]

    duration = exam.get("duration_minutes")
    if not duration:
        return hard_deadline

    personal = started_at + timedelta(minutes=int(duration) + times["extra_minutes"])
    return min(personal, hard_deadline) if hard_deadline else personal


def ensure_submission(exam: dict, student_id: int) -> dict:
    """
    Fetches this student's script, starting one if they have not begun.

    Starting is implicit on the first save or upload as well as on the explicit `/start`, so a
    client that goes straight to answering is not punished for skipping a call - and either
    way `expires_at` is stamped once, at the real moment they began.
    """
    existing = get_submission(exam["id"], student_id)
    if existing:
        return existing

    state = window_state(exam, student_id)
    if state is ExamWindowState.NOT_OPEN:
        opens = deadlines_for(exam, student_id)["opens_at"]
        raise HTTPException(
            status_code=403,
            detail=f"This exam opens at {opens.isoformat()}. You cannot start it yet.",
        )
    if state is ExamWindowState.CLOSED:
        raise HTTPException(status_code=403, detail="This exam has closed.")
    if state is ExamWindowState.GRACE and not exam.get("late_submission_allowed", True):
        raise HTTPException(
            status_code=403,
            detail="The exam deadline has passed and this exam does not accept late work.",
        )

    started_at = now_utc()
    document = {
        "exam_id": exam["id"],
        "student_id": student_id,
        "class_id": exam["class_id"],
        "subject_id": exam["subject_id"],
        "status": SubmissionStatus.IN_PROGRESS.value,
        "started_at": started_at.isoformat(),
        "submitted_at": None,
        "expires_at": _to_iso(_personal_deadline(exam, started_at, student_id)),
        "is_late": False,
        "late_by_minutes": 0.0,
        "answers": [],
        "attachments": [],
        "question_scores": [],
        "marks_obtained": None,
        "percentage": None,
        "grade": None,
        "passed": None,
        "auto_graded_marks": None,
        "evaluator_remarks": None,
        "evaluated_by": None,
        "evaluated_at": None,
        "grade_record_id": None,
        "created_at": started_at.isoformat(),
        "updated_at": None,
    }

    doc_id = submission_id(exam["id"], student_id)
    firestore_exam_submissions.add_document(doc_id, document)
    document["id"] = doc_id
    return document


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def assert_open_for_writing(exam: dict, submission: dict) -> None:
    """
    Refuses a write to a script whose time is up, or to one already handed in.

    A handed-in script is closed even inside the window: re-opening it would let a student
    change an answer after seeing a classmate's, and there is no audit trail that would show it.
    """
    if submission.get("status") in (SubmissionStatus.SUBMITTED.value, SubmissionStatus.EVALUATED.value):
        raise HTTPException(
            status_code=409,
            detail="You have already submitted this exam. Ask your teacher if you need it reopened.",
        )

    expires_at = to_utc(submission.get("expires_at"))
    grace = timedelta(minutes=int(exam.get("upload_grace_minutes") or 0))
    moment = now_utc()

    if expires_at and moment > expires_at + grace:
        raise HTTPException(status_code=403, detail="Your time for this exam has run out.")

    if expires_at and moment > expires_at and not exam.get("late_submission_allowed", True):
        raise HTTPException(
            status_code=403,
            detail="Your time for this exam has run out and late work is not accepted.",
        )


def merge_answers(submission: dict, answers_in: list) -> list[dict]:
    """
    Merges saved answers by question id, so a client can autosave one question at a time.

    A question sent with a null answer and no attachments is cleared rather than skipped - a
    student deselecting an option must be able to leave it blank.
    """
    merged = {a.get("question_id"): dict(a) for a in (submission.get("answers") or [])}

    for item in answers_in:
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        merged[payload["question_id"]] = {
            "question_id": payload["question_id"],
            "answer": payload.get("answer"),
            "attachments": list(payload.get("attachments") or []),
        }

    return list(merged.values())


def save_answers(exam: dict, submission: dict, answers_in: list) -> dict:
    """Saves progress without handing in."""
    assert_open_for_writing(exam, submission)
    _assert_known_questions(exam, answers_in)

    firestore_exam_submissions.add_document(str(submission["id"]), {
        "answers": merge_answers(submission, answers_in),
        "updated_at": now_utc().isoformat(),
    })
    return firestore_exam_submissions.get_document(str(submission["id"]))


def _assert_known_questions(exam: dict, answers_in: list) -> None:
    if exam.get("mode") != ExamMode.ONLINE.value and not exam.get("questions"):
        raise HTTPException(
            status_code=400,
            detail="This exam has no question form. Upload your answer sheet instead.",
        )

    known = {q["id"] for q in (exam.get("questions") or [])}
    unknown = [
        (a.question_id if hasattr(a, "question_id") else a["question_id"])
        for a in answers_in
    ]
    unknown = [q for q in unknown if q not in known]
    if unknown:
        raise HTTPException(
            status_code=404, detail=f"No question(s) {unknown} on this exam."
        )


def submit(exam: dict, submission: dict, answers_in: list | None = None) -> dict:
    """
    Hands the paper in, marking the objective section straight away when a key exists.

    Lateness is measured against this student's own `expires_at`, so a concession shows up as
    a later deadline rather than as a late flag somebody has to explain afterwards.
    """
    assert_open_for_writing(exam, submission)

    if answers_in:
        _assert_known_questions(exam, answers_in)
        submission = {**submission, "answers": merge_answers(submission, answers_in)}

    if exam.get("mode") == ExamMode.OFFLINE.value and not (
        submission.get("attachments") or submission.get("answers")
    ):
        raise HTTPException(
            status_code=400,
            detail="Upload your answer sheet before submitting this exam.",
        )

    moment = now_utc()
    expires_at = to_utc(submission.get("expires_at"))
    is_late = bool(expires_at and moment > expires_at)

    updates: dict[str, Any] = {
        "answers": submission.get("answers") or [],
        "status": SubmissionStatus.SUBMITTED.value,
        "submitted_at": moment.isoformat(),
        "is_late": is_late,
        "late_by_minutes": _minutes_between(moment, expires_at) if is_late else 0.0,
        "updated_at": moment.isoformat(),
    }

    if exam.get("auto_grade_objective", True) and exam.get("questions"):
        auto_scores, auto_total = auto_grade(exam, submission)
        if auto_scores:
            updates["question_scores"] = auto_scores
            updates["auto_graded_marks"] = auto_total
            # Only a fully objective paper can be totalled without a human. A mixed paper
            # leaves the total unset, so an essay awaiting valuation is never reported as a
            # finished mark that happens to be low.
            if len(auto_scores) == len(exam["questions"]):
                summary = summarize_marks(exam, auto_total)
                updates["marks_obtained"] = auto_total
                updates["percentage"] = summary["percentage"]
                updates["passed"] = summary["passed"]
                if exam.get("grading_scheme") == GradingScheme.MARKS.value:
                    updates["grade"] = summary["grade"]

    firestore_exam_submissions.add_document(str(submission["id"]), updates)
    return firestore_exam_submissions.get_document(str(submission["id"]))


async def store_answer_sheet(
    exam: dict,
    submission: dict,
    file: UploadFile,
    question_id: int | None = None,
) -> dict:
    """
    Attaches an answer sheet (or a file answering one question) to a script.

    This is the offline path's equivalent of typing an answer, so it is gated by exactly the
    same clock: the uploading concession is what lets a scan that started before the deadline
    finish landing after it.
    """
    assert_open_for_writing(exam, submission)

    attachments = list(submission.get("attachments") or [])
    limit = int(exam.get("max_upload_files") or 5)
    if len(attachments) >= limit:
        raise HTTPException(
            status_code=400,
            detail=f"This exam accepts at most {limit} file(s) and you have already uploaded {len(attachments)}.",
        )

    if question_id is not None and question_id not in {q["id"] for q in (exam.get("questions") or [])}:
        raise HTTPException(status_code=404, detail=f"No question {question_id} on this exam.")

    try:
        stored = await storage_service.save_file_detailed(
            file=file,
            folder=f"class_{exam['class_id']}/exams/{exam['id']}/submissions/{submission['student_id']}",
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    attachments.append({
        "file_url": stored["url"],
        "filename": file.filename,
        "provider": stored["provider"],
        "storage_warning": stored.get("warning"),
        "uploaded_at": now_utc().isoformat(),
        "question_id": question_id,
    })

    updates: dict[str, Any] = {"attachments": attachments, "updated_at": now_utc().isoformat()}

    if question_id is not None:
        answers = {a["question_id"]: dict(a) for a in (submission.get("answers") or [])}
        entry = answers.setdefault(question_id, {"question_id": question_id, "answer": None, "attachments": []})
        entry["attachments"] = list(entry.get("attachments") or []) + [stored["url"]]
        updates["answers"] = list(answers.values())

    firestore_exam_submissions.add_document(str(submission["id"]), updates)
    return firestore_exam_submissions.get_document(str(submission["id"]))


def reopen(exam: dict, submission: dict) -> dict:
    """
    Puts a handed-in script back in the student's hands.

    Deliberately a teacher's decision and never a student's: it is the answer to a genuine
    accident (a paper submitted blank, a browser that submitted on refresh) and would be a
    hole in the exam if the student could do it themselves. The clock is not reset - the
    student gets whatever is left of their original time, so reopening cannot be used to buy
    more of it.

    Any valuation is discarded, and a mark already published for it is withdrawn. A mark
    describes a particular script, and the teacher has just handed that script back to be
    changed; leaving the old number in place would put a mark on the grades screen and the
    report card that no submitted work supports. The script must be valued again once it is
    handed back in.
    """
    if submission.get("status") == SubmissionStatus.IN_PROGRESS.value:
        raise HTTPException(status_code=409, detail="That script has not been submitted.")

    published_row = submission.get("grade_record_id")
    if published_row:
        firestore_grades.delete_document(str(published_row))

    firestore_exam_submissions.add_document(str(submission["id"]), {
        "status": SubmissionStatus.IN_PROGRESS.value,
        "submitted_at": None,
        "question_scores": [],
        "marks_obtained": None,
        "percentage": None,
        "grade": None,
        "passed": None,
        "auto_graded_marks": None,
        "evaluator_remarks": None,
        "evaluated_by": None,
        "evaluated_at": None,
        "grade_record_id": None,
        "updated_at": now_utc().isoformat(),
    })
    logger.info(
        "Reopened script %s on exam %s; its valuation%s was withdrawn.",
        submission["id"], exam["id"], " and published mark" if published_row else "",
    )
    return firestore_exam_submissions.get_document(str(submission["id"]))


# --------------------------------------------------------------------------------------
# Valuation
# --------------------------------------------------------------------------------------

def evaluate(exam: dict, submission: dict, payload, evaluator) -> dict:
    """
    Records a valued script.

    Under MARKS a per-question sheet is totalled for you; a flat `marks_obtained` is taken as
    given, which is the natural shape for a paper marked with a red pen. Under GRADE the
    letter is what counts and must come from the exam's own scale, so a card cannot end up
    carrying a grade the school does not use.
    """
    if submission.get("status") == SubmissionStatus.IN_PROGRESS.value:
        raise HTTPException(
            status_code=409,
            detail="That script has not been handed in yet, so there is nothing to value.",
        )

    scheme = exam.get("grading_scheme")
    known = {q["id"] for q in (exam.get("questions") or [])}
    max_by_question = {q["id"]: float(q.get("marks") or 0.0) for q in (exam.get("questions") or [])}

    unknown = [s.question_id for s in payload.question_scores if s.question_id not in known]
    if unknown:
        raise HTTPException(status_code=404, detail=f"No question(s) {unknown} on this exam.")

    over = [
        f"question {s.question_id}: {s.marks_awarded} > {max_by_question[s.question_id]}"
        for s in payload.question_scores
        if s.marks_awarded > max_by_question[s.question_id]
    ]
    if over:
        raise HTTPException(
            status_code=400,
            detail=f"Marks awarded exceed what the question is worth - {'; '.join(over)}.",
        )

    manual = [
        {
            "question_id": s.question_id,
            "marks_awarded": float(s.marks_awarded),
            "max_marks": max_by_question[s.question_id],
            "auto": False,
            "remarks": s.remarks,
        }
        for s in payload.question_scores
    ]
    existing_auto = [s for s in (submission.get("question_scores") or []) if s.get("auto")]
    scores = _merge_scores(existing_auto, manual)

    marks: float | None = None
    if payload.marks_obtained is not None:
        marks = float(payload.marks_obtained)
    elif scores:
        marks = round(sum(float(s.get("marks_awarded") or 0.0) for s in scores), 2)

    max_marks = float(exam.get("max_marks") or 0.0)
    if marks is not None and max_marks and marks > max_marks:
        raise HTTPException(
            status_code=400,
            detail=f"marks_obtained ({marks}) exceeds this exam's max_marks ({max_marks}).",
        )

    summary = summarize_marks(exam, marks)
    grade = payload.grade

    if scheme == GradingScheme.GRADE.value:
        if not grade:
            raise HTTPException(
                status_code=400,
                detail="This exam is valued by grade, so `grade` is required.",
            )
        allowed = {b.get("grade") for b in (exam.get("grade_bands") or [])}
        if grade not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"'{grade}' is not one of this exam's grades ({sorted(allowed)}).",
            )
    elif grade is None:
        # Derived from the bands where the exam defines them; None when it does not.
        grade = summary["grade"]

    moment = now_utc()
    updates = {
        "question_scores": scores,
        "marks_obtained": marks,
        "percentage": summary["percentage"],
        "grade": grade,
        "passed": summary["passed"],
        "evaluator_remarks": payload.remarks,
        "evaluated_by": evaluator.id,
        "evaluated_at": moment.isoformat(),
        "status": SubmissionStatus.EVALUATED.value,
        "updated_at": moment.isoformat(),
    }
    firestore_exam_submissions.add_document(str(submission["id"]), updates)

    updated = firestore_exam_submissions.get_document(str(submission["id"]))
    if exam.get("results_published"):
        # The result is already out; a correction has to reach the student's grade row too,
        # or the exam screen and the grades screen disagree about the same mark.
        _write_grade_row(exam, updated)
    return updated


def _write_grade_row(exam: dict, submission: dict) -> int | None:
    """
    Mirrors a published result into the `exam_grades` collection.

    That collection predates this module and is what `/students/grades` and the admin
    analytics read. Writing to it keeps one place where a student looks up a mark, instead of
    a second results screen that the reports know nothing about. The row id is remembered on
    the submission so a correction updates the same row rather than adding another.
    """
    row_id = submission.get("grade_record_id") or generate_id()
    firestore_grades.add_document(str(row_id), {
        "student_id": submission["student_id"],
        "class_id": exam["class_id"],
        "subject_id": exam["subject_id"],
        "teacher_id": submission.get("evaluated_by") or exam["teacher_id"],
        "exam_id": exam["id"],
        "exam_name": exam.get("title"),
        "marks_obtained": submission.get("marks_obtained"),
        "max_marks": float(exam.get("max_marks") or 0.0),
        "grade": submission.get("grade"),
        "remarks": submission.get("evaluator_remarks"),
        "created_at": now_utc().isoformat(),
    })

    if not submission.get("grade_record_id"):
        firestore_exam_submissions.add_document(str(submission["id"]), {"grade_record_id": row_id})
    return row_id


def publish_results(exam: dict) -> dict:
    """
    Releases the marks to the class.

    Only valued scripts are released. An unmarked script left behind is reported rather than
    published as a zero, because "nobody has marked this yet" and "this scored nothing" are
    different facts and a student can tell the difference.
    """
    submissions = submissions_for_exam(exam["id"])
    evaluated = [s for s in submissions if s.get("status") == SubmissionStatus.EVALUATED.value]
    pending = [s for s in submissions if s.get("status") == SubmissionStatus.SUBMITTED.value]

    rows = 0
    for submission in evaluated:
        if submission.get("marks_obtained") is not None or submission.get("grade"):
            _write_grade_row(exam, submission)
            rows += 1

    moment = now_utc()
    firestore_exams.add_document(str(exam["id"]), {
        "results_published": True,
        "results_published_at": moment.isoformat(),
        "updated_at": moment.isoformat(),
    })

    message = f"Released {len(evaluated)} result(s) to the class."
    if pending:
        message += (
            f" {len(pending)} script(s) are still unmarked and were not released - "
            "value them and publish again."
        )

    return {
        "exam_id": exam["id"],
        "published": len(evaluated),
        "skipped_unevaluated": len(pending),
        "grade_rows_written": rows,
        "message": message,
    }


def exam_stats(exam: dict) -> dict:
    """A teacher's at-a-glance view of where an exam has got to."""
    submissions = submissions_for_exam(exam["id"])
    enrolled = enrolled_student_ids(exam["class_id"])

    submitted = [s for s in submissions if s.get("status") in (
        SubmissionStatus.SUBMITTED.value, SubmissionStatus.EVALUATED.value
    )]
    evaluated = [s for s in submissions if s.get("status") == SubmissionStatus.EVALUATED.value]
    percentages = [s["percentage"] for s in evaluated if s.get("percentage") is not None]
    passes = [s.get("passed") for s in evaluated if s.get("passed") is not None]

    return {
        "exam_id": exam["id"],
        "title": exam.get("title"),
        "class_id": exam["class_id"],
        "enrolled_students": len(enrolled),
        "started": len(submissions),
        "submitted": len(submitted),
        "evaluated": len(evaluated),
        "missing": max(len(enrolled) - len(submitted), 0),
        "late": sum(1 for s in submitted if s.get("is_late")),
        "average_percentage": round(sum(percentages) / len(percentages), 2) if percentages else None,
        "highest_percentage": max(percentages) if percentages else None,
        "lowest_percentage": min(percentages) if percentages else None,
        "pass_count": sum(1 for p in passes if p) if passes else None,
        "fail_count": sum(1 for p in passes if not p) if passes else None,
        "results_published": bool(exam.get("results_published")),
    }


# --------------------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------------------

def prefetch_exams(exams: list[dict]) -> None:
    prefetch_references(
        exams,
        ("teacher_id", firestore_users),
        ("class_id", firestore_classes),
        ("subject_id", firestore_subjects),
    )


def hydrate_exam(exam: dict) -> dict:
    """The staff view. Carries the answer key - never hand the result to a student."""
    questions = sorted(exam.get("questions") or [], key=lambda q: q.get("order") or 0)

    return {
        "id": exam["id"],
        "class_id": exam.get("class_id"),
        "class_room": _resolve_document(firestore_classes, exam.get("class_id")),
        "subject_id": exam.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, exam.get("subject_id")),
        "teacher_id": exam.get("teacher_id"),
        "teacher": _resolve_document(firestore_users, exam.get("teacher_id")),
        "created_by": exam.get("created_by"),

        "title": exam.get("title"),
        "description": exam.get("description"),
        "instructions": exam.get("instructions"),
        "mode": exam.get("mode"),
        "status": exam.get("status"),
        "window_state": window_state(exam).value,

        "grading_scheme": exam.get("grading_scheme"),
        "max_marks": exam.get("max_marks"),
        "pass_marks": exam.get("pass_marks"),
        "grade_bands": sorted_bands(exam.get("grade_bands")),

        "starts_at": exam.get("starts_at"),
        "ends_at": exam.get("ends_at"),
        "duration_minutes": exam.get("duration_minutes"),
        "upload_grace_minutes": exam.get("upload_grace_minutes") or 0,
        "late_submission_allowed": exam.get("late_submission_allowed", True),
        "time_concessions": exam.get("time_concessions") or {},

        "questions": questions,
        "question_count": len(questions),
        "questions_total_marks": questions_total(questions),
        "answer_key_complete": answer_key_complete(questions),
        "shuffle_questions": exam.get("shuffle_questions", False),
        "auto_grade_objective": exam.get("auto_grade_objective", True),

        "question_paper_url": exam.get("question_paper_url"),
        "question_paper_provider": exam.get("question_paper_provider"),
        "question_paper_warning": exam.get("question_paper_warning"),
        "max_upload_files": exam.get("max_upload_files") or 5,

        "results_published": bool(exam.get("results_published")),
        "results_published_at": exam.get("results_published_at"),
        "created_at": exam.get("created_at"),
        "updated_at": exam.get("updated_at"),
    }


def hydrate_exam_for_student(
    exam: dict,
    student_id: int,
    submission: dict | None = None,
    include_questions: bool = True,
) -> dict:
    """
    The only student-facing serializer for an exam.

    Two rules it exists to enforce, in one place rather than in every route:

    * the answer key and its explanations are stripped until the exam's results are published;
    * the timing is resolved for this student, so a granted concession reaches them as a later
      `closes_at` rather than as a rule the client has to reimplement.
    """
    times = deadlines_for(exam, student_id)
    state = window_state(exam, student_id)
    published = bool(exam.get("results_published"))

    questions = []
    if include_questions:
        for question in sorted(exam.get("questions") or [], key=lambda q: q.get("order") or 0):
            visible = {
                "id": question.get("id"),
                "order": question.get("order"),
                "question_type": question.get("question_type"),
                "text": question.get("text"),
                "marks": question.get("marks"),
                "options": question.get("options") or [],
                "required": question.get("required", True),
                "allow_attachments": question.get("allow_attachments", False),
                # Returned only with a published result, where showing the right answer is
                # the point. Before that, these two keys stay null.
                "correct_answer": question.get("correct_answer") if published else None,
                "answer_explanation": question.get("answer_explanation") if published else None,
            }
            questions.append(visible)

    status = submission.get("status") if submission else None
    if status is None and state is ExamWindowState.CLOSED:
        status = SubmissionStatus.MISSED.value

    handed_in = status in (SubmissionStatus.SUBMITTED.value, SubmissionStatus.EVALUATED.value)
    open_now = state in (ExamWindowState.OPEN, ExamWindowState.GRACE)
    late_ok = exam.get("late_submission_allowed", True)
    sittable = (
        exam.get("status") == ExamStatus.PUBLISHED.value
        and open_now
        and (state is ExamWindowState.OPEN or late_ok)
    )

    return {
        "id": exam["id"],
        "class_id": exam.get("class_id"),
        "class_room": _resolve_document(firestore_classes, exam.get("class_id")),
        "subject_id": exam.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, exam.get("subject_id")),
        "teacher": _resolve_document(firestore_users, exam.get("teacher_id")),

        "title": exam.get("title"),
        "description": exam.get("description"),
        "instructions": exam.get("instructions"),
        "mode": exam.get("mode"),
        "status": exam.get("status"),
        "window_state": state.value,

        "grading_scheme": exam.get("grading_scheme"),
        "max_marks": exam.get("max_marks"),
        "pass_marks": exam.get("pass_marks"),

        "starts_at": exam.get("starts_at"),
        "ends_at": _to_iso(times["ends_at"]),
        "closes_at": _to_iso(times["closes_at"]),
        "duration_minutes": exam.get("duration_minutes"),
        "extra_time_minutes": times["extra_minutes"],
        "upload_grace_minutes": times["grace_minutes"],
        "late_submission_allowed": late_ok,

        "question_paper_url": exam.get("question_paper_url"),
        "max_upload_files": exam.get("max_upload_files") or 5,
        "questions": questions,
        "question_count": len(exam.get("questions") or []),

        "results_published": published,
        "submission_status": status,
        "submitted_at": submission.get("submitted_at") if submission else None,
        "expires_at": submission.get("expires_at") if submission else None,
        "can_start": sittable and not handed_in,
        "can_submit": sittable and not handed_in and submission is not None,
    }


def hydrate_submission(submission: dict, exam: dict | None = None) -> dict:
    """The staff view of a script."""
    max_by_question = {
        q["id"]: float(q.get("marks") or 0.0) for q in ((exam or {}).get("questions") or [])
    }
    scores = [
        {**s, "max_marks": s.get("max_marks", max_by_question.get(s.get("question_id")))}
        for s in (submission.get("question_scores") or [])
    ]

    return {
        "id": str(submission["id"]),
        "exam_id": submission.get("exam_id"),
        "student_id": submission.get("student_id"),
        "student": _resolve_document(firestore_users, submission.get("student_id")),
        "class_id": submission.get("class_id"),
        "subject_id": submission.get("subject_id"),

        "status": submission.get("status"),
        "started_at": submission.get("started_at"),
        "submitted_at": submission.get("submitted_at"),
        "expires_at": submission.get("expires_at"),
        "is_late": bool(submission.get("is_late")),
        "late_by_minutes": submission.get("late_by_minutes") or 0.0,

        "answers": submission.get("answers") or [],
        "attachments": submission.get("attachments") or [],

        "question_scores": scores,
        "marks_obtained": submission.get("marks_obtained"),
        "percentage": submission.get("percentage"),
        "grade": submission.get("grade"),
        "passed": submission.get("passed"),
        "auto_graded_marks": submission.get("auto_graded_marks"),
        "evaluator_remarks": submission.get("evaluator_remarks"),
        "evaluated_by": submission.get("evaluated_by"),
        "evaluator": _resolve_document(firestore_users, submission.get("evaluated_by")),
        "evaluated_at": submission.get("evaluated_at"),

        "created_at": submission.get("created_at"),
        "updated_at": submission.get("updated_at"),
    }


def hydrate_submission_for_student(submission: dict, exam: dict) -> dict:
    """
    A student's own script.

    The valuation is withheld until the exam's results are published, so a marked-but-unreleased
    paper is indistinguishable from an unmarked one. Without this a student could read their
    mark off the API the moment a teacher saved it, which defeats the point of publishing.
    """
    published = bool(exam.get("results_published"))
    max_by_question = {q["id"]: float(q.get("marks") or 0.0) for q in (exam.get("questions") or [])}

    return {
        "id": str(submission["id"]),
        "exam_id": submission.get("exam_id"),
        "status": submission.get("status"),
        "started_at": submission.get("started_at"),
        "submitted_at": submission.get("submitted_at"),
        "expires_at": submission.get("expires_at"),
        "is_late": bool(submission.get("is_late")),

        "answers": submission.get("answers") or [],
        "attachments": submission.get("attachments") or [],

        "results_published": published,
        "max_marks": exam.get("max_marks") if published else None,
        "marks_obtained": submission.get("marks_obtained") if published else None,
        "percentage": submission.get("percentage") if published else None,
        "grade": submission.get("grade") if published else None,
        "passed": submission.get("passed") if published else None,
        "evaluator_remarks": submission.get("evaluator_remarks") if published else None,
        "question_scores": [
            {**s, "max_marks": s.get("max_marks", max_by_question.get(s.get("question_id")))}
            for s in (submission.get("question_scores") or [])
        ] if published else [],
    }


def require_exam(exam_id: int | str) -> dict:
    return require_document(firestore_exams, exam_id, "Exam")
