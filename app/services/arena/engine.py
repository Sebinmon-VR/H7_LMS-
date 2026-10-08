"""
The battle engine.

A match document holds who is playing, the questions (answer keys included - this document
is never sent as stored), the bot's pre-drawn answers and, once over, the outcome. Each
answer is its own document, created atomically with id `{match}:{round}:{user}`.

The live phase - countdown, question, reveal, finished - is *derived* from those and the
clock on every read (`timeline`). Nothing advances a round, so there is no background job,
no state for two simultaneous answers to fight over, and any worker can serve any poll.

    started_at -3s countdown- Q1 (15 s, or until everyone answered) -4s reveal- Q2 ... done
"""
import logging
import random
import secrets
import string
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.enums import UserRole
from app.core.firebase import (
    firestore_arena_answers, firestore_arena_challenges, firestore_arena_claims,
    firestore_arena_invites, firestore_arena_matches, firestore_arena_queue,
    firestore_classes, firestore_student_enrollments, firestore_subjects,
    firestore_syllabus_chapters, firestore_user_presence, firestore_users,
)
from app.services.arena import catalog, content
from app.services.arena.profiles import (
    equipped_look, get_profile, player_summary, require_student_class, save_profile,
    student_class_id,
)

logger = logging.getLogger("arena")

COUNTDOWN_S = 3.0
QUESTION_S = 15.0
REVEAL_S = 4.0
# After the last answer comes in, a beat before the reveal so the tap can land on screen.
SETTLE_S = 0.6
MAX_PLAYERS = 8
LOBBY_TTL = timedelta(minutes=15)
INVITE_TTL = timedelta(minutes=2)
QUEUE_STALE_S = 8.0
EMOTE_WINDOW_S = 6.0

LOBBY, LIVE, FINISHED, CANCELLED = "LOBBY", "LIVE", "FINISHED", "CANCELLED"
BOT_PREFIX = "bot:"


def _now() -> datetime:
    return datetime.utcnow()


def _iso(at: datetime) -> str:
    return at.isoformat()


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _is_bot(uid: str) -> bool:
    return str(uid).startswith(BOT_PREFIX)


def _players(match: dict) -> dict[str, dict]:
    """Seated players; a player removed from a lobby is stored as None."""
    return {uid: p for uid, p in (match.get("players") or {}).items() if p}


def _humans(match: dict) -> list[str]:
    return [uid for uid, p in _players(match).items() if not p.get("is_bot")]


# ------------------------------------------------------------------------------- scoring

def points_for(correct: bool, elapsed_s: float, streak_before: int) -> int:
    """500 for a right answer, up to 500 more for speed, +50 per right answer in a row before it."""
    if not correct:
        return 0
    remaining = max(0.0, QUESTION_S - elapsed_s) / QUESTION_S
    return int(round(500 + 500 * remaining)) + 50 * min(streak_before, 4)


# ------------------------------------------------------------------------------ timeline

def _answers_by_round(match_id) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    for a in firestore_arena_answers.query_documents("match_id", "==", int(match_id)):
        out.setdefault(int(a["round"]), {})[str(a["user_id"])] = a
    return out


def _bot_answer(match: dict, uid: str, round_index: int, start: datetime) -> dict | None:
    plan = ((match.get("bot_plan") or {}).get(uid) or [])
    if round_index >= len(plan):
        return None
    step = plan[round_index]
    if step["delay"] >= QUESTION_S:
        return None
    return {"option_key": step["pick"], "correct": step["correct"],
            "answered_at": start + timedelta(seconds=step["delay"]), "elapsed": step["delay"]}


def timeline(match: dict, answers: dict[int, dict[str, dict]]) -> list[dict]:
    """
    When each round starts, ends and finishes revealing, and who answered what.

    A round ends at its 15-second deadline, or a beat after the last player answers if that
    is sooner. Bot answers are pre-drawn, so they count as "in" at their scheduled moment.
    A player who left counts as having answered nothing the moment they left.
    """
    started = _parse(match.get("started_at"))
    if started is None:
        return []
    left = {uid: _parse(ts) for uid, ts in (match.get("left") or {}).items() if ts}
    players = _players(match)
    t = started + timedelta(seconds=COUNTDOWN_S)
    rounds = []
    streaks = {uid: 0 for uid in players}
    for index, question in enumerate(match.get("questions") or []):
        start = t
        deadline = start + timedelta(seconds=QUESTION_S)
        picks: dict[str, dict] = {}
        times = []
        everyone_in = True
        for uid, player in players.items():
            if player.get("is_bot"):
                answer = _bot_answer(match, uid, index, start)
                if answer:
                    answer["points"] = points_for(answer["correct"], answer["elapsed"], streaks[uid])
            else:
                stored = answers.get(index, {}).get(uid)
                answer = None
                if stored:
                    answer = {"option_key": stored.get("option_key"), "correct": bool(stored.get("correct")),
                              "answered_at": _parse(stored.get("answered_at")),
                              "elapsed": (stored.get("elapsed_ms") or 0) / 1000,
                              "points": int(stored.get("points") or 0)}
            done_at = answer["answered_at"] if answer else None
            if uid in left and left[uid] is not None:
                gone = max(start, left[uid])
                done_at = min(done_at, gone) if done_at else gone
            if done_at is None or done_at > deadline:
                everyone_in = False
            else:
                times.append(done_at)
            if answer:
                picks[uid] = answer
                streaks[uid] = streaks[uid] + 1 if answer["correct"] else 0
            else:
                streaks[uid] = 0
        end = deadline
        if everyone_in and times:
            end = min(deadline, max(times) + timedelta(seconds=SETTLE_S))
        rounds.append({"index": index, "start": start, "deadline": deadline, "end": end,
                       "reveal_end": end + timedelta(seconds=REVEAL_S), "picks": picks})
        t = end + timedelta(seconds=REVEAL_S)
    return rounds


