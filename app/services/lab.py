"""
The virtual lab: experiments teachers set for a class, and students' finished runs.

The chemistry runs in the browser (the frontend's lab engine); the server keeps what a
teacher needs to see and what a student earns. Library experiments are referred to by id
and their content lives with the engine; a teacher's own experiment is stored whole.
Completing an experiment pays arena XP and coins once per experiment (or assignment), so
replaying a favourite does not farm rewards.
"""
import json
from datetime import datetime

from fastapi import HTTPException

from app.core.firebase import (
    firestore_classes, firestore_lab_assignments, firestore_lab_attempts,
    firestore_student_enrollments, firestore_users, require_document,
)
from app.services.arena import catalog
from app.services.arena.leaderboard import add_points
from app.services.arena.profiles import get_profile, save_profile, student_class_id
from app.services.arena.rewards import award_collection_badges
from app.services.permissions import led_class_ids

# Snapshot of the library's titles so lists render without the engine. Kept small on purpose.
LIBRARY = {
    "neutralisation": ("Neutralising an acid", "🧪", "Acids, bases and salts"),
    "hydrogen": ("Metals and acids: the hydrogen pop", "💥", "Metals"),
    "co2_limewater": ("Carbon dioxide and limewater", "🫧", "Gases"),
    "oxygen_catalyst": ("A catalyst and oxygen", "✨", "Rates of reaction"),
    "precipitates": ("Testing for ions with precipitates", "🔬", "Chemical analysis"),
    "displacement": ("Displacement: iron in copper sulfate", "🔩", "Reactivity series"),
    "flame_tests": ("Flame tests", "🔥", "Chemical analysis"),
    "energy_changes": ("Hot and cold dissolving", "🌡️", "Energy changes"),
    "starch": ("Testing for starch", "🍞", "Food tests"),
    "thermal_decomposition": ("Heating copper carbonate", "♨️", "Types of reaction"),
    "elephant": ("Elephant toothpaste", "🐘", "Catalysts"),
    "water_test": ("Testing for water", "💧", "Chemical tests"),
}

MAX_CUSTOM_BYTES = 60_000


def _now() -> str:
    return datetime.utcnow().isoformat()


def _is_admin(user) -> bool:
    return getattr(getattr(user, "role", None), "value", getattr(user, "role", None)) == "ADMIN"


def _teacher_classes(user) -> set[int] | None:
    if _is_admin(user):
        return None
    from app.core.firebase import firestore_teacher_mappings

    taught = {m.get("class_id") for m in firestore_teacher_mappings.query_documents("teacher_id", "==", user.id)}
    return {c for c in taught | led_class_ids(user) if c is not None}


def assert_class_access(user, class_id: int) -> None:
    if not firestore_classes.get_document(str(class_id)):
        raise HTTPException(status_code=404, detail="Class not found")
    allowed = _teacher_classes(user)
    if allowed is not None and class_id not in allowed:
        raise HTTPException(status_code=403, detail="You do not teach this class.")


def _assignment_out(a: dict, attempts: list[dict] | None = None, student_id: int | None = None) -> dict:
    lib = LIBRARY.get(a.get("library_id") or "")
    custom = a.get("experiment") or None
    title = a.get("title") or (custom or {}).get("title") or (lib[0] if lib else "Experiment")
    out = {
        "id": int(a["id"]),
        "class_id": a.get("class_id"),
        "library_id": a.get("library_id"),
        "experiment": custom,
        "experiment_id": a.get("library_id") or f"custom-{a['id']}",
        "title": title,
        "emoji": (custom or {}).get("emoji") or (lib[1] if lib else "🧪"),
        "topic": (custom or {}).get("topic") or (lib[2] if lib else "Custom experiment"),
        "instructions": a.get("instructions"),
        "due_at": a.get("due_at"),
        "is_active": a.get("is_active", True),
        "teacher_id": a.get("teacher_id"),
        "created_at": a.get("created_at"),
    }
    if attempts is not None:
        mine = [t for t in attempts if student_id is None or t.get("student_id") == student_id]
        out["attempts"] = len(mine)
        best = max(mine, key=lambda t: (t.get("score") or 0) / max(1, t.get("total") or 1), default=None)
        out["best"] = {"score": best.get("score"), "total": best.get("total"), "completed_at": best.get("completed_at")} if best else None
    return out


