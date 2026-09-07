"""
Exam, submission, and report-card records.

These dataclasses document the shape of the three Firestore collections the exam module
owns. As elsewhere in this codebase they are descriptive rather than load-bearing - the
routers read and write plain dicts - but they are the one place to look to answer "what is
actually stored on an exam?" without reading the service.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.enums import ExamMode, ExamStatus, GradingScheme, QuestionType, SubmissionStatus


@dataclass(slots=True)
class ExamQuestion:
    """
    One question on the exam form.

    Stored inside the exam document rather than in a collection of its own: a form is read as
    a whole every single time - by the student sitting it and by the teacher valuing it - and
    a separate collection would turn one read into one-per-question, which is the dominant
    cost of a request here.

    `correct_answer` is the answer key for this question and must never reach a student
    before results are published. `app.core.firebase.hydrate_exam_for_student` is the only
    function permitted to build a student-facing exam, and it strips this field.
    """
    id: int
    order: int
    question_type: QuestionType
    text: str
    marks: float = 1.0
    # [{"key": "A", "text": "..."}] for the choice types; empty for everything else.
    options: list[dict[str, Any]] = field(default_factory=list)
    # Option key(s) for choice types, a string for SHORT_ANSWER, a number for NUMERIC.
    correct_answer: Any = None
    # Absolute tolerance for NUMERIC, so 3.14 can be accepted for pi.
    tolerance: float | None = None
    # Shown with the result once published, so a student learns from the paper.
    answer_explanation: str | None = None
    required: bool = True
    # Lets a student attach a file to an otherwise written answer (a graph, a derivation).
    allow_attachments: bool = False


@dataclass(slots=True)
class Exam:
    """
    An exam: who set it, for whom, when it may be answered, and what it is worth.
    """
    class_id: int
    subject_id: int
    teacher_id: int
    title: str
    mode: ExamMode
    starts_at: datetime
    ends_at: datetime

    description: str | None = None
    instructions: str | None = None
    status: ExamStatus = ExamStatus.DRAFT
    # The admin or teacher who actually pressed create, which may not be `teacher_id` when an
    # admin sets an exam on a teacher's behalf.
    created_by: int | None = None

    # --- Valuation rules, fixed when the exam is created -------------------------------
    grading_scheme: GradingScheme = GradingScheme.MARKS
    max_marks: float = 100.0
    pass_marks: float | None = None
    # [{"grade": "A+", "min_percentage": 90.0}], highest band first.
    grade_bands: list[dict[str, Any]] = field(default_factory=list)

    # --- Time rules --------------------------------------------------------------------
    # Per-student time limit once they start, within the window. None means "the whole window".
    duration_minutes: int | None = None
    # The uploading concession: minutes past `ends_at` in which a hand-in is still accepted,
    # flagged late. Covers the slow scan and the slow connection without extending the exam.
    upload_grace_minutes: int = 0
    late_submission_allowed: bool = True
    # Per-student extra time, keyed by student id as a string: {"1712...": 15}. An access
    # arrangement granted individually, added to both the window and the duration.
    time_concessions: dict[str, int] = field(default_factory=dict)

    # --- The online form ---------------------------------------------------------------
    questions: list[dict[str, Any]] = field(default_factory=list)
    shuffle_questions: bool = False
    # Whether objective answers are marked from the key the moment a script is handed in.
    auto_grade_objective: bool = True

    # --- The offline paper -------------------------------------------------------------
    question_paper_url: str | None = None
    question_paper_provider: str | None = None
    question_paper_warning: str | None = None
    max_upload_files: int = 5

    # --- Results -----------------------------------------------------------------------
    results_published: bool = False
    results_published_at: datetime | None = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class ExamSubmission:
    """
    One student's script for one exam. At most one per (exam, student) pair - the document id
    is derived from both, so a double submit updates rather than duplicates.
    """
    exam_id: int
    student_id: int
    class_id: int
    subject_id: int

    status: SubmissionStatus = SubmissionStatus.IN_PROGRESS
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    # The moment this particular student's time runs out, fixed when they start so that a
    # concession granted mid-exam cannot shorten a paper already under way.
    expires_at: datetime | None = None
    is_late: bool = False
    late_by_minutes: float = 0.0

    # [{"question_id": 1, "answer": ..., "attachments": [...]}]
    answers: list[dict[str, Any]] = field(default_factory=list)
    # Answer sheets for an offline exam: [{"file_url", "filename", "provider", "uploaded_at"}]
    attachments: list[dict[str, Any]] = field(default_factory=list)

    # --- Valuation ---------------------------------------------------------------------
    # [{"question_id", "marks_awarded", "auto", "remarks"}] - the mark sheet, per question.
    question_scores: list[dict[str, Any]] = field(default_factory=list)
    marks_obtained: float | None = None
    percentage: float | None = None
    grade: str | None = None
    passed: bool | None = None
    evaluator_remarks: str | None = None
    evaluated_by: int | None = None
    evaluated_at: datetime | None = None
    # What the key settled on its own, kept apart from the total so a teacher can see how much
    # of the mark they awarded themselves.
    auto_graded_marks: float | None = None

    # The `exam_grades` document written when results were published, so republishing
    # corrects that row instead of adding a second one for the same exam.
    grade_record_id: int | None = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: str | None = None


@dataclass(slots=True)
class ReportCard:
    """
    A student's consolidated result across every exam in a class.

    A snapshot, not a live view: it records what the marks were when the class teacher issued
    it. Re-generating writes a new version of the same card, but a card already handed out
    does not silently change because somebody corrected a mark afterwards.
    """
    student_id: int
    class_id: int
    title: str

    generated_by: int | None = None
    generated_at: datetime = field(default_factory=datetime.utcnow)
    # The window of exams the card covers; None on either side means unbounded.
    from_date: datetime | None = None
    to_date: datetime | None = None

    # One entry per subject, each carrying its exams: see `app.services.report_cards`.
    subjects: list[dict[str, Any]] = field(default_factory=list)

    total_marks: float = 0.0
    total_max_marks: float = 0.0
    overall_percentage: float = 0.0
    overall_grade: str | None = None
    exams_counted: int = 0
    exams_missed: int = 0
    attendance_percentage: float | None = None
    rank: int | None = None
    class_size: int | None = None

    remarks: str | None = None
    is_published: bool = False
    published_at: datetime | None = None
    # Derived from (class, student, title) so re-issuing overwrites rather than duplicates.
    id: str | None = None
