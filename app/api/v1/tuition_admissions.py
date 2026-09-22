"""
Online tuition - session years and admission categories, on their own board.

The records live in the same two collections as the school's (`academic_years` and
`admission_categories`), each already carrying a `programs` list. What this router adds is
the *separate option* the brief asks for: an administrator opening the tuition module sees
only tuition years and tuition categories, creates records that are tuition records without
ticking anything, and cannot reach a school year from here by guessing its id.

Everything is delegated to `app.services.admissions`. Duplicating the current-year rule, the
name check or the delete guard here would give the two boards two slightly different sets of
rules, and the whole point of the shared collection is that they cannot drift.

A year or category that declares both products appears on both boards - it *is* on both.
Moving a record off the tuition board is refused rather than silently done: an update that
drops TUITION from `programs` would make the record vanish from the screen that just edited
it, which reads as a lost record rather than as a re-scope.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import require_admin, require_tuition_user
from app.core.enums import Program
from app.schemas.admission import (
    AcademicYearCreate, AcademicYearOut, AcademicYearUpdate,
    AdmissionCategoryCreate, AdmissionCategoryOut, AdmissionCategoryUpdate,
    AssignStudentsRequest, AssignStudentsResult, YearStudentOut,
)
from app.schemas.user import UserOut
from app.services import admissions as service

router = APIRouter(prefix="/admin/tuition/admissions", tags=["Tuition - Admissions"])

TUITION = Program.TUITION.value


def _tuition_programs(payload) -> list[Program]:
    """
    The program list a record created here gets.

    Omitted from the body, it is tuition only - the schema's own default is LMS, which is
    right for `/admissions` and wrong here, so "omitted" is read from `model_fields_set`
    rather than from the value. Given explicitly, TUITION has to be in it: this is the
    tuition board, and a record created from it that is not a tuition record is a mistake in
    the request, not a re-scope.
    """
    programs = payload.programs if "programs" in payload.model_fields_set else None
    if not programs:
        return [Program.TUITION]
    if Program.TUITION not in programs:
        raise HTTPException(
            status_code=400,
            detail="Records created from the tuition admissions board must include TUITION "
                   "in 'programs'. Use /admissions for school-only records.",
        )
    return programs


def _assert_stays_tuition(programs) -> None:
    """An update may add LMS to a tuition record; it may not take TUITION away here."""
    if programs is not None and Program.TUITION not in programs:
        raise HTTPException(
            status_code=400,
            detail="This would remove the record from the tuition board. Re-scope it from "
                   "/admissions instead, where both products are visible.",
        )


def _require_tuition_year(year_id) -> dict:
    year = service.require_year(year_id)
    if not service.belongs_to_program(year, TUITION):
        raise HTTPException(status_code=404, detail="Academic year not found")
    return year


def _require_tuition_category(category_id) -> dict:
    category = service.require_category(category_id)
    if not service.belongs_to_program(category, TUITION):
        raise HTTPException(status_code=404, detail="Admission category not found")
    return category


# ---------------------------------------------------------------------------------------
# Session years
# ---------------------------------------------------------------------------------------

@router.post("/years", response_model=AcademicYearOut, status_code=status.HTTP_201_CREATED)
def create_year(payload: AcademicYearCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a tuition session year - a batch, an intake, a calendar year.

    `programs` may be left out; it defaults to TUITION alone. Setting `is_current` clears
    the flag on every other **tuition** year and leaves the school's current year untouched:
    the two products keep their own calendars.
    """
    payload.programs = _tuition_programs(payload)
    return AcademicYearOut(**service.present_year(service.create_year(payload, admin.id)))


