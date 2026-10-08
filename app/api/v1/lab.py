"""
The virtual lab. Students run experiments in the browser; teachers set experiments for a
class and see the results. See app/services/lab.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.v1.dependencies import require_student, require_teacher
from app.schemas.lab import LabAssignmentIn, LabAssignmentUpdate, LabAttemptIn
from app.schemas.user import UserOut
from app.services import lab

router = APIRouter(prefix="/lab", tags=["Virtual lab"])
manage_router = APIRouter(prefix="/lab/manage", tags=["Virtual lab (manage)"])


@router.get("/assignments")
def my_lab_assignments(current_user: UserOut = Depends(require_student)):
    """[Student] Experiments set for my class, with my best result on each."""
    return lab.my_assignments(current_user)


@router.post("/attempts", status_code=status.HTTP_201_CREATED)
def record_lab_attempt(body: LabAttemptIn, current_user: UserOut = Depends(require_student)):
    """[Student] Save a finished experiment; the first completion pays arena XP and coins."""
    return lab.record_attempt(current_user, body)


@router.get("/attempts")
def my_lab_attempts(current_user: UserOut = Depends(require_student)):
    """[Student] My last 50 finished experiments."""
    return lab.my_attempts(current_user)


@manage_router.get("/assignments")
def list_lab_assignments(class_id: Optional[int] = Query(None), current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Experiments set for my classes (or one class)."""
    return lab.list_assignments(current_user, class_id)


@manage_router.post("/assignments", status_code=status.HTTP_201_CREATED)
def create_lab_assignment(body: LabAssignmentIn, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Set a library or custom experiment for a class."""
    return lab.create_assignment(current_user, body)


@manage_router.put("/assignments/{assignment_id}")
def update_lab_assignment(assignment_id: int, body: LabAssignmentUpdate, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Edit, reschedule, or switch an assignment on or off."""
    return lab.update_assignment(current_user, assignment_id, body)


@manage_router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lab_assignment(assignment_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Remove an assignment. Students' past results are kept."""
    lab.delete_assignment(current_user, assignment_id)
    return None


@manage_router.get("/assignments/{assignment_id}/results")
def lab_assignment_results(assignment_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Every student in the class with their best run and latest observations."""
    return lab.assignment_results(current_user, assignment_id)
