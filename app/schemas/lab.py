"""Request bodies for the virtual lab."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class LabAssignmentIn(BaseModel):
    """
    An experiment set for a class: either one from the built-in library (`library_id`) or
    the teacher's own (`experiment`, the same shape the lab uses: title, aim, steps…).
    """
    class_id: int
    library_id: str | None = Field(None, max_length=64)
    experiment: dict[str, Any] | None = None
    title: str | None = Field(None, max_length=160)
    instructions: str | None = Field(None, max_length=2000)
    due_at: datetime | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _one_source(self) -> "LabAssignmentIn":
        if not self.library_id and not self.experiment:
            raise ValueError("Choose a library experiment or build your own.")
        if self.experiment is not None:
            steps = self.experiment.get("steps")
            if not self.experiment.get("title") or not isinstance(steps, list) or not steps:
                raise ValueError("A custom experiment needs a title and at least one step.")
            if len(steps) > 30:
                raise ValueError("Keep an experiment to 30 steps or fewer.")
        return self


class LabAssignmentUpdate(BaseModel):
    title: str | None = Field(None, max_length=160)
    instructions: str | None = Field(None, max_length=2000)
    experiment: dict[str, Any] | None = None
    due_at: datetime | None = None
    is_active: bool | None = None


class LabAttemptIn(BaseModel):
    """A finished run: the experiment, the quiz score and what the student saw."""
    experiment_id: str = Field(..., min_length=1, max_length=64)
    assignment_id: int | None = None
    score: int = Field(0, ge=0, le=100)
    total: int = Field(0, ge=0, le=100)
    observations: list[str] = Field(default_factory=list, max_length=80)
    seconds: int | None = Field(None, ge=0, le=6 * 3600)