def phase_at(match: dict, rounds: list[dict], now: datetime) -> dict:
    status = match.get("status")
    if status == CANCELLED:
        return {"phase": CANCELLED}
    if status == LOBBY:
        created = _parse(match.get("created_at")) or now
        if now - created > LOBBY_TTL:
            return {"phase": CANCELLED}
        return {"phase": LOBBY}
    if status == FINISHED:
        return {"phase": FINISHED}

    started = _parse(match.get("started_at"))
    # Everybody gone: the battle is over the moment the last person left.
    humans = _humans(match)
    left = match.get("left") or {}
    if humans and all(left.get(uid) for uid in humans):
        return {"phase": FINISHED}
    if started and now < started + timedelta(seconds=COUNTDOWN_S):
        return {"phase": "COUNTDOWN", "ends_at": started + timedelta(seconds=COUNTDOWN_S)}
    for r in rounds:
        if now < r["end"]:
            return {"phase": "QUESTION", "round": r["index"], "starts_at": r["start"], "ends_at": r["deadline"]}
        if now < r["reveal_end"]:
            return {"phase": "REVEAL", "round": r["index"], "starts_at": r["end"], "ends_at": r["reveal_end"]}
    return {"phase": FINISHED}


def scores(match: dict, rounds: list[dict], upto: int | None = None) -> dict[str, dict]:
    """Totals over rounds [0, upto); every round when `upto` is None."""
    out = {uid: {"score": 0, "correct": 0, "streak": 0} for uid in _players(match)}
    for r in rounds[: upto if upto is not None else len(rounds)]:
        for uid in out:
            answer = r["picks"].get(uid)
            if answer and answer["correct"]:
                out[uid]["score"] += answer["points"]
                out[uid]["correct"] += 1
                out[uid]["streak"] += 1
            else:
                out[uid]["streak"] = 0
    return out


def standings(match: dict, totals: dict[str, dict]) -> list[dict]:
    order = sorted(totals.items(), key=lambda kv: (-kv[1]["score"], -kv[1]["correct"]))
    out, rank, previous = [], 0, None
    for index, (uid, total) in enumerate(order, start=1):
        if total["score"] != previous:
            rank, previous = index, total["score"]
        out.append({"user_id": uid, "rank": rank, **total})
    return out


# ---------------------------------------------------------------------------- seating

def _seat(user_doc: dict, profile: dict) -> dict:
    return {
        "user_id": int(user_doc["id"]),
        "name": user_doc.get("full_name") or "Student",
        "photo_url": user_doc.get("photo_url"),
        "look": equipped_look(profile),
        "level": catalog.level_for_xp(int(profile.get("xp") or 0)),
        "class_id": profile.get("class_id"),
        "is_bot": False,
        "joined_at": _iso(_now()),
    }


def _bot_seat(level: str) -> dict:
    bot = catalog.BOTS[level]
    look = equipped_look(None)
    look["avatar"] = {**look["avatar"], "emoji": bot["avatar"], "name": bot["name"]}
    look["title"] = {**look["title"], "text": f"{level.title()} bot"}
    return {"user_id": None, "name": bot["name"], "look": look, "level": bot["level"],
            "is_bot": True, "bot_level": level, "joined_at": _iso(_now())}


def _user_doc(user) -> dict:
    return {"id": user.id, "full_name": user.full_name, "photo_url": getattr(user, "photo_url", None)}


def _clear_if_stale(profile: dict) -> None:
    """Drops a pointer to a battle that is over, so a closed tab never locks a student out."""
    match_id = profile.get("active_match_id")
    if not match_id:
        return
    match = firestore_arena_matches.get_document(str(match_id))
    if not match:
        save_profile(profile["student_id"], {"active_match_id": None})
        profile["active_match_id"] = None
        return
    phase = phase_at(match, timeline(match, _answers_by_round(match_id)), _now())["phase"]
    if phase in (FINISHED, CANCELLED) or str(profile["student_id"]) not in _players(match) \
            or (match.get("left") or {}).get(str(profile["student_id"])):
        if phase == FINISHED and match.get("status") == LIVE:
            finalize_if_due(match)
        save_profile(profile["student_id"], {"active_match_id": None})
        profile["active_match_id"] = None


