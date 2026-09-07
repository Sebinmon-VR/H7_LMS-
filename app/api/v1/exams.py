"""
The exam module's HTTP surface.

Three routers, split by who is talking rather than by resource:

* `router` (`/exams`)            - setting, valuing, and releasing exams. Teachers and admins.
* `report_router` (`/report-cards`) - consolidated results. Class teachers and admins.
* `student_router` (`/students`) - sitting an exam and reading your own result.

Exams live in their own module rather than being bolted onto the teacher and student routers
because the same set of operations is performed by three different roles, and duplicating
them per role is how two implementations drift apart. Role is enforced per endpoint by the
same guards the rest of the API uses.

Authority follows the existing rule exactly: a teacher owns the exams they set, a class
teacher may also act on every exam filed against a class they lead, and an admin may act on
anything. `assert_owner` is that rule; nothing here re-implements it.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.api.v1.dependencies import require_student, require_teacher
from app.core.concurrency import run_parallel
from app.core.enums import ExamMode, ExamStatus, SubmissionStatus
from app.core.firebase import (
    assert_owner, delete_with_dependencies, firestore_exam_submissions, firestore_exams,
    firestore_grades, firestore_report_cards, firestore_users, prefetch_references,
    require_document,
)
from app.schemas.exam import (
    AnswerKeyUpdate, AnswerSaveIn, EvaluationIn, ExamCreate, ExamOut, ExamStats, ExamUpdate,
    QuestionFormUpdate, ResultsPublished, StudentExamOut, StudentSubmissionOut, SubmissionOut,
    SubmitIn, TimeConcessionGrant,
)
from app.schemas.report_card import (
    ReportCardBatch, ReportCardGenerate, ReportCardOut, ReportCardUpdate,
)
from app.schemas.user import UserOut
from app.services import exams as exam_service
from app.services import report_cards as report_card_service
from app.services import timetable as timetable_service
from app.services.permissions import is_class_teacher_of, visible_records

logger = logging.getLogger("exams_api")

router = APIRouter(prefix="/exams", tags=["Exams Module"])
report_router = APIRouter(prefix="/report-cards", tags=["Report Cards"])
student_router = APIRouter(prefix="/students", tags=["Exams Module - Students"])


def _managed_exam(exam_id: int, current_user: UserOut) -> dict:
    """Loads an exam the caller is entitled to act on, or raises 404/403 saying which."""
    exam = exam_service.require_exam(exam_id)
    assert_owner(exam, current_user, "exams")
    return exam


# ======================================================================================
# Setting an exam
# ======================================================================================

@router.post("", response_model=ExamOut, status_code=status.HTTP_201_CREATED)
def create_exam(
    exam_in: ExamCreate,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Set an exam, ONLINE or OFFLINE.

    An ONLINE exam carries its question form in `questions`; an OFFLINE one is either a form
    the students answer on paper or a question paper uploaded to `/exams/{id}/paper`.

    Created as a DRAFT by default so the form and the answer key can be finished before the
    class can see anything. Release it with `POST /exams/{id}/publish`.

    The valuation rules - `grading_scheme`, `max_marks`, `pass_marks`, `grade_bands` - are
    fixed here, and become read-only once scripts have been handed in. `max_marks` defaults to
    what the question form adds up to.

    Time rules: `starts_at`/`ends_at` bound the window, `duration_minutes` limits each student
    once they start, and `upload_grace_minutes` is the uploading concession - the minutes past
    the deadline in which a hand-in is still accepted and flagged late.
    """
    exam = exam_service.create_exam(exam_in, current_user)
    return ExamOut(**exam_service.hydrate_exam(exam))


