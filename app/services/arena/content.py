"""
Syllabus chapters, the question bank and global challenges: who may write them, and how.

Authority follows the timetable's own source of truth. A teacher manages the class-subject
pairs they are mapped to teach (`teacher_subject_class_mappings`), plus every subject of a
class they lead as class teacher; an admin manages everything. Global questions and
challenges are admin-only - they reach every student in the school.
"""
import copy
import threading
import time
from datetime import datetime

from fastapi import HTTPException

from app.core.concurrency import run_parallel

from app.core.enums import UserRole
from app.core.firebase import (
    firestore_arena_challenges, firestore_classes, firestore_question_bank,
    firestore_subjects, firestore_syllabus_chapters, firestore_teacher_mappings,
    require_document,
)
from app.services.arena.catalog import CATEGORY_IDS
from app.services.permissions import led_class_ids

SCOPE_CLASS = "CLASS"
SCOPE_GLOBAL = "GLOBAL"
# Fewer than this many active questions and a chapter is not offered for battle.
MIN_BATTLE_QUESTIONS = 3


def _now() -> str:
    return datetime.utcnow().isoformat()


# The arena home is polled every few seconds by every student on it, and what it lists -
# playable chapters, open challenges - changes when a teacher edits, not per second. A short
# per-process memo keeps those polls to the reads that do change (invites, the live battle).
_MEMO_SECONDS = 20.0
_memo: dict = {}
_memo_lock = threading.Lock()


def _memoized(key, compute):
    now = time.monotonic()
    with _memo_lock:
        hit = _memo.get(key)
        if hit and now - hit[0] < _MEMO_SECONDS:
            return copy.deepcopy(hit[1])
    value = compute()
    with _memo_lock:
        _memo[key] = (now, value)
    return copy.deepcopy(value)


def forget_memo() -> None:
    """Called after every content write, so the person editing sees their change at once."""
    with _memo_lock:
        _memo.clear()


def is_admin(user) -> bool:
    return getattr(user, "role", None) == UserRole.ADMIN


def assert_admin(user) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Only an administrator can change global questions and challenges.")


# ---------------------------------------------------------------------------- authority

def manageable_pairs(user) -> list[dict]:
    """
    Every class-subject pair this user may write chapters and questions for, named.

    Admins get every pair that appears on the timetable mappings; an admin can still create
    content for any other class and subject (the picker offers all of them).
    """
    if is_admin(user):
        mappings = firestore_teacher_mappings.list_all()
    else:
        mappings = list(firestore_teacher_mappings.query_documents("teacher_id", "==", user.id))
        led = led_class_ids(user)
        if led:
            for m in firestore_teacher_mappings.list_all():
                if m.get("class_id") in led:
                    mappings.append(m)

    pairs: dict[tuple[int, int], dict] = {}
    for m in mappings:
        class_id, subject_id = m.get("class_id"), m.get("subject_id")
        if class_id is None or subject_id is None or (class_id, subject_id) in pairs:
            continue
        class_doc = firestore_classes.get_document(str(class_id))
        subject_doc = firestore_subjects.get_document(str(subject_id))
        if not class_doc or not subject_doc:
            continue
        pairs[(class_id, subject_id)] = {
            "class_id": class_id, "class_name": class_doc.get("name"),
            "subject_id": subject_id, "subject_name": subject_doc.get("name"),
        }
    return sorted(pairs.values(), key=lambda p: ((p["class_name"] or ""), (p["subject_name"] or "")))


def can_manage(user, class_id: int, subject_id: int) -> bool:
    if is_admin(user):
        return True
    if class_id in led_class_ids(user):
        return True
    return any(
        m.get("class_id") == class_id and m.get("subject_id") == subject_id
        for m in firestore_teacher_mappings.query_documents("teacher_id", "==", user.id)
    )


def assert_can_manage(user, class_id: int, subject_id: int) -> None:
    if not firestore_classes.get_document(str(class_id)):
        raise HTTPException(status_code=404, detail=f"ClassRoom {class_id} not found")
    if not firestore_subjects.get_document(str(subject_id)):
        raise HTTPException(status_code=404, detail=f"Subject {subject_id} not found")
    if not can_manage(user, class_id, subject_id):
        raise HTTPException(status_code=403, detail="You do not teach this subject in this class.")


# ----------------------------------------------------------------------------- chapters