# ---------------------------------------------------------------- teachers

def list_assignments(user, class_id: int | None) -> list[dict]:
    if class_id is not None:
        assert_class_access(user, class_id)
        rows = firestore_lab_assignments.query_documents("class_id", "==", class_id)
    else:
        allowed = _teacher_classes(user)
        rows = firestore_lab_assignments.list_all()
        if allowed is not None:
            rows = [r for r in rows if r.get("class_id") in allowed]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    out = []
    for r in rows:
        attempts = firestore_lab_attempts.query_documents("assignment_id", "==", int(r["id"]))
        item = _assignment_out(r)
        item["completed_by"] = len({t.get("student_id") for t in attempts})
        item["class_size"] = len(firestore_student_enrollments.query_documents("class_id", "==", r.get("class_id")))
        out.append(item)
    return out


def _clean_experiment(experiment: dict | None) -> dict | None:
    if experiment is None:
        return None
    if len(json.dumps(experiment)) > MAX_CUSTOM_BYTES:
        raise HTTPException(status_code=413, detail="That experiment is too large.")
    return experiment


def create_assignment(user, body) -> dict:
    assert_class_access(user, body.class_id)
    if body.library_id and body.library_id not in LIBRARY:
        raise HTTPException(status_code=404, detail="That library experiment does not exist.")
    assignment_id = firestore_lab_assignments.get_next_numeric_id()
    data = {
        "class_id": body.class_id,
        "library_id": body.library_id,
        "experiment": _clean_experiment(body.experiment),
        "title": (body.title or "").strip() or None,
        "instructions": (body.instructions or "").strip() or None,
        "due_at": body.due_at.replace(tzinfo=None).isoformat() if body.due_at else None,
        "is_active": body.is_active,
        "teacher_id": user.id,
        "created_at": _now(),
    }
    firestore_lab_assignments.add_document(str(assignment_id), data)
    return _assignment_out({**data, "id": assignment_id})


def _require_assignment(user, assignment_id: int) -> dict:
    a = require_document(firestore_lab_assignments, assignment_id, "Lab assignment")
    assert_class_access(user, a.get("class_id"))
    return a


def update_assignment(user, assignment_id: int, body) -> dict:
    _require_assignment(user, assignment_id)
    changes = body.model_dump(exclude_unset=True)
    if "experiment" in changes:
        changes["experiment"] = _clean_experiment(changes["experiment"])
    if isinstance(changes.get("due_at"), datetime):
        changes["due_at"] = changes["due_at"].replace(tzinfo=None).isoformat()
    if not changes:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    changes["updated_at"] = _now()
    firestore_lab_assignments.add_document(str(assignment_id), changes)
    return _assignment_out(firestore_lab_assignments.get_document(str(assignment_id)))


def delete_assignment(user, assignment_id: int) -> None:
    _require_assignment(user, assignment_id)
    firestore_lab_assignments.delete_document(str(assignment_id))


def assignment_results(user, assignment_id: int) -> dict:
    """Every student in the class with their best run of this experiment."""
    a = _require_assignment(user, assignment_id)
    attempts = firestore_lab_attempts.query_documents("assignment_id", "==", int(assignment_id))
    by_student: dict[int, list[dict]] = {}
    for t in attempts:
        by_student.setdefault(int(t.get("student_id")), []).append(t)
    students = []
    for e in firestore_student_enrollments.query_documents("class_id", "==", a.get("class_id")):
        sid = e.get("student_id")
        if sid is None:
            continue
        user_doc = firestore_users.get_document(str(sid))
        if not user_doc:
            continue
        runs = sorted(by_student.get(int(sid), []), key=lambda t: t.get("completed_at") or "", reverse=True)
        best = max(runs, key=lambda t: (t.get("score") or 0) / max(1, t.get("total") or 1), default=None)
        students.append({
            "student_id": int(sid),
            "full_name": user_doc.get("full_name"),
            "attempts": len(runs),
            "best": {"score": best.get("score"), "total": best.get("total")} if best else None,
            "last_completed_at": runs[0].get("completed_at") if runs else None,
            "observations": runs[0].get("observations") if runs else [],
        })
    students.sort(key=lambda s: (s["best"] is None, s["full_name"] or ""))
    return {"assignment": _assignment_out(a), "students": students}


