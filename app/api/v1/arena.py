"""
The arena: quiz battles between classmates (or against a bot), rewards and leaderboards.

Students play under `/arena`; teachers and admins write chapters, questions and challenges
under `/arena/manage`. The design is in docs/ARENA.md.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import require_any_authenticated, require_student, require_teacher
from app.core.concurrency import run_parallel
from app.core.enums import UserRole
from app.core.firebase import firestore_classes
from app.schemas.arena import (
    AnswerIn, ChallengeIn, ChallengeUpdate, ChapterCopy, ChapterCreate, ChapterReorder,
    ChapterUpdate, EmoteIn, ItemChoice, JoinByCode, MatchCreate, QuestionBulk, QuestionIn,
    QuestionUpdate, QueueJoin,
)
from app.schemas.user import UserOut
from app.services.arena import catalog, content, engine, leaderboard, profiles, starter_pack
from app.services.permissions import led_class_ids

router = APIRouter(prefix="/arena", tags=["Arena"])
manage_router = APIRouter(prefix="/arena/manage", tags=["Arena (manage)"])


def _student_only(user: UserOut) -> None:
    if user.role != UserRole.STUDENT:
        raise HTTPException(status_code=403, detail="The arena is for students. Use Arena management to set it up.")


def _with_people(user: UserOut) -> None:
    """
    Classmates, rooms, quick match and invites are the school arena. A tuition-only student
    gets the solo half (the bot, the lab, the locker) from the tuition games shelf.
    """
    profiles.require_school_player(user)


# ===================================================================== student: home

@router.get("/me")
def arena_home(current_user: UserOut = Depends(require_student)):
    """
    [Student] Everything the arena home needs in one call: my profile, the subjects and
    chapters I can battle on, open global challenges, challenges sent to me, my active battle
    and my recent battles.
    """
    _student_only(current_user)
    # Every database round trip is slow from here, so independent reads run together.
    first = run_parallel({
        "profile": lambda: profiles.get_profile(current_user.id),
        "class_id": lambda: profiles.student_class_id(current_user.id),
    })
    profile, class_id = first["profile"], first["class_id"]
    if profile is None:
        raise HTTPException(status_code=503, detail="The arena could not load your profile. Try again.")
    if class_id is not None and profile.get("class_id") != class_id:
        profiles.save_profile(current_user.id, {"class_id": class_id})
        profile["class_id"] = class_id
    rest = run_parallel({
        "active": lambda: engine.current_match_id(current_user.id, profile),
        "class_doc": lambda: firestore_classes.get_document(str(class_id)) if class_id is not None else None,
        "subjects": lambda: content.playable_subjects(class_id) if class_id is not None else [],
        "challenges": lambda: content.list_challenges(open_only=True),
        "invites": lambda: engine.pending_invites(current_user),
    })
    active = rest["active"]
    profile["active_match_id"] = active
    class_doc = rest["class_doc"]
    return {
        "profile": profiles.my_profile_out(profile, current_user, class_doc),
        "class": {"id": class_id, "name": class_doc.get("name")} if class_doc else None,
        "subjects": rest["subjects"] or [],
        "challenges": rest["challenges"] or [],
        "invites": rest["invites"] or [],
        "active_match_id": active,
        "recent": profile.get("recent") or [],
        "bots": [{"level": level, "name": bot["name"], "avatar": bot["avatar"], "accuracy": bot["accuracy"]}
                 for level, bot in catalog.BOTS.items()],
        "min_questions": content.MIN_BATTLE_QUESTIONS,
    }


@router.get("/catalog")
def arena_catalog(current_user: UserOut = Depends(require_student)):
    """[Student] Every item and badge, with what I own, have equipped, or still need to unlock."""
    _student_only(current_user)
    return profiles.catalog_for(profiles.get_profile(current_user.id))


@router.post("/shop/buy")
def buy_item(body: ItemChoice, current_user: UserOut = Depends(require_student)):
    """[Student] Buy an item with coins; it is equipped straight away."""
    _student_only(current_user)
    profile = profiles.buy(current_user.id, body.item_id)
    return profiles.my_profile_out(profile, current_user)


@router.post("/equip")
def equip_item(body: ItemChoice, current_user: UserOut = Depends(require_student)):
    """[Student] Wear an item I own."""
    _student_only(current_user)
    profile = profiles.equip(current_user.id, body.item_id)
    return profiles.my_profile_out(profile, current_user)


@router.get("/classmates")
def list_classmates(current_user: UserOut = Depends(require_student)):
    """[Student] My classmates, online first, to challenge."""
    _student_only(current_user)
    _with_people(current_user)
    return engine.classmates(current_user)


# ==================================================================== student: battles

@router.post("/matches", status_code=status.HTTP_201_CREATED)
def create_match(body: MatchCreate, current_user: UserOut = Depends(require_student)):
    """
    [Student] Start a battle: BOT starts at once; CHALLENGE invites classmates and starts
    when they accept; ROOM opens a lobby with a code the host starts.
    """
    _student_only(current_user)
    return engine.create_match(current_user, body)


@router.get("/matches")
def my_recent_matches(current_user: UserOut = Depends(require_student)):
    """[Student] My last dozen battles."""
    _student_only(current_user)
    return profiles.get_profile(current_user.id).get("recent") or []


@router.post("/matches/join")
def join_room(body: JoinByCode, current_user: UserOut = Depends(require_student)):
    """[Student] Join a room by its code."""
    _student_only(current_user)
    _with_people(current_user)
    return engine.join_by_code(current_user, body.code)


@router.get("/matches/{match_id}")
def get_match(match_id: int, current_user: UserOut = Depends(require_student)):
    """
    [Student] The battle as I see it now. Poll about once a second while it is live;
    `server_now` lets the client run its timer on the server's clock.
    """
    return engine.view(match_id, current_user)


@router.post("/matches/{match_id}/start")
def start_match(match_id: int, current_user: UserOut = Depends(require_student)):
    """[Student, host] Start a room."""
    _with_people(current_user)
    return engine.start_match(current_user, match_id)


@router.post("/matches/{match_id}/answer")
def answer_question(match_id: int, body: AnswerIn, current_user: UserOut = Depends(require_student)):
    """[Student] Answer the current question; says at once whether I was right."""
    return engine.answer(current_user, match_id, body.round, body.option_key)


@router.post("/matches/{match_id}/emote", status_code=status.HTTP_204_NO_CONTENT)
def send_emote(match_id: int, body: EmoteIn, current_user: UserOut = Depends(require_student)):
    """[Student] A quick reaction everyone in the battle sees for a few seconds."""
    engine.emote(current_user, match_id, body.emote)
    return None


@router.post("/matches/{match_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_match(match_id: int, current_user: UserOut = Depends(require_student)):
    """[Student] Leave a lobby, or forfeit a battle in progress."""
    engine.leave_match(current_user, match_id)
    return None


@router.post("/matches/{match_id}/rematch", status_code=status.HTTP_201_CREATED)
def rematch(match_id: int, current_user: UserOut = Depends(require_student)):
    """[Student] Play again with the same people and questions source."""
    _student_only(current_user)
    return engine.rematch(current_user, match_id)


@router.post("/invites/{invite_id}/accept")
def accept_invite(invite_id: int, current_user: UserOut = Depends(require_student)):
    """[Student] Accept a classmate's challenge."""
    _with_people(current_user)
    return engine.accept_invite(current_user, invite_id)