def current_match_id(student_id: int, profile: dict | None = None) -> int | None:
    profile = profile if profile is not None else get_profile(student_id)
    if not profile.get("active_match_id"):
        return None
    _clear_if_stale(profile)
    return profile.get("active_match_id")


def _assert_free(student_id: int) -> dict:
    profile = get_profile(student_id)
    _clear_if_stale(profile)
    if profile.get("active_match_id"):
        raise HTTPException(status_code=409, detail={
            "message": "You are already in a battle. Finish or leave it first.",
            "match_id": profile["active_match_id"],
        })
    return profile


# ------------------------------------------------------------------------------ content

def _resolve_content(user, scope: str, subject_id, chapter_id, challenge_id) -> dict:
    """Where the questions come from, checked against what this student may play."""
    if scope == "CLASS":
        class_id = require_student_class(user)
        subject_name = chapter_title = None
        if chapter_id is not None:
            chapter = firestore_syllabus_chapters.get_document(str(chapter_id))
            if not chapter or chapter.get("class_id") != class_id or not chapter.get("is_active", True):
                raise HTTPException(status_code=404, detail="That chapter is not part of your class.")
            subject_id = chapter.get("subject_id")
            chapter_title = chapter.get("title")
        if subject_id is not None:
            subject = firestore_subjects.get_document(str(subject_id))
            if not subject:
                raise HTTPException(status_code=404, detail="Subject not found")
            subject_name = subject.get("name")
        pool = content.class_battle_pool(class_id, subject_id, chapter_id)
        title = chapter_title or subject_name or "All subjects"
        return {"scope": "CLASS", "class_id": class_id, "subject_id": subject_id, "chapter_id": chapter_id,
                "challenge_id": None, "title": title, "subtitle": subject_name if chapter_title else None,
                "pool": pool, "rounds": None}

    challenge = firestore_arena_challenges.get_document(str(challenge_id))
    if not challenge or not content.challenge_is_open(challenge):
        raise HTTPException(status_code=404, detail="That challenge is not open right now.")
    return {"scope": "GLOBAL", "class_id": student_class_id(user.id), "subject_id": None, "chapter_id": None,
            "challenge_id": int(challenge_id), "title": challenge.get("title"),
            "subtitle": "Global challenge", "emoji": challenge.get("emoji"),
            "pool": content.challenge_pool(challenge), "rounds": challenge.get("rounds") or 7}


def _assert_enough(pool: list[dict]) -> None:
    if len(pool) < content.MIN_BATTLE_QUESTIONS:
        raise HTTPException(status_code=409, detail=(
            f"There are only {len(pool)} questions here so far - a battle needs at least "
            f"{content.MIN_BATTLE_QUESTIONS}. Ask your teacher to add more."))


def _draw_questions(pool: list[dict], rounds: int) -> list[dict]:
    picked = random.sample(pool, min(rounds, len(pool)))
    out = []
    for q in picked:
        options = list(q.get("options") or [])
        if q.get("question_type") != "TRUE_FALSE":
            random.shuffle(options)
        out.append({"qid": int(q["id"]), "text": q.get("text"), "question_type": q.get("question_type"),
                    "options": options, "correct_key": q.get("correct_key"),
                    "explanation": q.get("explanation"), "difficulty": int(q.get("difficulty") or 1),
                    "subject_id": q.get("subject_id"), "chapter_id": q.get("chapter_id"),
                    "category": q.get("category")})
    return out


def _bot_plan(level: str, questions: list[dict]) -> list[dict]:
    bot = catalog.BOTS[level]
    plan = []
    for q in questions:
        # Harder questions trip the bot up a little more often, like everyone else.
        accuracy = max(0.2, bot["accuracy"] - 0.06 * (q["difficulty"] - 1))
        correct = random.random() < accuracy
        wrong = [o["key"] for o in q["options"] if o["key"] != q["correct_key"]]
        pick = q["correct_key"] if correct or not wrong else random.choice(wrong)
        delay = round(random.uniform(*bot["delay"]), 2)
        plan.append({"pick": pick, "correct": correct, "delay": delay})
    return plan


def _room_code() -> str:
    alphabet = "".join(c for c in string.ascii_uppercase if c not in "IOL")
    for _ in range(20):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not firestore_arena_matches.query_documents("code", "==", code):
            return code
    raise HTTPException(status_code=503, detail="Could not make a room code; try again.")


# ------------------------------------------------------------------------------ create

