"""
Request and response schemas for the exam module.

Three audiences read an exam and they must not be served the same document:

* **staff** (the setter, the class teacher, an admin) see everything, answer key included;
* **a student sitting the paper** sees the questions and none of the key;
* **a student reading a published result** sees the key and the explanation as well, because
  that is what makes a returned paper worth anything.

`ExamOut` and `StudentExamOut` are separate types for exactly that reason. A single schema
with optional fields would leak the key the first time somebody forgot to blank it.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    CHOICE_QUESTION_VALUES, ExamMode, ExamStatus, ExamWindowState, GradingScheme,
    QuestionType, SubmissionStatus,
)
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


# --------------------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------------------

class GradeBand(BaseModel):
    """
    One letter grade and the percentage at which it starts.

    Only the floor is stored. The ceiling is whatever the next band up begins at, so a scale
    can never be defined with a gap between 79.5 and 80 that no student can be given.
    """
    grade: str = Field(..., min_length=1, max_length=8)
    min_percentage: float = Field(..., ge=0.0, le=100.0)
    description: str | None = None


# --------------------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------------------

class QuestionOption(BaseModel):
    key: str = Field(..., min_length=1, max_length=8, description="A, B, C... or TRUE/FALSE")
    text: str


class ExamQuestionIn(BaseModel):
    """
    A question as the setter writes it. `correct_answer` is optional here because the answer
    key may legitimately be set later - the requirement is that a key can be attached when the
    exam is created *or* once the scripts are in.
    """
    question_type: QuestionType
    text: str = Field(..., min_length=1)
    marks: float = Field(1.0, gt=0)
    options: list[QuestionOption] = Field(default_factory=list)
    correct_answer: Any = None
    tolerance: float | None = Field(None, ge=0)
    answer_explanation: str | None = None
    required: bool = True
    allow_attachments: bool = False
    # Supplied only when editing an existing form, to keep answers already filed against a
    # question attached to it. Omit it and the question is treated as new.
    id: int | None = None
    order: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ExamQuestionIn":
        kind = self.question_type.value

        if kind in CHOICE_QUESTION_VALUES:
            if kind == QuestionType.TRUE_FALSE.value and not self.options:
                # The only sensible options for a true/false, so writing them out by hand on
                # every question is busywork the setter should not have to do.
                self.options = [
                    QuestionOption(key="TRUE", text="True"),
                    QuestionOption(key="FALSE", text="False"),
                ]
            if len(self.options) < 2:
                raise ValueError(f"A {kind} question needs at least two options.")

            keys = [o.key for o in self.options]
            if len(set(keys)) != len(keys):
                raise ValueError(f"Option keys must be unique within a question: {keys}")

            if self.correct_answer is not None:
                chosen = self.correct_answer if isinstance(self.correct_answer, list) else [self.correct_answer]
                unknown = [c for c in chosen if c not in keys]
                if unknown:
                    raise ValueError(
                        f"Answer key {unknown} does not match any option of this question ({keys})."
                    )
                if kind != QuestionType.MULTI_SELECT.value and len(chosen) > 1:
                    raise ValueError(f"A {kind} question has exactly one correct option.")
        elif self.options:
            raise ValueError(f"A {kind} question does not take options.")

        if self.tolerance is not None and self.question_type is not QuestionType.NUMERIC:
            raise ValueError("`tolerance` only applies to a NUMERIC question.")

        if self.question_type is QuestionType.NUMERIC and self.correct_answer is not None:
            try:
                float(self.correct_answer)
            except (TypeError, ValueError):
                raise ValueError("The answer key for a NUMERIC question must be a number.")

        return self


class ExamQuestionOut(BaseModel):
    """A question as staff see it: the key included."""
    id: int
    order: int
    question_type: QuestionType
    text: str
    marks: float
    options: list[QuestionOption] = Field(default_factory=list)
    correct_answer: Any = None
    tolerance: float | None = None
    answer_explanation: str | None = None
    required: bool = True
    allow_attachments: bool = False

    model_config = ConfigDict(from_attributes=True)


class StudentQuestionOut(BaseModel):
    """
    A question as a student sees it while sitting the paper.

    `correct_answer` and `answer_explanation` exist on the model but are populated only once
    the exam's results are published - see `hydrate_exam_for_student`.
    """
    id: int
    order: int
    question_type: QuestionType
    text: str
    marks: float
    options: list[QuestionOption] = Field(default_factory=list)
    required: bool = True
    allow_attachments: bool = False
    correct_answer: Any = None
    answer_explanation: str | None = None

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------------------
# Exams
# --------------------------------------------------------------------------------------

class _ExamRules(BaseModel):
    """The valuation and timing fields shared by create and update."""

    grading_scheme: GradingScheme = GradingScheme.MARKS
    max_marks: float | None = Field(None, gt=0)
    pass_marks: float | None = Field(None, ge=0)
    grade_bands: list[GradeBand] = Field(default_factory=list)

    duration_minutes: int | None = Field(None, gt=0, le=24 * 60)
    upload_grace_minutes: int = Field(
        0, ge=0, le=24 * 60,
        description="Uploading concession: minutes past the deadline a hand-in is still accepted, flagged late.",
    )
    late_submission_allowed: bool = True

    shuffle_questions: bool = False
    auto_grade_objective: bool = True
    max_upload_files: int = Field(5, ge=1, le=25)


class ExamCreate(_ExamRules):
    """
    Everything needed to set an exam.

    `teacher_id` is for an admin setting an exam on a teacher's behalf; a teacher creating
    their own leaves it out and the exam is filed under them.
    """
    class_id: int
    subject_id: int
    title: str = Field(..., min_length=1)
    mode: ExamMode
    starts_at: datetime
    ends_at: datetime

    description: str | None = None
    instructions: str | None = None
    status: ExamStatus = Field(
        ExamStatus.DRAFT,
        description="DRAFT keeps the exam hidden from students until you publish it.",
    )
    teacher_id: int | None = None
    questions: list[ExamQuestionIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_rules(self) -> "ExamCreate":
        _validate_window(self.starts_at, self.ends_at, self.duration_minutes)
        _validate_scheme(self.grading_scheme, self.grade_bands, self.max_marks, self.pass_marks)

        if self.mode is ExamMode.ONLINE and self.status is ExamStatus.PUBLISHED and not self.questions:
            raise ValueError(
                "An ONLINE exam needs at least one question before it can be published. "
                "Create it as a DRAFT and add the form first."
            )
        return self


class ExamUpdate(BaseModel):
    """
    Partial update; omitted fields are left unchanged.

    `mode` is absent on purpose - see `ExamMode`. Questions are replaced through their own
    endpoint so that a careless PUT of the exam's metadata cannot wipe the form.
    """
    title: str | None = None
    description: str | None = None
    instructions: str | None = None
    status: ExamStatus | None = None
    teacher_id: int | None = None

    starts_at: datetime | None = None
    ends_at: datetime | None = None
    duration_minutes: int | None = Field(None, gt=0, le=24 * 60)
    upload_grace_minutes: int | None = Field(None, ge=0, le=24 * 60)
    late_submission_allowed: bool | None = None

    grading_scheme: GradingScheme | None = None
    max_marks: float | None = Field(None, gt=0)
    pass_marks: float | None = Field(None, ge=0)
    grade_bands: list[GradeBand] | None = None

    shuffle_questions: bool | None = None
    auto_grade_objective: bool | None = None
    max_upload_files: int | None = Field(None, ge=1, le=25)


class QuestionFormUpdate(BaseModel):
    """Replaces the whole form in one call, so question order is never ambiguous."""
    questions: list[ExamQuestionIn]


class AnswerKeyItem(BaseModel):
    question_id: int
    correct_answer: Any = None
    tolerance: float | None = Field(None, ge=0)
    answer_explanation: str | None = None


class AnswerKeyUpdate(BaseModel):
    """
    Sets or corrects the answer key after the fact.

    `regrade` re-marks every objective answer already submitted against the new key, which is
    the point of being allowed to set a key late. Turn it off to fix a typo in an explanation
    without disturbing marks a teacher has since adjusted by hand.
    """
    answers: list[AnswerKeyItem]
    regrade: bool = True


class TimeConcessionGrant(BaseModel):
    """
    Extra minutes for one student, added to both the exam window and their personal duration.
    Zero removes an existing concession.
    """
    student_id: int
    extra_minutes: int = Field(..., ge=0, le=24 * 60)
    reason: str | None = None


class ExamOut(BaseModel):
    """The staff view. Carries the answer key; never return this to a student."""
    id: int

    # --- Which product this belongs to -------------------------------------------------
    # "LMS" for a school exam set for a class, "TUITION" for one set for a single student on
    # a one-to-one arrangement. Absent on exams written before the tuition module existed,
    # which read as LMS - which is what they are.
    program: str = "LMS"
    category: str = Field("EXAM", description="EXAM | HOMEWORK | ASSIGNMENT | TEST | PROJECT")
    student_id: int | None = Field(
        None, description="The single student a tuition assessment is set for. Null for a "
                          "class exam, whose roster comes from the class."
    )
    enrollment_id: int | None = None

    # Null on a tuition assessment, which is set for one student rather than a class. Every
    # *output* schema in this module allows it; `ExamCreate` above does not, because the LMS
    # route that uses it genuinely does require a class. See `app.services.exams.roster_for`.
    class_id: int | None = None
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    teacher: UserOut | None = None
    created_by: int | None = None

    title: str
    description: str | None = None
    instructions: str | None = None
    mode: ExamMode
    status: ExamStatus
    window_state: ExamWindowState

    grading_scheme: GradingScheme
    max_marks: float
    pass_marks: float | None = None
    grade_bands: list[GradeBand] = Field(default_factory=list)

    starts_at: datetime
    ends_at: datetime
    duration_minutes: int | None = None
    upload_grace_minutes: int = 0
    late_submission_allowed: bool = True
    time_concessions: dict[str, int] = Field(default_factory=dict)

    questions: list[ExamQuestionOut] = Field(default_factory=list)
    question_count: int = 0
    # The sum of the questions' marks. Shown next to `max_marks` so a form that does not add
    # up to what the exam is out of is visible to the setter rather than discovered at valuation.
    questions_total_marks: float = 0.0
    answer_key_complete: bool = False
    shuffle_questions: bool = False
    auto_grade_objective: bool = True

    question_paper_url: str | None = None
    question_paper_provider: str | None = None
    question_paper_warning: str | None = None
    max_upload_files: int = 5

    results_published: bool = False
    results_published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class StudentExamOut(BaseModel):
    """
    The student view: the paper without the key, plus where they personally stand.

    The timing fields are resolved for *this* student, so a granted concession shows up as a
    later `closes_at` rather than as a rule the client has to apply itself.
    """
    id: int
    program: str = "LMS"
    category: str = "EXAM"
    enrollment_id: int | None = None

    # Null on a tuition assessment, which is set for one student rather than a class. Every
    # *output* schema in this module allows it; `ExamCreate` above does not, because the LMS
    # route that uses it genuinely does require a class. See `app.services.exams.roster_for`.
    class_id: int | None = None
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher: UserOut | None = None

    title: str
    description: str | None = None
    instructions: str | None = None
    mode: ExamMode
    status: ExamStatus
    window_state: ExamWindowState

    grading_scheme: GradingScheme
    max_marks: float
    pass_marks: float | None = None

    starts_at: datetime
    ends_at: datetime
    # `ends_at` plus this student's concession and the upload grace: the last moment a
    # hand-in of theirs is accepted.
    closes_at: datetime
    duration_minutes: int | None = None
    extra_time_minutes: int = 0
    upload_grace_minutes: int = 0
    late_submission_allowed: bool = True

    question_paper_url: str | None = None
    max_upload_files: int = 5
    questions: list[StudentQuestionOut] = Field(default_factory=list)
    question_count: int = 0

    results_published: bool = False

    # This student's own state, so a list screen needs no second call per exam.
    submission_status: SubmissionStatus | None = None
    submitted_at: datetime | None = None
    expires_at: datetime | None = None
    can_start: bool = False
    can_submit: bool = False

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------------------
# Submissions
# --------------------------------------------------------------------------------------

class AnswerIn(BaseModel):
    question_id: int
    # A key or list of keys for choice questions, text for written ones, a number for NUMERIC.
    answer: Any = None
    # File urls previously returned by the attachment endpoint.
    attachments: list[str] = Field(default_factory=list)


class AnswerSaveIn(BaseModel):
    """
    Saves progress without handing in. Answers are merged by question id, so a client can
    autosave one question at a time instead of resending the whole paper.
    """
    answers: list[AnswerIn]


class SubmitIn(BaseModel):
    """Hands the paper in. Any answers supplied are merged first, so a single call can do both."""
    answers: list[AnswerIn] = Field(default_factory=list)


class AnswerOut(BaseModel):
    question_id: int
    answer: Any = None
    attachments: list[str] = Field(default_factory=list)


class AttachmentOut(BaseModel):
    file_url: str
    filename: str | None = None
    provider: str | None = None
    storage_warning: str | None = None
    uploaded_at: datetime | None = None
    question_id: int | None = None


class QuestionScoreOut(BaseModel):
    question_id: int
    marks_awarded: float
    max_marks: float | None = None
    auto: bool = False
    remarks: str | None = None


class SubmissionOut(BaseModel):
    """One script, as staff read it during valuation."""
    id: str
    exam_id: int
    student_id: int
    student: UserOut | None = None
    # Null on a tuition assessment, which is set for one student rather than a class. Every
    # *output* schema in this module allows it; `ExamCreate` above does not, because the LMS
    # route that uses it genuinely does require a class. See `app.services.exams.roster_for`.
    class_id: int | None = None
    subject_id: int

    status: SubmissionStatus
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    expires_at: datetime | None = None
    is_late: bool = False
    late_by_minutes: float = 0.0

    answers: list[AnswerOut] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)

    question_scores: list[QuestionScoreOut] = Field(default_factory=list)
    marks_obtained: float | None = None
    percentage: float | None = None
    grade: str | None = None
    passed: bool | None = None
    auto_graded_marks: float | None = None
    evaluator_remarks: str | None = None
    evaluated_by: int | None = None
    evaluator: UserOut | None = None
    evaluated_at: datetime | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class StudentSubmissionOut(BaseModel):
    """
    A student's own script. The valuation fields are filled in only once results are
    published, so a marked-but-unreleased paper reads exactly like an unmarked one.
    """
    id: str
    exam_id: int
    status: SubmissionStatus
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    expires_at: datetime | None = None
    is_late: bool = False

    answers: list[AnswerOut] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)

    results_published: bool = False
    marks_obtained: float | None = None
    max_marks: float | None = None
    percentage: float | None = None
    grade: str | None = None
    passed: bool | None = None
    evaluator_remarks: str | None = None
    question_scores: list[QuestionScoreOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class QuestionScoreIn(BaseModel):
    question_id: int
    marks_awarded: float = Field(..., ge=0)
    remarks: str | None = None


class EvaluationIn(BaseModel):
    """
    A valued script.

    Under MARKS, supply either `question_scores` (and the total is added up for you) or a
    flat `marks_obtained` - a per-question sheet is the natural fit for an online form, a
    single total for a paper the teacher marked with a red pen. Under GRADE, supply `grade`.
    """
    question_scores: list[QuestionScoreIn] = Field(default_factory=list)
    marks_obtained: float | None = Field(None, ge=0)
    grade: str | None = None
    remarks: str | None = None

    @model_validator(mode="after")
    def _something_to_record(self) -> "EvaluationIn":
        if not self.question_scores and self.marks_obtained is None and self.grade is None:
            raise ValueError(
                "Provide question_scores, marks_obtained, or grade - there is nothing to record."
            )
        return self


class ResultsPublished(BaseModel):
    """What publishing an exam's results actually did."""
    exam_id: int
    published: int
    skipped_unevaluated: int
    grade_rows_written: int
    message: str