@router.post("/invites/{invite_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
def decline_invite(invite_id: int, current_user: UserOut = Depends(require_student)):
    """[Student] Turn a challenge down."""
    _with_people(current_user)
    engine.decline_invite(current_user, invite_id)
    return None


@router.post("/queue")
def join_queue(body: QueueJoin, current_user: UserOut = Depends(require_student)):
    """[Student] Quick match: pair with anyone waiting on the same questions, or wait."""
    _student_only(current_user)
    _with_people(current_user)
    return engine.join_queue(current_user, body)


@router.get("/queue")
def poll_queue(current_user: UserOut = Depends(require_student)):
    """[Student] Still waiting, or matched? Poll every couple of seconds."""
    _with_people(current_user)
    return engine.poll_queue(current_user)


@router.delete("/queue", status_code=status.HTTP_204_NO_CONTENT)
def leave_queue(current_user: UserOut = Depends(require_student)):
    """[Student] Stop waiting."""
    _with_people(current_user)
    engine.leave_queue(current_user)
    return None


# ================================================================ leaderboards, cards

def _staff_class_ids(user: UserOut) -> set[int] | None:
    """Classes a teacher may see boards for; None means all (an admin)."""
    if user.role == UserRole.ADMIN:
        return None
    from app.core.firebase import firestore_teacher_mappings

    taught = {m.get("class_id") for m in firestore_teacher_mappings.query_documents("teacher_id", "==", user.id)}
    return {c for c in taught | led_class_ids(user) if c is not None}


@router.get("/leaderboard")
def get_leaderboard(
    board: str = Query("class", pattern="^(class|global|challenge)$"),
    period: str = Query("week", pattern="^(week|all)$"),
    challenge_id: Optional[int] = Query(None),
    class_id: Optional[int] = Query(None, description="Staff only; a student always sees their own class"),
    current_user: UserOut = Depends(require_any_authenticated),
):
    """
    A leaderboard. Students see their own class's board (never another's), the global
    board and challenge boards; teachers see the classes they teach; admins see everything.
    """
    viewer_id = current_user.id if current_user.role == UserRole.STUDENT else None
    if current_user.role == UserRole.STUDENT:
        # The boards rank the school; online tuition plays without them.
        profiles.require_school_player(current_user, profiles.TUITION_NO_BOARDS)
    if board == "class":
        if current_user.role == UserRole.STUDENT:
            class_id = profiles.require_student_class(current_user)
        else:
            if class_id is None:
                raise HTTPException(status_code=400, detail="Choose a class.")
            allowed = _staff_class_ids(current_user)
            if allowed is not None and class_id not in allowed:
                raise HTTPException(status_code=403, detail="You do not teach this class.")
        result = leaderboard.board(f"class:{class_id}", period, viewer_id)
        class_doc = firestore_classes.get_document(str(class_id))
        result["class"] = {"id": class_id, "name": class_doc.get("name") if class_doc else None}
        return result
    if board == "challenge":
        if challenge_id is None:
            raise HTTPException(status_code=400, detail="Choose a challenge.")
        return leaderboard.board(f"challenge:{challenge_id}", period, viewer_id)
    return leaderboard.board("global", period, viewer_id)


@router.get("/profiles/{student_id}")
def get_profile_card(student_id: int, current_user: UserOut = Depends(require_any_authenticated)):
    """
    A student's profile card. Students may open classmates' cards and the card of anyone on
    the global boards; teachers the students of their classes; admins anyone.
    """
    target_class = profiles.student_class_id(student_id)
    if current_user.role == UserRole.STUDENT and student_id != current_user.id:
        profiles.require_school_player(current_user, profiles.TUITION_NO_BOARDS)
        same_class = target_class is not None and target_class == profiles.student_class_id(current_user.id)
        on_global = leaderboard.rank_of("global", "all", student_id) is not None
        if not same_class and not on_global:
            raise HTTPException(status_code=403, detail="You can only see classmates and global players.")
    elif current_user.role in (UserRole.TEACHER, UserRole.CLASS_TEACHER):
        allowed = _staff_class_ids(current_user)
        on_global = leaderboard.rank_of("global", "all", student_id) is not None
        if target_class not in (allowed or set()) and not on_global:
            raise HTTPException(status_code=403, detail="This student is not in a class you teach.")
    elif current_user.role == UserRole.PARENT:
        raise HTTPException(status_code=403, detail="Not available.")
    return profiles.profile_card(student_id)


@router.get("/challenges")
def open_challenges(_: UserOut = Depends(require_any_authenticated)):
    """Global challenges open right now."""
    return content.list_challenges(open_only=True)


# ============================================================ manage: chapters, questions

@manage_router.get("/scopes")
def manageable_scopes(current_user: UserOut = Depends(require_teacher)):
    """
    [Teacher/Admin] The class-subject pairs I can write chapters for. Admins also get every
    class and subject, since they may set up a pair nobody is mapped to yet.
    """
    out = {"pairs": content.manageable_pairs(current_user), "is_admin": content.is_admin(current_user),
           "categories": catalog.GLOBAL_CATEGORIES}
    if content.is_admin(current_user):
        from app.core.firebase import firestore_subjects

        out["classes"] = sorted(({"id": int(c["id"]), "name": c.get("name")} for c in firestore_classes.list_all()),
                                key=lambda c: c["name"] or "")
        out["subjects"] = sorted(({"id": int(s["id"]), "name": s.get("name")} for s in firestore_subjects.list_all()),
                                 key=lambda s: s["name"] or "")
    return out


@manage_router.get("/chapters")
def list_chapters(class_id: int, subject_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] The chapters of one class's subject, in order, with question counts."""
    content.assert_can_manage(current_user, class_id, subject_id)
    return content.list_chapters(class_id, subject_id)


@manage_router.post("/chapters", status_code=status.HTTP_201_CREATED)
def create_chapter(body: ChapterCreate, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Add a chapter at the end of the list."""
    return content.create_chapter(current_user, body)


@manage_router.post("/chapters/reorder")
def reorder_chapters(body: ChapterReorder, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Put a subject's chapters in a new order."""
    return content.reorder_chapters(current_user, body)


@manage_router.post("/chapters/copy")
def copy_chapters(body: ChapterCopy, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Copy one class's chapters (and questions) for a subject into another class."""
    return content.copy_chapters(current_user, body)


@manage_router.put("/chapters/{chapter_id}")
def update_chapter(chapter_id: int, body: ChapterUpdate, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Rename, describe, hide or show a chapter."""
    return content.update_chapter(current_user, chapter_id, body)


@manage_router.delete("/chapters/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(chapter_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Delete a chapter and its questions."""
    content.delete_chapter(current_user, chapter_id)
    return None


@manage_router.get("/questions")
def list_questions(
    chapter_id: Optional[int] = Query(None),
    scope: Optional[str] = Query(None, pattern="^(CLASS|GLOBAL)$"),
    category: Optional[str] = Query(None),
    current_user: UserOut = Depends(require_teacher),
):
    """[Teacher/Admin] A chapter's questions, or (admins) the global pool by category."""
    if chapter_id is None and scope != "GLOBAL":
        raise HTTPException(status_code=400, detail="Choose a chapter, or scope=GLOBAL.")
    return content.list_questions(current_user, chapter_id, scope, category)


@manage_router.post("/questions", status_code=status.HTTP_201_CREATED)
def create_question(body: QuestionIn, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Add one question to a chapter, or (admins) to the global pool."""
    return content.create_question(current_user, body)


@manage_router.post("/questions/bulk", status_code=status.HTTP_201_CREATED)
def create_questions_bulk(body: QuestionBulk, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Add many questions at once (the client parses pasted text or CSV)."""
    return content.create_questions_bulk(current_user, body)


@manage_router.put("/questions/{question_id}")
def update_question(question_id: int, body: QuestionUpdate, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Edit a question."""
    return content.update_question(current_user, question_id, body)


@manage_router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Delete a question. Battles already played keep their copy."""
    content.delete_question(current_user, question_id)
    return None


# ======================================================================= manage: global

@manage_router.get("/challenges")
def list_all_challenges(current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] Every global challenge, open or not."""
    return content.list_challenges()


@manage_router.post("/challenges", status_code=status.HTTP_201_CREATED)
def create_challenge(body: ChallengeIn, current_user: UserOut = Depends(require_teacher)):
    """[Admin] Create a global challenge over the global question pool."""
    return content.create_challenge(current_user, body)


@manage_router.put("/challenges/{challenge_id}")
def update_challenge(challenge_id: int, body: ChallengeUpdate, current_user: UserOut = Depends(require_teacher)):
    """[Admin] Edit a global challenge."""
    return content.update_challenge(current_user, challenge_id, body)


@manage_router.delete("/challenges/{challenge_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_challenge(challenge_id: int, current_user: UserOut = Depends(require_teacher)):
    """[Admin] Delete a global challenge. Its leaderboard rows stay, unreachable."""
    content.delete_challenge(current_user, challenge_id)
    return None


@manage_router.post("/starter-pack")
def install_starter_pack(current_user: UserOut = Depends(require_teacher)):
    """[Admin] Install the starter global questions and challenges. Safe to press twice."""
    content.assert_admin(current_user)
    return starter_pack.install(current_user)


@manage_router.get("/classes")
def leaderboard_classes(current_user: UserOut = Depends(require_teacher)):
    """[Teacher/Admin] The classes whose leaderboards I may open."""
    allowed = _staff_class_ids(current_user)
    rows = firestore_classes.list_all()
    return sorted(({"id": int(c["id"]), "name": c.get("name")} for c in rows
                   if allowed is None or int(c["id"]) in allowed), key=lambda c: c["name"] or "")