def _chapters_of(class_id: int, subject_id: int | None = None) -> list[dict]:
    rows = firestore_syllabus_chapters.query_documents("class_id", "==", class_id)
    if subject_id is not None:
        rows = [r for r in rows if r.get("subject_id") == subject_id]
    return sorted(rows, key=lambda r: (r.get("order") or 0, r.get("created_at") or ""))


def _class_questions(class_id: int) -> list[dict]:
    return firestore_question_bank.query_documents("class_id", "==", class_id)


def chapter_out(chapter: dict, counts: dict[int, dict] | None = None) -> dict:
    tally = (counts or {}).get(int(chapter["id"]), {"total": 0, "active": 0})
    return {
        "id": int(chapter["id"]),
        "class_id": chapter.get("class_id"),
        "subject_id": chapter.get("subject_id"),
        "title": chapter.get("title"),
        "description": chapter.get("description"),
        "order": chapter.get("order") or 0,
        "is_active": chapter.get("is_active", True),
        "question_count": tally["total"],
        "active_question_count": tally["active"],
        "created_at": chapter.get("created_at"),
        "updated_at": chapter.get("updated_at"),
    }


def question_counts(questions: list[dict]) -> dict[int, dict]:
    counts: dict[int, dict] = {}
    for q in questions:
        chapter_id = q.get("chapter_id")
        if chapter_id is None:
            continue
        tally = counts.setdefault(int(chapter_id), {"total": 0, "active": 0})
        tally["total"] += 1
        if q.get("is_active", True):
            tally["active"] += 1
    return counts


def list_chapters(class_id: int, subject_id: int) -> list[dict]:
    counts = question_counts(_class_questions(class_id))
    return [chapter_out(c, counts) for c in _chapters_of(class_id, subject_id)]


def create_chapter(user, body) -> dict:
    forget_memo()
    assert_can_manage(user, body.class_id, body.subject_id)
    existing = _chapters_of(body.class_id, body.subject_id)
    chapter_id = firestore_syllabus_chapters.get_next_numeric_id()
    data = {
        "class_id": body.class_id,
        "subject_id": body.subject_id,
        "title": body.title.strip(),
        "description": (body.description or "").strip() or None,
        "order": (max((c.get("order") or 0) for c in existing) + 1) if existing else 1,
        "is_active": True,
        "created_by": user.id,
        "created_at": _now(),
        "updated_at": _now(),
    }
    firestore_syllabus_chapters.add_document(str(chapter_id), data)
    return chapter_out({**data, "id": chapter_id})


def _require_chapter(user, chapter_id: int) -> dict:
    chapter = require_document(firestore_syllabus_chapters, chapter_id, "Chapter")
    if not can_manage(user, chapter.get("class_id"), chapter.get("subject_id")):
        raise HTTPException(status_code=403, detail="You do not teach this subject in this class.")
    return chapter


def update_chapter(user, chapter_id: int, body) -> dict:
    forget_memo()
    _require_chapter(user, chapter_id)
    updated = body.model_dump(exclude_unset=True)
    if "title" in updated and updated["title"]:
        updated["title"] = updated["title"].strip()
    if "description" in updated:
        updated["description"] = (updated["description"] or "").strip() or None
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    updated["updated_at"] = _now()
    firestore_syllabus_chapters.add_document(str(chapter_id), updated)
    chapter = firestore_syllabus_chapters.get_document(str(chapter_id))
    return chapter_out(chapter, question_counts(_class_questions(chapter.get("class_id"))))


def delete_chapter(user, chapter_id: int) -> None:
    forget_memo()
    """Deletes the chapter and its questions; battles already played keep their copies."""
    chapter = _require_chapter(user, chapter_id)
    for q in firestore_question_bank.query_documents("chapter_id", "==", int(chapter_id)):
        firestore_question_bank.delete_document(str(q["id"]))
    firestore_syllabus_chapters.delete_document(str(chapter_id))
    # Close the gap so the order stays 1..n.
    for index, c in enumerate(_chapters_of(chapter["class_id"], chapter["subject_id"]), start=1):
        if c.get("order") != index:
            firestore_syllabus_chapters.add_document(str(c["id"]), {"order": index})


def reorder_chapters(user, body) -> list[dict]:
    forget_memo()
    assert_can_manage(user, body.class_id, body.subject_id)
    current = {int(c["id"]): c for c in _chapters_of(body.class_id, body.subject_id)}
    if set(body.chapter_ids) != set(current):
        raise HTTPException(status_code=400, detail="List every chapter of this subject exactly once.")
    for index, chapter_id in enumerate(body.chapter_ids, start=1):
        if current[chapter_id].get("order") != index:
            firestore_syllabus_chapters.add_document(str(chapter_id), {"order": index, "updated_at": _now()})
    return list_chapters(body.class_id, body.subject_id)