def _new_match(mode: str, source: dict, host_id: int | None, rounds: int, players: dict, extra: dict | None = None) -> dict:
    match_id = firestore_arena_matches.get_next_numeric_id()
    data = {
        "mode": mode,
        "scope": source["scope"],
        "class_id": source["class_id"],
        "subject_id": source["subject_id"],
        "chapter_id": source["chapter_id"],
        "challenge_id": source["challenge_id"],
        "title": source["title"],
        "subtitle": source.get("subtitle"),
        "emoji": source.get("emoji"),
        "host_id": host_id,
        "rounds": source["rounds"] or rounds,
        "status": LOBBY,
        "players": players,
        "left": {},
        "emotes": {},
        "created_at": _iso(_now()),
        **(extra or {}),
    }
    firestore_arena_matches.add_document(str(match_id), data)
    return {**data, "id": match_id}


def _start(match: dict) -> dict:
    """Draws the questions and the bot's answers and starts the countdown. Once only."""
    if not firestore_arena_claims.create_document(f"start:{match['id']}", {"at": _iso(_now())}):
        return firestore_arena_matches.get_document(str(match["id"]))
    source = {
        "CLASS": lambda: content.class_battle_pool(match["class_id"], match.get("subject_id"), match.get("chapter_id")),
        "GLOBAL": lambda: content.challenge_pool(firestore_arena_challenges.get_document(str(match["challenge_id"])) or {}),
    }[match["scope"]]()
    if len(source) < content.MIN_BATTLE_QUESTIONS:
        firestore_arena_claims.delete_document(f"start:{match['id']}")
        _assert_enough(source)
    questions = _draw_questions(source, int(match.get("rounds") or 7))
    plan = {uid: _bot_plan(p["bot_level"], questions) for uid, p in _players(match).items() if p.get("is_bot")}
    fields = {"status": LIVE, "started_at": _iso(_now()), "questions": questions, "bot_plan": plan}
    firestore_arena_matches.add_document(str(match["id"]), fields)
    for invite in firestore_arena_invites.query_documents("match_id", "==", int(match["id"])):
        if invite.get("status") == "PENDING":
            firestore_arena_invites.add_document(str(invite["id"]), {"status": "EXPIRED"})
    return {**match, **fields}


def create_match(user, body) -> dict:
    profile = _assert_free(user.id)
    source = _resolve_content(user, body.scope, body.subject_id, body.chapter_id, body.challenge_id)
    _assert_enough(source["pool"])
    me = {str(user.id): _seat(_user_doc(user), profile)}

    if body.mode == "BOT":
        bot_uid = f"{BOT_PREFIX}{body.bot_level.lower()}"
        match = _new_match("BOT", source, user.id, body.rounds, {**me, bot_uid: _bot_seat(body.bot_level)},
                           {"bot_level": body.bot_level})
        match = _start(match)
    elif body.mode == "ROOM":
        match = _new_match("ROOM", source, user.id, body.rounds, me, {"code": _room_code()})
    else:
        opponents = _check_opponents(user, source, body.opponent_ids)
        match = _new_match("CHALLENGE", source, user.id, body.rounds, me, {"invited": [o["id"] for o in opponents]})
        for opponent in opponents:
            _invite(user, opponent, match)

    save_profile(user.id, {"active_match_id": int(match["id"])})
    return view(match["id"], user)


def _check_opponents(user, source: dict, opponent_ids: list[int]) -> list[dict]:
    out = []
    for opponent_id in dict.fromkeys(opponent_ids):
        if opponent_id == user.id:
            raise HTTPException(status_code=400, detail="You cannot challenge yourself.")
        doc = firestore_users.get_document(str(opponent_id))
        if not doc or doc.get("role") != UserRole.STUDENT.value or not doc.get("is_active", True):
            raise HTTPException(status_code=404, detail="That student was not found.")
        if source["scope"] == "CLASS" and student_class_id(opponent_id) != source["class_id"]:
            raise HTTPException(status_code=403, detail="Class battles are between classmates. Use a global challenge or a room code instead.")
        out.append({**doc, "id": int(opponent_id)})
    if len(out) + 1 > MAX_PLAYERS:
        raise HTTPException(status_code=400, detail=f"A battle holds at most {MAX_PLAYERS} players.")
    return out


def _invite(user, opponent: dict, match: dict) -> None:
    invite_id = firestore_arena_invites.get_next_numeric_id()
    firestore_arena_invites.add_document(str(invite_id), {
        "match_id": int(match["id"]), "from_id": user.id, "from_name": user.full_name,
        "to_id": int(opponent["id"]), "to_name": opponent.get("full_name"),
        "title": match.get("title"), "scope": match.get("scope"),
        "status": "PENDING", "created_at": _iso(_now()),
    })


# -------------------------------------------------------------------------- lobby moves

def _require_match(match_id: int) -> dict:
    match = firestore_arena_matches.get_document(str(match_id))
    if not match:
        raise HTTPException(status_code=404, detail="Battle not found")
    return match