# ---------------------------------------------------------------- students

def my_assignments(user) -> list[dict]:
    class_id = student_class_id(user.id)
    if class_id is None:
        return []
    rows = [r for r in firestore_lab_assignments.query_documents("class_id", "==", class_id) if r.get("is_active", True)]
    mine = firestore_lab_attempts.query_documents("student_id", "==", user.id)
    rows.sort(key=lambda r: (r.get("due_at") or "9999", r.get("created_at") or ""))
    return [_assignment_out(r, [t for t in mine if t.get("assignment_id") == int(r["id"])], user.id) for r in rows]


def record_attempt(user, body) -> dict:
    """Saves a finished run; the first completion of an experiment pays XP and coins."""
    class_id = student_class_id(user.id)
    if body.assignment_id is not None:
        a = require_document(firestore_lab_assignments, body.assignment_id, "Lab assignment")
        if a.get("class_id") != class_id:
            raise HTTPException(status_code=403, detail="That experiment was set for another class.")
    previous = firestore_lab_attempts.query_documents("student_id", "==", user.id)
    key = f"a{body.assignment_id}" if body.assignment_id is not None else f"x{body.experiment_id}"
    first_time = not any(
        (f"a{t.get('assignment_id')}" if t.get("assignment_id") is not None else f"x{t.get('experiment_id')}") == key
        for t in previous
    )

    total = max(body.total, 0)
    ratio = (body.score / total) if total else 1.0
    xp = int(round(30 + 40 * ratio)) if first_time else 0
    coins = int(round(15 + 25 * ratio)) if first_time else 0

    attempt_id = firestore_lab_attempts.get_next_numeric_id()
    firestore_lab_attempts.add_document(str(attempt_id), {
        "student_id": user.id,
        "class_id": class_id,
        "assignment_id": body.assignment_id,
        "experiment_id": body.experiment_id,
        "score": body.score,
        "total": total,
        "observations": [o[:400] for o in body.observations][:80],
        "seconds": body.seconds,
        "xp": xp,
        "coins": coins,
        "completed_at": _now(),
    })

    new_badges = []
    if first_time:
        profile = get_profile(user.id)
        level_before = catalog.level_for_xp(int(profile.get("xp") or 0))
        profile["xp"] = int(profile.get("xp") or 0) + xp
        profile["coins"] = int(profile.get("coins") or 0) + coins
        profile["experiments"] = int(profile.get("experiments") or 0) + 1
        save_profile(user.id, {"xp": profile["xp"], "coins": profile["coins"], "experiments": profile["experiments"]})
        if class_id is not None:
            add_points([f"class:{class_id}"], user.id, class_id, xp, False)
        new_badges = award_collection_badges(user.id, profile)
        level_after = catalog.level_for_xp(profile["xp"])
    else:
        level_before = level_after = None
    return {
        "id": attempt_id,
        "first_time": first_time,
        "xp": xp,
        "coins": coins,
        "new_badges": new_badges,
        "level_before": level_before,
        "level_after": level_after,
    }


def my_attempts(user) -> list[dict]:
    rows = firestore_lab_attempts.query_documents("student_id", "==", user.id)
    rows.sort(key=lambda r: r.get("completed_at") or "", reverse=True)
    return [{k: r.get(k) for k in ("id", "experiment_id", "assignment_id", "score", "total", "xp", "coins", "completed_at")} for r in rows[:50]]
