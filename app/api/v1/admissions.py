"""
Admin endpoints for academic years and admission categories.

Admin-only in both directions. A student has no business changing which year they were
admitted into, and a teacher has no use for the collection at all - what either of them needs
is the *effect* of these records, which reaches them through their own profile.

The one exception is `GET /admissions/years/current`, which any signed-in user may read: a
frontend renders "2025-26" in its header for everybody, and making that an admin call would
mean the header is blank for the people who are not admins.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.v1.dependencies import require_admin, require_any_authenticated
from app.core.enums import Program
from app.schemas.admission import (
    AcademicYearCreate, AcademicYearOut, AcademicYearUpdate,
    AdmissionCategoryCreate, AdmissionCategoryOut, AdmissionCategoryUpdate,
    AssignStudentsRequest, AssignStudentsResult, PromoteRequest, PromoteResult,
    YearStudentOut,
)
from app.schemas.user import UserOut
from app.services import admissions as service

router = APIRouter(prefix="/admissions", tags=["Admissions"])


# ---------------------------------------------------------------------------------------
# Academic years
# ---------------------------------------------------------------------------------------

@router.post("/years", response_model=AcademicYearOut, status_code=status.HTTP_201_CREATED)
def create_year(payload: AcademicYearCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a session year.

    Setting `is_current` clears the flag on every other year in the same write, so the
    "exactly one current year" rule cannot be broken by creating a second one.
    """
    return AcademicYearOut(**service.present_year(service.create_year(payload, admin.id)))