def _join(match: dict, user) -> dict:
    phase = phase_at(match, [], _now())["phase"]
    if phase != LOBBY:
        raise HTTPException(status_code=409, detail="This battle has already started or ended.")
    seated = _players(match)
    if str(user.id) in seated:
        return match
    if len(seated) >= MAX_PLAYERS:
        raise HTTPException(status_code=409, detail="This room is full.")
    if match["scope"] == "CLASS" and student_class_id(user.id) != match.get("class_id"):
        raise HTTPException(status_code=403, detail="This is a class battle for another class.")
    profile = _assert_free(user.id)
    firestore_arena_matches.add_document(str(match["id"]), {"players": {str(user.id): _seat(_user_doc(user), profile)}})
    save_profile(user.id, {"active_match_id": int(match["id"])})
    return firestore_arena_matches.get_document(str(match["id"]))


def join_by_code(user, code: str) -> dict:
    rows = firestore_arena_matches.query_documents("code", "==", code.strip().upper())
    match = next((m for m in rows if phase_at(m, [], _now())["phase"] == LOBBY), None)
    if not match:
        raise HTTPException(status_code=404, detail="No open room has that code.")
    _join(match, user)
    return view(match["id"], user)


def accept_invite(user, invite_id: int) -> dict:
    invite = firestore_arena_invites.get_document(str(invite_id))
    if not invite or int(invite.get("to_id")) != user.id:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.get("status") != "PENDING" or _now() - (_parse(invite.get("created_at")) or _now()) > INVITE_TTL:
        raise HTTPException(status_code=409, detail="This challenge has expired.")
    match = _require_match(invite["match_id"])
    match = _join(match, user)
    firestore_arena_invites.add_document(str(invite_id), {"status": "ACCEPTED"})
    _maybe_autostart(match)
    return view(match["id"], user)


def decline_invite(user, invite_id: int) -> None:
    invite = firestore_arena_invites.get_document(str(invite_id))
    if not invite or int(invite.get("to_id")) != user.id:
        raise HTTPException(status_code=404, detail="Invite not found")
    firestore_arena_invites.add_document(str(invite_id), {"status": "DECLINED"})
    match = firestore_arena_matches.get_document(str(invite["match_id"]))
    if match:
        _maybe_autostart(match)


def _invites_of(match_id) -> list[dict]:
    return firestore_arena_invites.query_documents("match_id", "==", int(match_id))


def _maybe_autostart(match: dict) -> None:
    """A challenge starts once nobody it is waiting for is still deciding."""
    if match.get("mode") != "CHALLENGE" or match.get("status") != LOBBY:
        return
    invites = _invites_of(match["id"])
    now = _now()
    pending = [i for i in invites if i.get("status") == "PENDING"
               and now - (_parse(i.get("created_at")) or now) <= INVITE_TTL]
    if pending:
        return
    if len(_players(match)) >= 2:
        _start(match)
    elif invites and all(i.get("status") in ("DECLINED", "EXPIRED") for i in invites):
        firestore_arena_matches.add_document(str(match["id"]), {"status": CANCELLED, "cancel_reason": "DECLINED"})


def start_match(user, match_id: int) -> dict:
    match = _require_match(match_id)
    if match.get("host_id") != user.id:
        raise HTTPException(status_code=403, detail="Only the host can start.")
    if phase_at(match, [], _now())["phase"] != LOBBY:
        raise HTTPException(status_code=409, detail="This battle has already started or ended.")
    if len(_players(match)) < 2:
        raise HTTPException(status_code=409, detail="Wait for at least one more player.")
    _start(match)
    return view(match_id, user)


def leave_match(user, match_id: int) -> None:
    match = _require_match(match_id)
    uid = str(user.id)
    if uid not in _players(match):
        return
    if match.get("status") == LOBBY:
        if match.get("host_id") == user.id:
            firestore_arena_matches.add_document(str(match_id), {"status": CANCELLED, "cancel_reason": "HOST_LEFT"})
            for invite in _invites_of(match_id):
                if invite.get("status") == "PENDING":
                    firestore_arena_invites.add_document(str(invite["id"]), {"status": "EXPIRED"})
            for other in _humans(match):
                save_profile(int(other), {"active_match_id": None})
        else:
            firestore_arena_matches.add_document(str(match_id), {"players": {uid: None}})
    elif match.get("status") == LIVE:
        firestore_arena_matches.add_document(str(match_id), {"left": {uid: _iso(_now())}})
        refreshed = firestore_arena_matches.get_document(str(match_id))
        finalize_if_due(refreshed)
    save_profile(user.id, {"active_match_id": None})


def rematch(user, match_id: int) -> dict:
    """The same battle again with the same people (or the same bot)."""
    match = _require_match(match_id)
    if str(user.id) not in _players(match):
        raise HTTPException(status_code=403, detail="You were not in this battle.")
    if match.get("rematch_id"):
        existing = firestore_arena_matches.get_document(str(match["rematch_id"]))
        if existing and phase_at(existing, [], _now())["phase"] == LOBBY:
            _join(existing, user)
            _maybe_autostart(firestore_arena_matches.get_document(str(existing["id"])))
            return view(existing["id"], user)

    from app.schemas.arena import MatchCreate

    others = [int(uid) for uid in _humans(match) if int(uid) != user.id
              and not (match.get("left") or {}).get(uid)]
    body = MatchCreate(
        mode="BOT" if match.get("mode") == "BOT" or not others else "CHALLENGE",
        scope=match["scope"], subject_id=match.get("subject_id"), chapter_id=match.get("chapter_id"),
        challenge_id=match.get("challenge_id"), bot_level=match.get("bot_level") or "MEDIUM",
        opponent_ids=others, rounds=int(match.get("rounds") or 7),
    )
    created = create_match(user, body)
    firestore_arena_matches.add_document(str(match_id), {"rematch_id": created["id"]})
    return created


