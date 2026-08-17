"""
Class-teacher authority.

An ordinary teacher only ever sees and edits what they filed themselves: every list endpoint
in the teacher router filters on `teacher_id == me`, and `assert_owner` refuses writes to
anyone else's record. That is correct for a subject teacher and useless for a class teacher,
whose job is precisely to answer for a class as a whole - including the periods taught by
other people.

This module is the single place that resolves the difference. Two rules, applied everywhere:

* **The mapping is the authority.** `class_teacher_mappings` names which classes a teacher
  leads. Authority is always per class, never global - a teacher can lead 9-A while remaining
  an ordinary subject teacher in 10-B, and nothing here grants power over a class they do not
  lead.
* **The role is only the label.** `UserRole.CLASS_TEACHER` exists so the frontend can gate
  navigation straight from the Firebase claim without an extra call. It is maintained *from*
  the mappings by the admin router (see `sync_class_teacher_role`), so the two cannot drift
  into the state where a role says CLASS_TEACHER but no mapping says which class.

Admins pass every check here, matching `assert_owner` and the rest of the codebase.
"""

import logging
from typing import Any, Iterable

from fastapi import HTTPException

from app.core.concurrency import run_parallel
from app.core.enums import UserRole
from app.core.firebase import (
    FirestoreService,
    firestore_class_teacher_mappings,
    firestore_classes,
    require_document,
)

logger = logging.getLogger("permissions")


def _is_admin(user: Any) -> bool:
    return getattr(user, "role", None) == UserRole.ADMIN


def led_class_ids(user: Any) -> set[int]:
    """
    The ids of every class this user is the class teacher of.

    Empty for a subject teacher, a student, and - deliberately - for an admin: an admin's
    reach is unlimited and is expressed by the `_is_admin` short-circuits below rather than
    by pretending they lead every class, which would mean listing the whole `class_rooms`
    collection on every request.
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return set()

    mappings = firestore_class_teacher_mappings.query_documents("teacher_id", "==", user_id)
    return {m["class_id"] for m in mappings if m.get("class_id") is not None}


def teachers_of_class(class_id: int) -> list[int]:
    """The user ids of every class teacher assigned to a class."""
    if class_id is None:
        return []

    mappings = firestore_class_teacher_mappings.query_documents("class_id", "==", class_id)
    return [m["teacher_id"] for m in mappings if m.get("teacher_id") is not None]


def is_class_teacher_of(user: Any, class_id: Any) -> bool:
    """Whether the user may act over the whole of `class_id`. Admins always may."""
    if class_id is None:
        return False
    if _is_admin(user):
        return True
    return class_id in led_class_ids(user)


def assert_leads_class(user: Any, class_id: int, action: str = "act on this class") -> None:
    """
    Guards a class-scoped endpoint, 404ing an unknown class before 403ing an unauthorized one.

    The order matters: reporting "you are not the class teacher of class 999" for a class that
    was never created sends the caller looking for a permission problem that does not exist.
    """
    require_document(firestore_classes, class_id, "ClassRoom")

    if not is_class_teacher_of(user, class_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"You are not the class teacher of class {class_id}, so you cannot {action}. "
                "Ask an administrator to assign you, or use the endpoints scoped to your own records."
            ),
        )


def visible_class_ids(user: Any, requested: int | None = None) -> set[int] | None:
    """
    The classes a class-scoped listing should cover.

    `None` means "no class restriction" (an admin, who sees everything). A `requested` class
    the user does not lead raises rather than silently returning nothing, because an empty
    list reads as "there is no data" when the truth is "you may not see it".
    """
    if requested is not None:
        if not is_class_teacher_of(user, requested):
            raise HTTPException(
                status_code=403,
                detail=f"You are not the class teacher of class {requested}.",
            )
        return {requested}

    if _is_admin(user):
        return None
    return led_class_ids(user)


def owns_or_leads(record: dict, user: Any, led: set[int] | None = None) -> bool:
    """
    Whether the user may see or modify one record.

    Pass `led` when checking a batch, so the mapping lookup happens once instead of per record.
    """
    if _is_admin(user):
        return True
    if record.get("teacher_id") == getattr(user, "id", None):
        return True

    scope = led_class_ids(user) if led is None else led
    return record.get("class_id") in scope


def visible_records(
    service: FirestoreService,
    user: Any,
    class_id: int | None = None,
) -> list[dict]:
    """
    Every record in `service` the user is entitled to see: their own, plus everything filed
    against the classes they lead, whoever entered it.

    The teacher's own records and each led class are independent queries, so they are issued
    concurrently - a class teacher leading two classes pays one network wait rather than
    three. Results are merged by document id because a record the class teacher filed against
    their own class comes back from both queries.

    `class_id` narrows the result to one class and is verified against the caller's authority
    first, so it can never widen what they see.
    """
    user_id = getattr(user, "id", None)

    if _is_admin(user):
        records = (
            service.query_documents("class_id", "==", class_id)
            if class_id is not None else service.list_all()
        )
        return records

    led = led_class_ids(user)
    if class_id is not None and class_id not in led:
        # Not a class they lead, so they only ever see their own records within it.
        return [
            r for r in service.query_documents("teacher_id", "==", user_id)
            if r.get("class_id") == class_id
        ]

    scope = {class_id} if class_id is not None else led

    queries: dict[str, Any] = {
        "own": (lambda: service.query_documents("teacher_id", "==", user_id))
    }
    for led_id in scope:
        queries[f"class:{led_id}"] = (lambda c=led_id: service.query_documents("class_id", "==", c))

    merged: dict[Any, dict] = {}
    for records in run_parallel(queries).values():
        for record in records or []:
            merged[record["id"]] = record

    if class_id is not None:
        return [r for r in merged.values() if r.get("class_id") == class_id]
    return list(merged.values())


def filter_visible(records: Iterable[dict], user: Any) -> list[dict]:
    """
    Drops records the user may not see from an already-fetched list.
    The mapping lookup runs once for the whole batch.
    """
    if _is_admin(user):
        return list(records)

    led = led_class_ids(user)
    return [r for r in records if owns_or_leads(r, user, led=led)]