def copy_chapters(user, body) -> list[dict]:
    forget_memo()
    """Appends the source pair's chapters (and questions) to the target class's list."""
    if body.from_class_id == body.to_class_id:
        raise HTTPException(status_code=400, detail="Choose a different class to copy into.")
    assert_can_manage(user, body.from_class_id, body.subject_id)
    assert_can_manage(user, body.to_class_id, body.subject_id)
    source = _chapters_of(body.from_class_id, body.subject_id)
    if not source:
        raise HTTPException(status_code=404, detail="That class has no chapters for this subject yet.")
    target = _chapters_of(body.to_class_id, body.subject_id)
    next_order = (max((c.get("order") or 0) for c in target) + 1) if target else 1
    questions = _class_questions(body.from_class_id) if body.include_questions else []

    for chapter in source:
        new_id = firestore_syllabus_chapters.get_next_numeric_id()
        firestore_syllabus_chapters.add_document(str(new_id), {
            "class_id": body.to_class_id, "subject_id": body.subject_id,
            "title": chapter.get("title"), "description": chapter.get("description"),
            "order": next_order, "is_active": chapter.get("is_active", True),
            "created_by": user.id, "created_at": _now(), "updated_at": _now(),
            "copied_from": int(chapter["id"]),
        })
        next_order += 1
        for q in questions:
            if q.get("chapter_id") != int(chapter["id"]):
                continue
            copy = {k: v for k, v in q.items() if k != "id"}
            copy.update({"class_id": body.to_class_id, "chapter_id": new_id,
                         "created_by": user.id, "created_at": _now()})
            firestore_question_bank.add_document(str(firestore_question_bank.get_next_numeric_id()), copy)
    return list_chapters(body.to_class_id, body.subject_id)


# ---------------------------------------------------------------------------- questions

def question_out(q: dict, with_key: bool = True) -> dict:
    out = {
        "id": int(q["id"]),
        "scope": q.get("scope"),
        "class_id": q.get("class_id"),
        "subject_id": q.get("subject_id"),
        "chapter_id": q.get("chapter_id"),
        "category": q.get("category"),
        "question_type": q.get("question_type"),
        "text": q.get("text"),
        "options": q.get("options") or [],
        "explanation": q.get("explanation"),
        "difficulty": q.get("difficulty") or 1,
        "is_active": q.get("is_active", True),
        "created_at": q.get("created_at"),
    }
    if with_key:
        out["correct_key"] = q.get("correct_key")
    return out


def _question_fields(q) -> dict:
    return {
        "question_type": q.question_type,
        "text": q.text.strip(),
        "options": [{"key": o.key, "text": o.text.strip()} for o in q.options],
        "correct_key": q.correct_key,
        "explanation": (q.explanation or "").strip() or None,
        "difficulty": q.difficulty,
        "is_active": q.is_active,
    }


def _placement(user, chapter_id: int | None, category: str | None) -> dict:
    """Where a new question goes: a chapter the user may manage, or a global category."""
    if chapter_id is not None:
        chapter = _require_chapter(user, chapter_id)
        return {"scope": SCOPE_CLASS, "class_id": chapter["class_id"],
                "subject_id": chapter["subject_id"], "chapter_id": int(chapter_id), "category": None}
    assert_admin(user)
    category = (category or "").strip().upper()
    if category not in CATEGORY_IDS:
        raise HTTPException(status_code=400, detail="Choose a category for a global question.")
    return {"scope": SCOPE_GLOBAL, "class_id": None, "subject_id": None, "chapter_id": None,
            "category": category}


def create_question(user, q) -> dict:
    forget_memo()
    place = _placement(user, q.chapter_id, q.category)
    question_id = firestore_question_bank.get_next_numeric_id()
    data = {**place, **_question_fields(q), "created_by": user.id, "created_at": _now()}
    firestore_question_bank.add_document(str(question_id), data)
    return question_out({**data, "id": question_id})