# ------------------------------------------------------------------------------ playing

def answer(user, match_id: int, round_index: int, option_key: str) -> dict:
    match = _require_match(match_id)
    uid = str(user.id)
    if uid not in _players(match):
        raise HTTPException(status_code=403, detail="You are not in this battle.")
    if (match.get("left") or {}).get(uid):
        raise HTTPException(status_code=409, detail="You left this battle.")
    answers = _answers_by_round(match_id)
    rounds = timeline(match, answers)
    now = _now()
    phase = phase_at(match, rounds, now)
    if phase["phase"] != "QUESTION" or phase["round"] != round_index:
        raise HTTPException(status_code=409, detail="Too late - that question has closed.")

    question = match["questions"][round_index]
    if option_key not in {o["key"] for o in question["options"]}:
        raise HTTPException(status_code=400, detail="That is not one of the options.")
    correct = option_key == question["correct_key"]
    elapsed = (now - rounds[round_index]["start"]).total_seconds()
    streak_before = 0
    for r in reversed(rounds[:round_index]):
        pick = r["picks"].get(uid)
        if not pick or not pick["correct"]:
            break
        streak_before += 1
    points = points_for(correct, elapsed, streak_before)

    created = firestore_arena_answers.create_document(f"{match_id}:{round_index}:{uid}", {
        "match_id": int(match_id), "round": round_index, "user_id": user.id,
        "option_key": option_key, "correct": correct, "points": points,
        "elapsed_ms": int(elapsed * 1000), "answered_at": _iso(now),
    })
    if not created:
        raise HTTPException(status_code=409, detail="You already answered this question.")
    return {"accepted": True, "correct": correct, "points": points, "streak": streak_before + 1 if correct else 0}


def emote(user, match_id: int, value: str) -> None:
    if value not in catalog.EMOTES:
        raise HTTPException(status_code=400, detail="Unknown emote.")
    match = _require_match(match_id)
    if str(user.id) not in _players(match):
        raise HTTPException(status_code=403, detail="You are not in this battle.")
    now = _now()
    firestore_arena_matches.add_document(str(match_id), {"emotes": {
        f"{int(now.timestamp() * 1000)}-{user.id}": {"user_id": user.id, "emote": value, "at": _iso(now)},
    }})


# --------------------------------------------------------------------------------- view

def finalize_if_due(match: dict) -> dict:
    from app.services.arena.rewards import finalize

    if match.get("status") != LIVE:
        return match
    answers = _answers_by_round(match["id"])
    rounds = timeline(match, answers)
    if phase_at(match, rounds, _now())["phase"] != FINISHED:
        return match
    return finalize(match, rounds)


