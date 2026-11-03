"""
Report card schemas.

A report card consolidates every exam a student sat in one class over a period. It is issued
by the class teacher (or an admin) and is a *snapshot*: the numbers on it are the numbers as
they stood when it was generated. See `app.services.report_cards` for why.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.exam import GradeBand
from app.schemas.user import UserOut


class ReportCardExamLine(BaseModel):
    """One exam's row on the card."""
    exam_id: int
    title: str
    mode: str | None = None
    conducted_on: datetime | None = None
    grading_scheme: str | None = None
    marks_obtained: float | None = None
    max_marks: float | None = None
    percentage: float | None = None
    grade: str | None = None
    passed: bool | None = None
    # True when the student never handed in. Counted as a zero only if the card was generated
    # with `count_missing_as_zero`; otherwise the exam is excluded from the totals and this
    # line stands as the record of why.
    missed: bool = False
    remarks: str | None = None


class ReportCardSubjectLine(BaseModel):
    """A subject's block: its exams, and what they add up to."""
    subject_id: int
    subject_name: str | None = None
    subject_code: str | None = None
    exams: list[ReportCardExamLine] = Field(default_factory=list)
    total_marks: float = 0.0
    total_max_marks: float = 0.0
    percentage: float | None = None
    grade: str | None = None
    exams_counted: int = 0
    exams_missed: int = 0
    teacher_remarks: str | None = None


class ReportCardGenerate(BaseModel):
    """
    Issues report cards for a class.

    Leave `student_ids` empty to cover every enrolled student, which is the usual case at the
    end of a term. `from_date`/`to_date` bound which exams count, so a term card does not
    silently absorb last term's papers.
    """
    class_id: int
    title: str = Field(..., min_length=1, description='e.g. "Term 1 Report Card 2026"')
    student_ids: list[int] = Field(default_factory=list)
    exam_ids: list[int] = Field(default_factory=list, description="Restrict to these exams only.")
    from_date: datetime | None = None
    to_date: datetime | None = None

    # Only exams whose results have been released. Turn it off to preview a card mid-term.
    published_results_only: bool = True
    # A paper never handed in scores zero rather than being left out. Off by default, because
    # silently zeroing an absence a school has excused is the worse mistake of the two.
    count_missing_as_zero: bool = False
    # The scale used for the subject and overall grades. Empty falls back to each exam's own
    # bands where they agree, and to no letter at all where they do not.
    grade_bands: list[GradeBand] = Field(default_factory=list)
    include_attendance: bool = True
    # Position within the class by overall percentage. Needs the whole class computed, so it
    # is only available when `student_ids` is empty.
    include_rank: bool = True
    remarks: str | None = None
    # Issue to students immediately. Off by default so a class teacher can read the cards
    # before the class does.
    publish: bool = False

    @model_validator(mode="after")
    def _check_range(self) -> "ReportCardGenerate":
        if self.from_date and self.to_date and self.to_date <= self.from_date:
            raise ValueError("`to_date` must be after `from_date`.")
        if self.include_rank and self.student_ids:
            # Ranking a subset would produce a position within that subset, which reads as a
            # class rank and is not one.
            raise ValueError(
                "`include_rank` needs the whole class. Leave `student_ids` empty, or set "
                "`include_rank` to false."
            )
        return self


class ReportCardUpdate(BaseModel):
    """Edits the parts of an issued card a human owns: the remarks, and whether it is out."""
    remarks: str | None = None
    title: str | None = None
    is_published: bool | None = None
    # Per-subject teacher remarks, keyed by subject id as a string.
    subject_remarks: dict[str, str] | None = None


class ReportCardOut(BaseModel):
    id: str
    student_id: int
    student: UserOut | None = None
    # "LMS" for a class card, "TUITION" for one spanning a student's one-to-one subjects.
    # A tuition card carries no rank or class size: a one-to-one student has no cohort.
    program: str = "LMS"
    # Null on a tuition record, which belongs to one student rather than a class.
    class_id: int | None = None
    class_room: ClassRoomOut | None = None

    title: str
    generated_by: int | None = None
    generated_at: datetime
    from_date: datetime | None = None
    to_date: datetime | None = None

    subjects: list[ReportCardSubjectLine] = Field(default_factory=list)
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

    model_config = ConfigDict(from_attributes=True)


class ReportCardBatch(BaseModel):
    """The outcome of generating a whole class's cards in one call."""
    class_id: int
    generated: int
    skipped: int
    published: bool
    cards: list[ReportCardOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------
# Marks for exams held on paper
#
# A term exam the school sets and invigilates in the hall never passes through the LMS, so
# there is no exam record for a report card to read. The class teacher types the marks into
# a register instead - one sheet per exam, a column per subject - and the cards count them.
# See `app.services.paper_marks`.
# ---------------------------------------------------------------------------------------

class PaperMark(BaseModel):
    student_id: int
    # None clears the student's mark: absent, or not entered yet. Never stored as a zero.
    marks_obtained: float | None = Field(None, ge=0)


class PaperSubjectMarks(BaseModel):
    subject_id: int
    max_marks: float = Field(..., gt=0)
    marks: list[PaperMark] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_marks(self) -> "PaperSubjectMarks":
        for entry in self.marks:
            if entry.marks_obtained is not None and entry.marks_obtained > self.max_marks:
                raise ValueError(f"A mark of {entry.marks_obtained:g} is more than the {self.max_marks:g} it is out of.")
        return self


class PaperMarkSheetSave(BaseModel):
    """
    Saves one exam's marks for a class.

    `previous_exam_name` is the sheet being edited, so a renamed sheet moves its marks rather
    than starting a second one. Leave it empty for a new sheet. `subjects` are the subjects the
    exam had: one the sheet had before that is left out is removed, marks and all.
    """
    class_id: int
    exam_name: str = Field(..., min_length=1, max_length=120)
    previous_exam_name: str | None = None
    conducted_on: date
    subjects: list[PaperSubjectMarks] = Field(..., min_length=1)


class PaperSheetSubject(BaseModel):
    subject_id: int
    max_marks: float | None = None
    # Student id (as a string) -> marks. Only students with a mark appear.
    marks: dict[str, float] = Field(default_factory=dict)


class PaperMarkSheet(BaseModel):
    exam_name: str
    conducted_on: date | None = None
    subjects: list[PaperSheetSubject] = Field(default_factory=list)
    entered_by: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class PaperRegisterStudent(BaseModel):
    id: int
    full_name: str
    roll_number: str | None = None
    admission_number: str | None = None


class PaperMarksRegister(BaseModel):
    """Everything the register screen needs for one class, in one call."""
    class_id: int
    class_room: ClassRoomOut | None = None
    students: list[PaperRegisterStudent] = Field(default_factory=list)
    # The subjects taught in the class (and any a sheet already has), then the rest of the
    # school's, for an exam in a subject nobody is mapped to teach in this class.
    subjects: list[SubjectOut] = Field(default_factory=list)
    other_subjects: list[SubjectOut] = Field(default_factory=list)
    sheets: list[PaperMarkSheet] = Field(default_factory=list)


class PaperMarkSheetSaved(BaseModel):
    sheet: PaperMarkSheet
    saved: int
    cleared: int
    unchanged: int