@router.get("/years", response_model=List[AcademicYearOut])
def list_years(
    with_counts: bool = Query(
        False, description="Include student and category counts. One extra query per row."
    ),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Every tuition session year, newest first."""
    return [
        AcademicYearOut(**service.present_year(y, with_counts=with_counts))
        for y in service.list_years(TUITION)
    ]


@router.get("/years/current", response_model=Optional[AcademicYearOut])
def get_current_year(_: UserOut = Depends(require_tuition_user)):
    """
    The tuition year everything defaults to.

    Readable by anyone in the tuition programme, so the header can show the batch name for
    students and teachers too. `null` rather than 404 when no tuition year exists yet - a
    programme that has not set this up is not in an error state.
    """
    year = service.current_year(TUITION)
    return AcademicYearOut(**service.present_year(year)) if year else None


@router.get("/years/{year_id}", response_model=AcademicYearOut)
def get_year(year_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One tuition session year, with its counts. 404 for a school-only year."""
    year = _require_tuition_year(year_id)
    return AcademicYearOut(**service.present_year(year, with_counts=True))


@router.put("/years/{year_id}", response_model=AcademicYearOut)
def update_year(
    year_id: int, payload: AcademicYearUpdate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a tuition year. Partial: omitted fields are left unchanged.

    Setting `is_current` rolls the tuition programme over to this year. Dropping TUITION from
    `programs` is refused here - see the module docstring.
    """
    year = _require_tuition_year(year_id)
    _assert_stays_tuition(payload.programs)
    return AcademicYearOut(**service.present_year(service.update_year(year, payload, admin.id)))


@router.delete("/years/{year_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_year(year_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete a tuition year.

    Refused while students are admitted into it or categories are scoped to it; the error
    names what is in the way. A batch that is simply over should be set to CLOSED.
    """
    service.delete_year(_require_tuition_year(year_id))


# ---------------------------------------------------------------------------------------
# Admission categories
# ---------------------------------------------------------------------------------------

@router.post("/categories", response_model=AdmissionCategoryOut,
             status_code=status.HTTP_201_CREATED)
def create_category(
    payload: AdmissionCategoryCreate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Create a tuition admission category - "Regular", "Sibling", "Scholarship".

    `programs` defaults to TUITION alone. A category may be scoped to one tuition year with
    `academic_year_id`, or left standing so every batch inherits it.

    `default_discount_percent` and `waives_admission_charge` are read by the tuition fee
    engine when a student under this category is invoiced.
    """
    payload.programs = _tuition_programs(payload)
    if payload.academic_year_id is not None:
        _require_tuition_year(payload.academic_year_id)
    return AdmissionCategoryOut(
        **service.present_category(service.create_category(payload, admin.id))
    )


@router.get("/categories", response_model=List[AdmissionCategoryOut])
def list_categories(
    academic_year_id: Optional[int] = Query(
        None, description="Categories applying to this tuition year, standing ones included."
    ),
    include_inactive: bool = Query(False),
    with_counts: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Tuition admission categories, in display order."""
    categories = service.list_categories(
        program=TUITION,
        academic_year_id=academic_year_id,
        include_inactive=include_inactive,
    )
    return [
        AdmissionCategoryOut(**service.present_category(c, with_counts=with_counts))
        for c in categories
    ]


@router.get("/categories/{category_id}", response_model=AdmissionCategoryOut)
def get_category(category_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One tuition category, with its student count."""
    category = _require_tuition_category(category_id)
    return AdmissionCategoryOut(**service.present_category(category, with_counts=True))


@router.put("/categories/{category_id}", response_model=AdmissionCategoryOut)
def update_category(
    category_id: int, payload: AdmissionCategoryUpdate,
    admin: UserOut = Depends(require_admin),
):
    """[Admin Only] Update a tuition category. Partial: omitted fields are left unchanged."""
    category = _require_tuition_category(category_id)
    _assert_stays_tuition(payload.programs)
    if payload.academic_year_id is not None:
        _require_tuition_year(payload.academic_year_id)
    return AdmissionCategoryOut(
        **service.present_category(service.update_category(category, payload, admin.id))
    )


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete a tuition category.

    Refused while any student holds it. Set `is_active` to false instead, so every student
    already admitted under it keeps a resolvable record of why they pay what they pay.
    """
    service.delete_category(_require_tuition_category(category_id))


# ---------------------------------------------------------------------------------------
# Mapping tuition students into a year
# ---------------------------------------------------------------------------------------

@router.get("/years/{year_id}/students", response_model=List[YearStudentOut])
def year_roster(
    year_id: int,
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Tuition students mapped into this year, by name.

    Only accounts with tuition access are listed, so a year shared with the school does not
    show the school's pupils here. `class_id` and `class_name` are null for a tuition-only
    student - tuition has enrollments, not classes.
    """
    _require_tuition_year(year_id)
    return [
        YearStudentOut(**row)
        for row in service.students_in_year_detailed(
            year_id, include_inactive=include_inactive, program=TUITION
        )
    ]


@router.get("/students/unassigned", response_model=List[YearStudentOut])
def unassigned_students(
    include_inactive: bool = Query(False), _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Tuition students carrying no session year at all.

    Every tuition student created before a tuition year existed is in here. Feed these ids
    into `POST /admin/tuition/admissions/years/{year_id}/students`.
    """
    return [
        YearStudentOut(**row)
        for row in service.unassigned_students(include_inactive, program=TUITION)
    ]


@router.post("/years/{year_id}/students", response_model=AssignStudentsResult)
def assign_students(
    year_id: int, payload: AssignStudentsRequest, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Map a list of tuition students into this year, in one call.

    This is the tuition rollover as well as the first-time mapping: there are no classes to
    promote through, so moving a batch into the next year is just this call with last year's
    roster. `dry_run: true` reports what would change without writing anything.
    """
    _require_tuition_year(year_id)
    return AssignStudentsResult(**service.assign_students(
        year_id, payload.student_ids, admin.id, payload.dry_run
    ))