def view(match_id: int, user) -> dict:
    """The match as one player sees it now. Answer keys only for rounds already over."""
    match = _require_match(match_id)
    uid = str(user.id)
    players = _players(match)
    if uid not in players and getattr(user, "role", None) != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="You are not in this battle.")

    now = _now()
    answers = _answers_by_round(match_id)
    rounds = timeline(match, answers)
    phase = phase_at(match, rounds, now)
    if phase["phase"] == FINISHED and match.get("status") == LIVE:
        match = finalize_if_due(match)
        phase = {"phase": FINISHED}

    current = phase.get("round")
    # Scores count only rounds whose answers are already revealed.
    revealed = (current + 1) if phase["phase"] == "REVEAL" else (current if current is not None else len(rounds))
    if phase["phase"] in ("COUNTDOWN", LOBBY):
        revealed = 0
    totals = scores(match, rounds, revealed)
    ranked = {s["user_id"]: s["rank"] for s in standings(match, totals)}
    left = match.get("left") or {}

    out_players = []
    for pid, p in players.items():
        current_pick = rounds[current]["picks"].get(pid) if current is not None and current < len(rounds) else None
        answered_now = bool(current_pick) and (current_pick["answered_at"] <= now)
        out_players.append({
            "id": pid,
            "user_id": p.get("user_id"),
            "name": p.get("name"),
            "photo_url": p.get("photo_url"),
            "look": p.get("look"),
            "level": p.get("level"),
            "is_bot": bool(p.get("is_bot")),
            "bot_level": p.get("bot_level"),
            "is_host": p.get("user_id") is not None and p.get("user_id") == match.get("host_id"),
            "is_me": pid == uid,
            "left": bool(left.get(pid)),
            "score": totals.get(pid, {}).get("score", 0),
            "correct": totals.get(pid, {}).get("correct", 0),
            "streak": totals.get(pid, {}).get("streak", 0),
            "rank": ranked.get(pid),
            "answered": answered_now if phase["phase"] == "QUESTION" else bool(current_pick),
        })
    out_players.sort(key=lambda p: (p["rank"] or 99, p["name"] or ""))

    out = {
        "id": int(match["id"]),
        "mode": match.get("mode"),
        "scope": match.get("scope"),
        "title": match.get("title"),
        "subtitle": match.get("subtitle"),
        "emoji": match.get("emoji"),
        "code": match.get("code"),
        "host_id": match.get("host_id"),
        "is_host": match.get("host_id") == user.id,
        "status": match.get("status"),
        "phase": phase["phase"],
        "server_now": _iso(now),
        "phase_starts_at": _iso(phase["starts_at"]) if phase.get("starts_at") else None,
        "phase_ends_at": _iso(phase["ends_at"]) if phase.get("ends_at") else None,
        "round": current,
        "round_count": len(match.get("questions") or []) or int(match.get("rounds") or 0),
        "question_seconds": QUESTION_S,
        "reveal_seconds": REVEAL_S,
        "players": out_players,
        "max_players": MAX_PLAYERS,
        "can_start": match.get("host_id") == user.id and phase["phase"] == LOBBY and len(players) >= 2,
        "question": None,
        "my_answer": None,
        "reveal": None,
        "results": None,
        "invites": [],
        "emotes": [],
        "rematch_id": match.get("rematch_id"),
        "cancel_reason": match.get("cancel_reason"),
    }

    if phase["phase"] == LOBBY:
        out["invites"] = [{"id": int(i["id"]), "to_id": i.get("to_id"), "name": i.get("to_name"),
                           "status": i.get("status")} for i in _invites_of(match_id)]

    if current is not None and current < len(rounds):
        q = match["questions"][current]
        out["question"] = {"index": current, "text": q["text"], "question_type": q["question_type"],
                           "options": q["options"], "difficulty": q["difficulty"]}
        mine = rounds[current]["picks"].get(uid)
        if mine:
            out["my_answer"] = {"option_key": mine["option_key"], "correct": mine["correct"], "points": mine["points"]}
        if phase["phase"] == "REVEAL":
            out["reveal"] = {
                "correct_key": q["correct_key"],
                "explanation": q.get("explanation"),
                "picks": {pid: {"option_key": a["option_key"], "correct": a["correct"], "points": a["points"]}
                          for pid, a in rounds[current]["picks"].items()},
            }

    if phase["phase"] == FINISHED:
        totals_all = scores(match, rounds)
        out["results"] = {
            "standings": standings(match, totals_all),
            "finalized": match.get("status") == FINISHED,
            "mine": (match.get("results") or {}).get(uid),
            "questions": [{"text": q["text"], "options": q["options"], "correct_key": q["correct_key"],
                           "explanation": q.get("explanation"),
                           "my_pick": (r["picks"].get(uid) or {}).get("option_key"),
                           "my_correct": bool((r["picks"].get(uid) or {}).get("correct"))}
                          for q, r in zip(match.get("questions") or [], rounds)],
        }
        for p in out["players"]:
            final = next((s for s in out["results"]["standings"] if s["user_id"] == p["id"]), None)
            if final:
                p.update(score=final["score"], correct=final["correct"], rank=final["rank"])

    recent = now - timedelta(seconds=EMOTE_WINDOW_S)
    for key, e in sorted((match.get("emotes") or {}).items()):
        at = _parse((e or {}).get("at"))
        if at and at >= recent:
            out["emotes"].append({"id": key, "user_id": e.get("user_id"), "emote": e.get("emote"), "at": e.get("at")})
    return out


# -------------------------------------------------------------------------- quick match

def _pool_key(source: dict) -> str:
    return "|".join(str(x) for x in (
        "q", source["scope"], source["class_id"] if source["scope"] == "CLASS" else "all",
        source["subject_id"] or "", source["chapter_id"] or "", source["challenge_id"] or ""))