def create_questions_bulk(user, body) -> dict:
    forget_memo()
    created = 0
    places: dict = {}
    for q in body.questions:
        chapter_id = q.chapter_id if q.chapter_id is not None else body.chapter_id
        category = q.category or body.category
        key = (chapter_id, category)
        if key not in places:
            places[key] = _placement(user, chapter_id, category)
        question_id = firestore_question_bank.get_next_numeric_id()
        firestore_question_bank.add_document(str(question_id), {
            **places[key], **_question_fields(q), "created_by": user.id, "created_at": _now(),
        })
        created += 1
    return {"created": created}


def _require_question(user, question_id: int) -> dict:
    q = require_document(firestore_question_bank, question_id, "Question")
    if q.get("scope") == SCOPE_GLOBAL:
        assert_admin(user)
    elif not can_manage(user, q.get("class_id"), q.get("subject_id")):
        raise HTTPException(status_code=403, detail="You do not teach this subject in this class.")
    return q


def update_question(user, question_id: int, body) -> dict:
    forget_memo()
    from app.schemas.arena import QuestionIn

    current = _require_question(user, question_id)
    changes = body.model_dump(exclude_unset=True)
    merged = {
        "question_type": current.get("question_type"),
        "text": current.get("text"),
        "options": current.get("options") or [],
        "correct_key": current.get("correct_key"),
        "explanation": current.get("explanation"),
        "difficulty": current.get("difficulty") or 1,
        "is_active": current.get("is_active", True),
        **{k: v for k, v in changes.items() if k != "category"},
    }
    if merged["question_type"] == "TRUE_FALSE" and "options" not in changes:
        merged["options"] = []
    try:
        validated = QuestionIn(**merged)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    data = _question_fields(validated)
    if "category" in changes and current.get("scope") == SCOPE_GLOBAL:
        category = (changes["category"] or "").upper()
        if category not in CATEGORY_IDS:
            raise HTTPException(status_code=400, detail="Unknown category.")
        data["category"] = category
    data["updated_at"] = _now()
    firestore_question_bank.add_document(str(question_id), data)
    return question_out(firestore_question_bank.get_document(str(question_id)))


def delete_question(user, question_id: int) -> None:
    forget_memo()
    _require_question(user, question_id)
    firestore_question_bank.delete_document(str(question_id))


def list_questions(user, chapter_id: int | None, scope: str | None, category: str | None) -> list[dict]:
    if chapter_id is not None:
        _require_chapter(user, chapter_id)
        rows = firestore_question_bank.query_documents("chapter_id", "==", int(chapter_id))
    else:
        assert_admin(user)
        rows = global_questions()
        if category:
            rows = [r for r in rows if r.get("category") == category.upper()]
    rows.sort(key=lambda r: r.get("created_at") or "")
    return [question_out(r) for r in rows]


def global_questions() -> list[dict]:
    return firestore_question_bank.query_documents("scope", "==", SCOPE_GLOBAL)


# --------------------------------------------------------------------------- challenges