class ExamStats(BaseModel):
    """A quick read on where an exam has got to, for the teacher's dashboard."""
    exam_id: int
    title: str
    # Null on a tuition assessment, which is set for one student rather than a class. Every
    # *output* schema in this module allows it; `ExamCreate` above does not, because the LMS
    # route that uses it genuinely does require a class. See `app.services.exams.roster_for`.
    class_id: int | None = None
    enrolled_students: int = Field(
        ..., description="Students entitled to sit it: the class roster, or 1 for tuition."
    )
    started: int
    submitted: int
    evaluated: int
    missing: int
    late: int
    average_percentage: float | None = None
    highest_percentage: float | None = None
    lowest_percentage: float | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    results_published: bool = False


# --------------------------------------------------------------------------------------
# Shared validation
# --------------------------------------------------------------------------------------

def _validate_window(starts_at: datetime, ends_at: datetime, duration_minutes: int | None) -> None:
    if ends_at <= starts_at:
        raise ValueError("`ends_at` must be after `starts_at`.")

    if duration_minutes is not None:
        window = (ends_at - starts_at).total_seconds() / 60
        if duration_minutes > window:
            raise ValueError(
                f"`duration_minutes` ({duration_minutes}) is longer than the window between "
                f"`starts_at` and `ends_at` ({window:.0f} minutes), so no student could ever "
                "use their full time."
            )


def _validate_scheme(
    scheme: GradingScheme,
    bands: list[GradeBand],
    max_marks: float | None,
    pass_marks: float | None,
) -> None:
    if scheme is GradingScheme.GRADE and not bands:
        raise ValueError(
            "A GRADE exam needs `grade_bands`, otherwise there is no scale to award from."
        )

    if bands:
        labels = [b.grade for b in bands]
        if len(set(labels)) != len(labels):
            raise ValueError(f"Grade labels must be unique: {labels}")

        floors = [b.min_percentage for b in bands]
        if len(set(floors)) != len(floors):
            raise ValueError(
                "Two grades cannot start at the same percentage - the lower one could never be awarded."
            )

    if pass_marks is not None and max_marks is not None and pass_marks > max_marks:
        raise ValueError(f"`pass_marks` ({pass_marks}) cannot exceed `max_marks` ({max_marks}).")