def join_queue(user, body) -> dict:
    """Pairs the student with whoever is waiting on the same questions, or starts them waiting."""
    profile = _assert_free(user.id)
    source = _resolve_content(user, body.scope, body.subject_id, body.chapter_id, body.challenge_id)
    _assert_enough(source["pool"])
    key = _pool_key(source)
    now = _now()
    slot_data = {"user_id": user.id, "since": _iso(now), "seen_at": _iso(now), "pool": key,
                 "scope": source["scope"], "subject_id": source["subject_id"],
                 "chapter_id": source["chapter_id"], "challenge_id": source["challenge_id"]}

    for _ in range(3):
        if firestore_arena_queue.create_document(key, slot_data):
            save_profile(user.id, {"queue": {"pool": key, "since": slot_data["since"]}})
            return {"status": "WAITING", "since": slot_data["since"], "match_id": None}
        waiting = firestore_arena_queue.get_document(key)
        if not waiting:
            continue
        if int(waiting.get("user_id")) == user.id:
            firestore_arena_queue.add_document(key, {"seen_at": _iso(now)})
            return {"status": "WAITING", "since": waiting.get("since"), "match_id": None}
        seen = _parse(waiting.get("seen_at")) or now
        if (now - seen).total_seconds() > QUEUE_STALE_S:
            firestore_arena_queue.delete_document(key)
            continue
        # Exactly one newcomer gets to pair with this waiting student.
        claim = f"pair:{key}:{waiting.get('user_id')}:{waiting.get('since')}"
        if not firestore_arena_claims.create_document(claim, {"by": user.id, "at": _iso(now)}):
            continue
        firestore_arena_queue.delete_document(key)
        other_id = int(waiting["user_id"])
        other_doc = firestore_users.get_document(str(other_id))
        other_profile = get_profile(other_id)
        if not other_doc or other_profile.get("active_match_id"):
            continue
        players = {str(user.id): _seat(_user_doc(user), profile),
                   str(other_id): _seat({**other_doc, "id": other_id}, other_profile)}
        match = _new_match("QUICK", source, None, 7, players)
        match = _start(match)
        save_profile(user.id, {"active_match_id": int(match["id"]), "queue": None})
        save_profile(other_id, {"active_match_id": int(match["id"]), "queue": None})
        return {"status": "MATCHED", "since": None, "match_id": int(match["id"])}
    return {"status": "WAITING", "since": _iso(now), "match_id": None}


def poll_queue(user) -> dict:
    profile = get_profile(user.id)
    queued = profile.get("queue") or {}
    if profile.get("active_match_id"):
        match = firestore_arena_matches.get_document(str(profile["active_match_id"]))
        if match and match.get("mode") == "QUICK":
            save_profile(user.id, {"queue": None})
            return {"status": "MATCHED", "since": queued.get("since"), "match_id": int(match["id"])}
    key = queued.get("pool")
    if not key:
        return {"status": "IDLE", "since": None, "match_id": None}
    slot = firestore_arena_queue.get_document(key)
    if slot and int(slot.get("user_id")) == user.id:
        firestore_arena_queue.add_document(key, {"seen_at": _iso(_now())})
        return {"status": "WAITING", "since": slot.get("since"), "match_id": None}
    # Our slot is gone and no match arrived (the pairing student gave up); wait again.
    return {"status": "IDLE", "since": None, "match_id": None}


def leave_queue(user) -> None:
    profile = get_profile(user.id)
    key = (profile.get("queue") or {}).get("pool")
    if key:
        slot = firestore_arena_queue.get_document(key)
        if slot and int(slot.get("user_id")) == user.id:
            firestore_arena_queue.delete_document(key)
    save_profile(user.id, {"queue": None})


# ----------------------------------------------------------------------------- lobbyists

def pending_invites(user) -> list[dict]:
    now = _now()
    out = []
    for invite in firestore_arena_invites.query_documents("to_id", "==", user.id):
        created = _parse(invite.get("created_at")) or now
        if invite.get("status") != "PENDING" or now - created > INVITE_TTL:
            continue
        match = firestore_arena_matches.get_document(str(invite["match_id"]))
        if not match or phase_at(match, [], now)["phase"] != LOBBY:
            continue
        sender = firestore_users.get_document(str(invite["from_id"]))
        out.append({
            "id": int(invite["id"]), "match_id": int(invite["match_id"]),
            "from": player_summary({**sender, "id": int(invite["from_id"])} if sender else None,
                                   get_profile(int(invite["from_id"]), create=False)),
            "title": invite.get("title"), "scope": invite.get("scope"),
            "created_at": invite.get("created_at"),
            "expires_at": _iso(created + INVITE_TTL),
        })
    return out


def classmates(user) -> list[dict]:
    from app.services.presence import ONLINE_WINDOW_SECONDS

    class_id = require_student_class(user)
    ids = [int(e["student_id"]) for e in firestore_student_enrollments.query_documents("class_id", "==", class_id)
           if e.get("student_id") is not None and int(e["student_id"]) != user.id]
    users = firestore_users.get_documents(ids)
    presence = firestore_user_presence.get_documents(ids)
    from app.core.firebase import firestore_arena_profiles
    profiles = firestore_arena_profiles.get_documents(ids)
    class_doc = firestore_classes.get_document(str(class_id))
    cutoff = _now() - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    out = []
    for sid in ids:
        doc = users.get(str(sid))
        if not doc or not doc.get("is_active", True) or doc.get("role") != UserRole.STUDENT.value:
            continue
        seen = _parse((presence.get(str(sid)) or {}).get("last_seen_at"))
        profile = profiles.get(str(sid))
        out.append({
            **player_summary({**doc, "id": sid}, profile, {**class_doc, "id": class_id} if class_doc else None),
            "is_online": bool(seen and seen >= cutoff),
            "in_battle": bool((profile or {}).get("active_match_id")),
        })
    out.sort(key=lambda s: (not s["is_online"], s["full_name"]))
    return out