def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def challenge_is_open(challenge: dict, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if not challenge.get("is_active", True):
        return False
    starts, ends = _parse_dt(challenge.get("starts_at")), _parse_dt(challenge.get("ends_at"))
    return (starts is None or starts <= now) and (ends is None or now <= ends)


def challenge_pool(challenge: dict, rows: list[dict] | None = None) -> list[dict]:
    rows = global_questions() if rows is None else rows
    categories = set(challenge.get("categories") or [])
    difficulty = challenge.get("difficulty")
    return [
        r for r in rows
        if r.get("is_active", True)
        and (not categories or r.get("category") in categories)
        and (difficulty is None or r.get("difficulty") == difficulty)
    ]


def challenge_out(challenge: dict, rows: list[dict] | None = None) -> dict:
    return {
        "id": int(challenge["id"]),
        "title": challenge.get("title"),
        "description": challenge.get("description"),
        "emoji": challenge.get("emoji") or "🌍",
        "categories": challenge.get("categories") or [],
        "difficulty": challenge.get("difficulty"),
        "rounds": challenge.get("rounds") or 7,
        "starts_at": challenge.get("starts_at"),
        "ends_at": challenge.get("ends_at"),
        "is_active": challenge.get("is_active", True),
        "is_open": challenge_is_open(challenge),
        "question_count": len(challenge_pool(challenge, rows)),
    }


def list_challenges(open_only: bool = False) -> list[dict]:
    if open_only:
        return _memoized(("challenges",), lambda: _list_challenges(True))
    return _list_challenges(False)


def _list_challenges(open_only: bool) -> list[dict]:
    got = run_parallel({"rows": global_questions, "challenges": firestore_arena_challenges.list_all})
    rows = got["rows"] or []
    challenges = sorted(got["challenges"] or [], key=lambda c: c.get("created_at") or "")
    out = [challenge_out(c, rows) for c in challenges]
    return [c for c in out if c["is_open"]] if open_only else out


def _challenge_fields(body, partial: bool) -> dict:
    data = body.model_dump(exclude_unset=partial)
    if "categories" in data and data["categories"] is not None:
        cats = [c.upper() for c in data["categories"]]
        unknown = [c for c in cats if c not in CATEGORY_IDS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown categories: {', '.join(unknown)}")
        data["categories"] = cats
    for key in ("starts_at", "ends_at"):
        if isinstance(data.get(key), datetime):
            data[key] = data[key].replace(tzinfo=None).isoformat()
    return data


def create_challenge(user, body) -> dict:
    forget_memo()
    assert_admin(user)
    challenge_id = firestore_arena_challenges.get_next_numeric_id()
    data = {**_challenge_fields(body, partial=False), "created_by": user.id, "created_at": _now()}
    firestore_arena_challenges.add_document(str(challenge_id), data)
    return challenge_out({**data, "id": challenge_id})


def update_challenge(user, challenge_id: int, body) -> dict:
    forget_memo()
    assert_admin(user)
    require_document(firestore_arena_challenges, challenge_id, "Challenge")
    data = _challenge_fields(body, partial=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    data["updated_at"] = _now()
    firestore_arena_challenges.add_document(str(challenge_id), data)
    return challenge_out(firestore_arena_challenges.get_document(str(challenge_id)))


def delete_challenge(user, challenge_id: int) -> None:
    forget_memo()
    assert_admin(user)
    require_document(firestore_arena_challenges, challenge_id, "Challenge")
    firestore_arena_challenges.delete_document(str(challenge_id))


# ------------------------------------------------------------------- what students play

def playable_subjects(class_id: int) -> list[dict]:
    """The student's subjects, each with its chapters that have enough questions to battle."""
    return _memoized(("subjects", class_id), lambda: _playable_subjects(class_id))


def _playable_subjects(class_id: int) -> list[dict]:
    got = run_parallel({
        "chapters": lambda: _chapters_of(class_id),
        "questions": lambda: _class_questions(class_id),
        "mappings": lambda: firestore_teacher_mappings.query_documents("class_id", "==", class_id),
    })
    chapters = [c for c in (got["chapters"] or []) if c.get("is_active", True)]
    questions = [q for q in (got["questions"] or []) if q.get("is_active", True)]
    counts = question_counts(questions)

    subjects: dict[int, dict] = {}
    for m in got["mappings"] or []:
        sid = m.get("subject_id")
        doc = firestore_subjects.get_document(str(sid)) if sid is not None else None
        if doc and sid not in subjects:
            subjects[sid] = {"subject_id": sid, "name": doc.get("name"), "chapters": [], "question_count": 0}
    for c in chapters:
        sid = c.get("subject_id")
        if sid not in subjects:
            doc = firestore_subjects.get_document(str(sid))
            if not doc:
                continue
            subjects[sid] = {"subject_id": sid, "name": doc.get("name"), "chapters": [], "question_count": 0}
        active = counts.get(int(c["id"]), {}).get("active", 0)
        subjects[sid]["chapters"].append({
            "id": int(c["id"]), "title": c.get("title"), "order": c.get("order") or 0,
            "question_count": active, "playable": active >= MIN_BATTLE_QUESTIONS,
        })
    for q in questions:
        if q.get("subject_id") in subjects:
            subjects[q["subject_id"]]["question_count"] += 1
    out = sorted(subjects.values(), key=lambda s: s["name"] or "")
    for s in out:
        s["playable"] = s["question_count"] >= MIN_BATTLE_QUESTIONS
    return out


def class_battle_pool(class_id: int, subject_id: int | None, chapter_id: int | None) -> list[dict]:
    rows = [q for q in _class_questions(class_id) if q.get("is_active", True)]
    if chapter_id is not None:
        rows = [q for q in rows if q.get("chapter_id") == chapter_id]
    elif subject_id is not None:
        rows = [q for q in rows if q.get("subject_id") == subject_id]
    # Hidden chapters stay out of battles too.
    hidden = {int(c["id"]) for c in _chapters_of(class_id) if not c.get("is_active", True)}
    return [q for q in rows if q.get("chapter_id") not in hidden]