@router.get("/years", response_model=List[AcademicYearOut])
def list_years(
    program: Optional[Program] = Query(None, description="Filter to LMS or TUITION"),
    with_counts: bool = Query(
        False, description="Include student and category counts. One extra query per row."
    ),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Every session year, newest first."""
    years = service.list_years(program.value if program else None)
    return [AcademicYearOut(**service.present_year(y, with_counts=with_counts)) for y in years]


@router.get("/years/current", response_model=Optional[AcademicYearOut])
def get_current_year(
    program: Program = Query(Program.LMS, description="Which product's calendar"),
    _: UserOut = Depends(require_any_authenticated),
):
    """
    The session year everything defaults to.

    Readable by any signed-in user, so a frontend can show the year in its header without
    being an admin. Returns `null` rather than 404 when no year has been created: a school
    that has not set admissions up is not in an error state, and the frontend should render
    nothing rather than an error toast.
    """
    year = service.current_year(program.value)
    return AcademicYearOut(**service.present_year(year)) if year else None


@router.get("/years/{year_id}", response_model=AcademicYearOut)
def get_year(year_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One session year, with its counts."""
    year = service.require_year(year_id)
    return AcademicYearOut(**service.present_year(year, with_counts=True))


@router.put("/years/{year_id}", response_model=AcademicYearOut)
def update_year(
    year_id: int, payload: AcademicYearUpdate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a session year. Partial: omitted fields are left unchanged.

    Setting `is_current` here has the same exclusivity effect as at creation - this is how
    a school rolls over to the next year.
    """
    year = service.require_year(year_id)
    return AcademicYearOut(**service.present_year(service.update_year(year, payload, admin.id)))


@router.delete("/years/{year_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_year(year_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete a session year.

    Refused while students are admitted into it or categories are scoped to it; the error
    names what is in the way. A year that is simply over should be set to CLOSED, not
    deleted - every historical report resolves ids through this collection.
    """
    service.delete_year(service.require_year(year_id))


# ---------------------------------------------------------------------------------------
# Admission categories
# ---------------------------------------------------------------------------------------

@router.post("/categories", response_model=AdmissionCategoryOut,
             status_code=status.HTTP_201_CREATED)
def create_category(
    payload: AdmissionCategoryCreate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Create an admission category - "Regular", "Staff Ward", "Transfer".

    Leave `academic_year_id` empty for a standing category that every year inherits, which
    is what you want for all but the one-off scholarship schemes.

    `default_discount_percent` is the concession the category itself carries. It is applied
    before any sibling or multi-registration discount when a fee is worked out.
    """
    return AdmissionCategoryOut(
        **service.present_category(service.create_category(payload, admin.id))
    )


@router.get("/categories", response_model=List[AdmissionCategoryOut])
def list_categories(
    program: Optional[Program] = Query(None),
    academic_year_id: Optional[int] = Query(
        None, description="Categories applying to this year, standing ones included."
    ),
    include_inactive: bool = Query(False),
    with_counts: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Admission categories, in display order."""
    categories = service.list_categories(
        program=program.value if program else None,
        academic_year_id=academic_year_id,
        include_inactive=include_inactive,
    )
    return [
        AdmissionCategoryOut(**service.present_category(c, with_counts=with_counts))
        for c in categories
    ]


@router.get("/categories/{category_id}", response_model=AdmissionCategoryOut)
def get_category(category_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One admission category, with its student count."""
    category = service.require_category(category_id)
    return AdmissionCategoryOut(**service.present_category(category, with_counts=True))


@router.put("/categories/{category_id}", response_model=AdmissionCategoryOut)
def update_category(
    category_id: int, payload: AdmissionCategoryUpdate,
    admin: UserOut = Depends(require_admin),
):
    """[Admin Only] Update a category. Partial: omitted fields are left unchanged."""
    category = service.require_category(category_id)
    return AdmissionCategoryOut(
        **service.present_category(service.update_category(category, payload, admin.id))
    )


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete an admission category.

    Refused while any student holds it. Set `is_active` to false instead: the category
    leaves the admission form's dropdown while every student already admitted under it keeps
    a resolvable record of why they pay what they pay.
    """
    service.delete_category(service.require_category(category_id))


# ---------------------------------------------------------------------------------------
# Mapping students into a year
# ---------------------------------------------------------------------------------------

@router.get("/years/{year_id}/students", response_model=List[YearStudentOut])
def year_roster(
    year_id: int,
    class_id: Optional[int] = Query(None, description="Narrow to one class."),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Who is mapped into this session year.

    Sorted by class then name - the order a roster is read in and checked against a paper
    list. Each row carries the student's class and admission category already resolved, so
    the screen renders without a request per student.
    """
    service.require_year(year_id)
    return [
        YearStudentOut(**row)
        for row in service.students_in_year_detailed(year_id, class_id, include_inactive)
    ]


@router.get("/students/unassigned", response_model=List[YearStudentOut])
def unassigned_students(
    include_inactive: bool = Query(False), _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Students carrying no academic year at all.

    Every account created before the admissions module existed is in here, which is exactly
    who to map first. Feed these ids straight into
    `POST /admissions/years/{year_id}/students`.
    """
    return [YearStudentOut(**row) for row in service.unassigned_students(include_inactive)]


@router.post("/years/{year_id}/students", response_model=AssignStudentsResult)
def assign_students(
    year_id: int, payload: AssignStudentsRequest, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Map a list of students into this year, in one call.

    Use `dry_run: true` first to see what would change. Ids that are not students, or do not
    exist, come back under `skipped` rather than failing the whole request - a roster pasted
    from a spreadsheet nearly always has one bad row.

    This sets the year only. To move students up a class as well, use `/promote`.
    """
    return AssignStudentsResult(**service.assign_students(
        year_id, payload.student_ids, admin.id, payload.dry_run
    ))


@router.post("/years/{year_id}/promote", response_model=PromoteResult)
def promote_into_year(
    year_id: int, payload: PromoteRequest, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] The July rollover: move last year's students into this year, up a class.

    `year_id` in the path is the year being promoted **into**; `source_year_id` in the body
    is the year they are coming from.

    Two things change per student: the `academic_year_id` on their profile, and the
    `student_enrollments` row that says which class they sit in. Doing only the first is the
    bug that leaves a whole school enrolled in last year's classes, so both happen here.

    **Defaults to a dry run.** Send `dry_run: false` to apply it. Preview first - this
    touches every active student in the source year, and the response lists each one with the
    class they would move from and to.

    Classes absent from `class_map` keep their students where they are. Classes in
    `graduating_class_ids` are left in the source year entirely: leaving school has fee and
    record consequences, and a bulk operation quietly closing thirty accounts is not
    something anybody finds until a parent cannot log in.
    """
    return PromoteResult(**service.promote(
        payload.source_year_id, year_id, payload.class_map,
        payload.graduating_class_ids, admin.id, payload.dry_run,
    ))