@router.get("", response_model=List[ExamOut])
def list_exams(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    exam_status: Optional[ExamStatus] = Query(None, alias="status"),
    mode: Optional[ExamMode] = Query(None),
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Exams this user set, plus every exam filed against a class they are the
    class teacher of. Admins see all of them.
    """
    exams = visible_records(firestore_exams, current_user, class_id)

    if subject_id is not None:
        exams = [e for e in exams if e.get("subject_id") == subject_id]
    if exam_status is not None:
        exams = [e for e in exams if e.get("status") == exam_status.value]
    if mode is not None:
        exams = [e for e in exams if e.get("mode") == mode.value]

    exams.sort(key=lambda e: e.get("starts_at") or "", reverse=True)
    exam_service.prefetch_exams(exams)
    return [ExamOut(**exam_service.hydrate_exam(e)) for e in exams]


@router.get("/{exam_id}", response_model=ExamOut)
def get_exam(exam_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher / Admin] One exam in full, answer key included."""
    return ExamOut(**exam_service.hydrate_exam(_managed_exam(exam_id, current_user)))


@router.put("/{exam_id}", response_model=ExamOut)
def update_exam(
    exam_id: int,
    update_in: ExamUpdate,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Correct an exam's details or time rules.

    Returns 409 if you try to change what the exam is worth after scripts have been handed
    in - re-scaling an exam that has already been valued would silently change everyone's
    percentage. The question form is edited through `/exams/{id}/questions`, so a metadata
    update can never wipe it.
    """
    exam = _managed_exam(exam_id, current_user)
    return ExamOut(**exam_service.hydrate_exam(exam_service.apply_exam_update(exam, update_in)))


@router.delete("/{exam_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exam(
    exam_id: int,
    force: bool = Query(False, description="Also delete the submitted scripts and published marks."),
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Delete an exam.

    Refuses with 409 while students' scripts still reference it, naming the count. `force=true`
    cascades, which throws away their work and any marks already published from it.
    """
    _managed_exam(exam_id, current_user)
    delete_with_dependencies(
        firestore_exams,
        exam_id,
        "Exam",
        [
            ("student submission(s)", firestore_exam_submissions, "exam_id"),
            ("published grade row(s)", firestore_grades, "exam_id"),
        ],
        force=force,
    )
    return None


@router.put("/{exam_id}/questions", response_model=ExamOut)
def replace_question_form(
    exam_id: int,
    form_in: QuestionFormUpdate,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Build or rewrite the exam sheet in one call.

    The whole form is replaced, so question order is never ambiguous. A question sent back
    with the `id` it already has keeps that id and stays attached to any answers already filed
    against it; anything else is treated as a new question.

    Refuses once scripts have been handed in - changing the paper underneath a valued script
    is not an edit, it is a different exam.
    """
    exam = _managed_exam(exam_id, current_user)

    handed_in = [
        s for s in exam_service.submissions_for_exam(exam_id)
        if s.get("status") != SubmissionStatus.IN_PROGRESS.value
    ]
    if handed_in:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(handed_in)} script(s) have already been handed in, so the question form "
                "is fixed. Use PUT /exams/{id}/answer-key to correct the answers instead."
            ),
        )

    questions = exam_service.build_questions(form_in.questions, exam.get("questions"))
    updates = {"questions": questions, "updated_at": exam_service.now_utc().isoformat()}

    # A form that is rewritten before anyone sits it re-establishes what the exam is out of,
    # unless the setter pinned `max_marks` to something other than the questions' total.
    if float(exam.get("max_marks") or 0) == exam_service.questions_total(exam.get("questions")):
        updates["max_marks"] = exam_service.questions_total(questions) or exam.get("max_marks")

    firestore_exams.add_document(str(exam_id), updates)
    return ExamOut(**exam_service.hydrate_exam(firestore_exams.get_document(str(exam_id))))


@router.put("/{exam_id}/answer-key", response_model=ExamOut)
def set_answer_key(
    exam_id: int,
    key_in: AnswerKeyUpdate,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Set or correct the answer key - at any point, before or after the exam.

    With `regrade` (the default) every script already handed in is re-marked against the new
    key, which is the point of being allowed to set one late. Marks a teacher awarded by hand
    are kept: the key only settles the objective questions.
    """
    exam = _managed_exam(exam_id, current_user)
    updated = exam_service.set_answer_key(exam, key_in)
    return ExamOut(**exam_service.hydrate_exam(updated))


@router.post("/{exam_id}/paper", response_model=ExamOut)
async def upload_question_paper(
    exam_id: int,
    file: UploadFile = File(..., description="The question paper: PDF, DOC, or an image."),
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Upload the question paper for an OFFLINE exam.

    Stored on the configured backend exactly like a study material. Check `question_paper_provider`
    on the response: if it reads LOCAL when Drive or Cloud Storage was configured,
    `question_paper_warning` says what went wrong.
    """
    exam = _managed_exam(exam_id, current_user)
    updated = await exam_service.store_question_paper(exam, file)
    return ExamOut(**exam_service.hydrate_exam(updated))


@router.post("/{exam_id}/publish", response_model=ExamOut)
def publish_exam(exam_id: int, current_user: UserOut = Depends(require_teacher)):
    """
    [Teacher / Admin] Release the exam to the class.

    Until this is called the exam is a DRAFT: it does not appear on any student's list and a
    student who guesses its id gets a 404. Refuses to release an exam with nothing to answer.
    """
    exam = _managed_exam(exam_id, current_user)
    exam_service.assert_publishable(exam)

    firestore_exams.add_document(str(exam_id), {
        "status": ExamStatus.PUBLISHED.value,
        "updated_at": exam_service.now_utc().isoformat(),
    })
    return ExamOut(**exam_service.hydrate_exam(firestore_exams.get_document(str(exam_id))))


@router.post("/{exam_id}/concessions", response_model=ExamOut)
def grant_time_concession(
    exam_id: int,
    grant_in: TimeConcessionGrant,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Grant one student extra time on this exam.

    The minutes are added to both their deadline and their personal duration, and to nobody
    else's. Send `extra_minutes: 0` to withdraw a concession.

    Distinct from `upload_grace_minutes`, which is the whole class's uploading concession: this
    is an access arrangement for one student, and their work is not flagged late for using it.
    """
    exam = _managed_exam(exam_id, current_user)
    return ExamOut(**exam_service.hydrate_exam(exam_service.grant_concession(exam, grant_in)))


# ======================================================================================
# Submissions and valuation
# ======================================================================================

@router.get("/{exam_id}/stats", response_model=ExamStats)
def get_exam_stats(exam_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher / Admin] How the class is doing: who has sat it, what is left to value."""
    return ExamStats(**exam_service.exam_stats(_managed_exam(exam_id, current_user)))


@router.get("/{exam_id}/submissions", response_model=List[SubmissionOut])
def list_submissions(
    exam_id: int,
    submission_status: Optional[SubmissionStatus] = Query(None, alias="status"),
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Every script handed in for this exam.

    Only staff who may act on the exam can reach it - students never see one another's work.
    Filter by `status=SUBMITTED` for the valuation queue.
    """
    exam = _managed_exam(exam_id, current_user)
    submissions = exam_service.submissions_for_exam(exam_id)

    if submission_status is not None:
        submissions = [s for s in submissions if s.get("status") == submission_status.value]

    submissions.sort(key=lambda s: s.get("submitted_at") or s.get("started_at") or "")
    prefetch_references(
        submissions, ("student_id", firestore_users), ("evaluated_by", firestore_users)
    )
    return [SubmissionOut(**exam_service.hydrate_submission(s, exam)) for s in submissions]


@router.get("/{exam_id}/submissions/{student_id}", response_model=SubmissionOut)
def get_submission(
    exam_id: int,
    student_id: int,
    current_user: UserOut = Depends(require_teacher),
):
    """[Teacher / Admin] One student's script, with their answers and any uploaded sheets."""
    exam = _managed_exam(exam_id, current_user)

    submission = exam_service.get_submission(exam_id, student_id)
    if not submission:
        raise HTTPException(
            status_code=404,
            detail=f"Student {student_id} has not started this exam.",
        )
    return SubmissionOut(**exam_service.hydrate_submission(submission, exam))


@router.post("/{exam_id}/submissions/{student_id}/evaluate", response_model=SubmissionOut)
def evaluate_submission(
    exam_id: int,
    student_id: int,
    evaluation_in: EvaluationIn,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Value a script.

    Under MARKS send `question_scores` and the total is added up for you, or send a flat
    `marks_obtained` for a paper you marked by hand. Under GRADE send `grade`, which must be
    one of the exam's own bands.

    Marks entered here override anything the answer key awarded, and the mark sheet records
    which lines were decided by the key and which by you. The student sees none of it until
    the results are published.
    """
    exam = _managed_exam(exam_id, current_user)

    submission = exam_service.get_submission(exam_id, student_id)
    if not submission:
        raise HTTPException(
            status_code=404, detail=f"Student {student_id} has not sat this exam."
        )

    valued = exam_service.evaluate(exam, submission, evaluation_in, current_user)
    return SubmissionOut(**exam_service.hydrate_submission(valued, exam))


@router.post("/{exam_id}/submissions/{student_id}/reopen", response_model=SubmissionOut)
def reopen_submission(
    exam_id: int,
    student_id: int,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Teacher / Admin] Hand a submitted script back to the student.

    For the genuine accident - a paper submitted blank, a browser that submitted on refresh.
    The clock is not reset: the student gets whatever is left of their original time, so this
    cannot be used to buy more of it.

    Any valuation is discarded and a mark already published for it is withdrawn, because the
    script it described is about to change. Value the script again once it is handed back in.
    """
    exam = _managed_exam(exam_id, current_user)

    submission = exam_service.get_submission(exam_id, student_id)
    if not submission:
        raise HTTPException(
            status_code=404, detail=f"Student {student_id} has not sat this exam."
        )

    reopened = exam_service.reopen(exam, submission)
    return SubmissionOut(**exam_service.hydrate_submission(reopened, exam))


@router.post("/{exam_id}/results/publish", response_model=ResultsPublished)
def publish_results(exam_id: int, current_user: UserOut = Depends(require_teacher)):
    """
    [Teacher / Admin] Release the marks to the class.

    Until this is called a student sees their script but no mark, however long ago it was
    valued. Publishing also mirrors each result into `exam_grades`, so it shows up on
    `/students/grades` and in the admin analytics rather than living in a second results
    screen the reports know nothing about.

    Scripts still unmarked are reported back rather than released as zeros; value them and
    publish again to release the rest.
    """
    exam = _managed_exam(exam_id, current_user)
    return ResultsPublished(**exam_service.publish_results(exam))


# ======================================================================================
# Report cards
# ======================================================================================

def _assert_card_authority(class_id: int, current_user: UserOut) -> None:
    """
    Report cards are a class teacher's business.

    A subject teacher answers for their own column and may not issue or read a card that
    consolidates every other teacher's marks, so this is deliberately stricter than the
    ownership rule the exam endpoints use.
    """
    if not is_class_teacher_of(current_user, class_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Report cards for class {class_id} are issued by its class teacher. "
                "Ask an administrator to assign you, or to issue them."
            ),
        )


@report_router.post("/generate", response_model=ReportCardBatch, status_code=status.HTTP_201_CREATED)
def generate_report_cards(
    request_in: ReportCardGenerate,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Class Teacher / Admin] Issue report cards for a class from every exam in it.

    One card per enrolled student, each consolidating their exams by subject with totals, a
    percentage, a letter grade, attendance, and their position in the class. Bound the period
    with `from_date`/`to_date`, or name `exam_ids` to cover a specific set.

    Re-issuing a card with the same `title` overwrites the previous version rather than
    leaving two cards with different numbers on them.

    Cards are created unpublished so you can read them before the class does; release them
    with `publish: true` here, or per card afterwards.
    """
    _assert_card_authority(request_in.class_id, current_user)
    return ReportCardBatch(**report_card_service.generate(request_in, current_user))


@report_router.get("", response_model=List[ReportCardOut])
def list_report_cards(
    class_id: Optional[int] = Query(None),
    student_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher),
):
    """[Class Teacher / Admin] Report cards issued for the classes you lead."""
    if class_id is not None:
        _assert_card_authority(class_id, current_user)
        cards = firestore_report_cards.query_documents("class_id", "==", class_id)
    elif student_id is not None:
        cards = [
            c for c in firestore_report_cards.query_documents("student_id", "==", student_id)
            if is_class_teacher_of(current_user, c.get("class_id"))
        ]
    else:
        cards = [
            c for c in firestore_report_cards.list_all()
            if is_class_teacher_of(current_user, c.get("class_id"))
        ]

    if student_id is not None:
        cards = [c for c in cards if c.get("student_id") == student_id]

    cards.sort(key=lambda c: c.get("generated_at") or "", reverse=True)
    report_card_service.prefetch_cards(cards)
    return [ReportCardOut(**report_card_service.hydrate_report_card(c)) for c in cards]


@report_router.get("/{card_id}", response_model=ReportCardOut)
def get_report_card(card_id: str, current_user: UserOut = Depends(require_teacher)):
    """[Class Teacher / Admin] One report card in full."""
    card = require_document(firestore_report_cards, card_id, "Report card")
    _assert_card_authority(card.get("class_id"), current_user)
    return ReportCardOut(**report_card_service.hydrate_report_card(card))


@report_router.put("/{card_id}", response_model=ReportCardOut)
def update_report_card(
    card_id: str,
    update_in: ReportCardUpdate,
    current_user: UserOut = Depends(require_teacher),
):
    """
    [Class Teacher / Admin] Add remarks to a card, or release it to the student.

    The marks themselves are a snapshot and are not editable here - correct the underlying
    submission and re-issue the card, so the numbers on it always trace back to a valued script.
    """
    card = require_document(firestore_report_cards, card_id, "Report card")
    _assert_card_authority(card.get("class_id"), current_user)

    updated = report_card_service.apply_update({**card, "id": card_id}, update_in)
    return ReportCardOut(**report_card_service.hydrate_report_card(updated))


@report_router.delete("/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report_card(card_id: str, current_user: UserOut = Depends(require_teacher)):
    """[Class Teacher / Admin] Withdraw a report card issued in error."""
    card = require_document(firestore_report_cards, card_id, "Report card")
    _assert_card_authority(card.get("class_id"), current_user)

    firestore_report_cards.delete_document(card_id)
    return None


# ======================================================================================
# Sitting an exam
# ======================================================================================

def _student_exam(exam_id: int, student: UserOut) -> dict:
    """
    Loads an exam this student is entitled to sit.

    A draft or cancelled exam 404s rather than 403ing: telling a student that an exam exists
    but is not released yet is itself a leak of what is coming.
    """
    exam = exam_service.require_exam(exam_id)
    exam_service.assert_student_may_sit(exam, student.id)
    return exam


@student_router.get("/exams", response_model=List[StudentExamOut])
def list_my_exams(
    subject_id: Optional[int] = Query(None),
    upcoming_only: bool = Query(False, description="Hide exams whose window has closed."),
    current_user: UserOut = Depends(require_student),
):
    """
    [Student] Every released exam for the classes this student is enrolled in.

    Each entry carries this student's own state - whether they can start, whether they have
    handed in, and their personal `closes_at` with any granted extra time already applied -
    so a list screen needs no further call per exam. Answer keys are never included.
    """
    class_ids = timetable_service.class_ids_for_student(current_user.id)
    if not class_ids:
        return []

    fetched = run_parallel({
        str(class_id): (lambda c=class_id: firestore_exams.query_documents("class_id", "==", c))
        for class_id in class_ids
    })

    exams = [
        exam for records in fetched.values() for exam in (records or [])
        if exam.get("status") == ExamStatus.PUBLISHED.value
    ]
    if subject_id is not None:
        exams = [e for e in exams if e.get("subject_id") == subject_id]
    if upcoming_only:
        exams = [
            e for e in exams
            if exam_service.window_state(e, current_user.id).value != "CLOSED"
        ]

    exams.sort(key=lambda e: e.get("starts_at") or "")
    exam_service.prefetch_exams(exams)

    # One batched read for every script this student has open, rather than one per exam.
    submissions = firestore_exam_submissions.get_documents(
        [exam_service.submission_id(e["id"], current_user.id) for e in exams]
    )

    return [
        StudentExamOut(**exam_service.hydrate_exam_for_student(
            exam,
            current_user.id,
            submission=submissions.get(exam_service.submission_id(exam["id"], current_user.id)),
            # The paper itself is withheld from a listing: it is only released when the
            # student opens the exam, and only once its window has opened.
            include_questions=False,
        ))
        for exam in exams
    ]


@student_router.get("/exams/{exam_id}", response_model=StudentExamOut)
def get_my_exam(exam_id: int, current_user: UserOut = Depends(require_student)):
    """
    [Student] Open an exam.

    The question form is served only once the window has opened - before that this returns the
    exam's details, its time rules, and nothing to answer. The answer key stays hidden until
    the results are published, at which point the correct answers and any explanations are
    included so the paper is worth reading back.
    """
    exam = _student_exam(exam_id, current_user)
    state = exam_service.window_state(exam, current_user.id)
    submission = exam_service.get_submission(exam_id, current_user.id)

    return StudentExamOut(**exam_service.hydrate_exam_for_student(
        exam,
        current_user.id,
        submission=submission,
        include_questions=(state.value != "NOT_OPEN"),
    ))


@student_router.post("/exams/{exam_id}/start", response_model=StudentSubmissionOut)
def start_my_exam(exam_id: int, current_user: UserOut = Depends(require_student)):
    """
    [Student] Start the exam and stamp the clock.

    `expires_at` on the response is when this student's time runs out: the earlier of their
    duration expiring and the exam's deadline, with any granted extra time included. It is
    fixed at this moment and does not move afterwards.

    Calling it again returns the script already in progress rather than restarting it.
    """
    exam = _student_exam(exam_id, current_user)
    submission = exam_service.ensure_submission(exam, current_user.id)
    return StudentSubmissionOut(**exam_service.hydrate_submission_for_student(submission, exam))


@student_router.patch("/exams/{exam_id}/answers", response_model=StudentSubmissionOut)
def save_my_answers(
    exam_id: int,
    answers_in: AnswerSaveIn,
    current_user: UserOut = Depends(require_student),
):
    """
    [Student] Save progress without handing in.

    Answers are merged by question id, so a client can autosave one question at a time. Starts
    the exam implicitly if it has not been started yet.
    """
    exam = _student_exam(exam_id, current_user)
    submission = exam_service.ensure_submission(exam, current_user.id)
    saved = exam_service.save_answers(exam, submission, answers_in.answers)
    return StudentSubmissionOut(**exam_service.hydrate_submission_for_student(saved, exam))


@student_router.post("/exams/{exam_id}/attachments", response_model=StudentSubmissionOut)
async def upload_my_answer_sheet(
    exam_id: int,
    file: UploadFile = File(..., description="The answer sheet: a PDF, document, or photo."),
    question_id: Optional[int] = Form(None, description="Attach to one question instead of the script."),
    current_user: UserOut = Depends(require_student),
):
    """
    [Student] Upload an answer sheet.

    The offline path: write on paper, scan it, upload it here, then call `/submit`. Also used
    for a single question's attachment on an online paper - a diagram, a worked derivation.

    Governed by the same clock as a typed answer. The exam's uploading concession is what lets
    a scan begun before the deadline finish landing after it.
    """
    exam = _student_exam(exam_id, current_user)
    submission = exam_service.ensure_submission(exam, current_user.id)
    updated = await exam_service.store_answer_sheet(exam, submission, file, question_id)
    return StudentSubmissionOut(**exam_service.hydrate_submission_for_student(updated, exam))


@student_router.post("/exams/{exam_id}/submit", response_model=StudentSubmissionOut)
def submit_my_exam(
    exam_id: int,
    submit_in: SubmitIn,
    current_user: UserOut = Depends(require_student),
):
    """
    [Student] Hand the paper in. Any answers sent along are saved first.

    Final: a handed-in script cannot be edited, and only a teacher can reopen it. If an answer
    key is set and the paper is entirely objective, it is marked immediately - but the mark
    stays hidden until the teacher publishes the results.
    """
    exam = _student_exam(exam_id, current_user)
    submission = exam_service.ensure_submission(exam, current_user.id)
    submitted = exam_service.submit(exam, submission, submit_in.answers)
    return StudentSubmissionOut(**exam_service.hydrate_submission_for_student(submitted, exam))


@student_router.get("/exams/{exam_id}/submission", response_model=StudentSubmissionOut)
def get_my_submission(exam_id: int, current_user: UserOut = Depends(require_student)):
    """
    [Student] This student's own script, and their result once it has been published.

    Before publication the marks read as null however long ago the teacher valued the paper.
    """
    exam = _student_exam(exam_id, current_user)

    submission = exam_service.get_submission(exam_id, current_user.id)
    if not submission:
        raise HTTPException(status_code=404, detail="You have not started this exam.")

    return StudentSubmissionOut(**exam_service.hydrate_submission_for_student(submission, exam))


@student_router.get("/report-cards", response_model=List[ReportCardOut])
def list_my_report_cards(current_user: UserOut = Depends(require_student)):
    """
    [Student] Report cards issued to this student.

    Only published cards are listed: a card a class teacher is still writing is not a result.
    """
    cards = [
        c for c in firestore_report_cards.query_documents("student_id", "==", current_user.id)
        if c.get("is_published")
    ]
    cards.sort(key=lambda c: c.get("generated_at") or "", reverse=True)
    report_card_service.prefetch_cards(cards)
    return [ReportCardOut(**report_card_service.hydrate_report_card(c)) for c in cards]


@student_router.get("/report-cards/{card_id}", response_model=ReportCardOut)
def get_my_report_card(card_id: str, current_user: UserOut = Depends(require_student)):
    """[Student] One of this student's published report cards."""
    card = require_document(firestore_report_cards, card_id, "Report card")

    if card.get("student_id") != current_user.id or not card.get("is_published"):
        # 404 rather than 403: another student's card is not something this student should be
        # able to confirm the existence of.
        raise HTTPException(status_code=404, detail="Report card not found")

    return ReportCardOut(**report_card_service.hydrate_report_card(card))
