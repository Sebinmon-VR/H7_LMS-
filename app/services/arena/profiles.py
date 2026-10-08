"""
A student's arena profile: XP, coins, items, badges and stats, and the card others see.

The profile is created lazily on first visit with the starter items equipped. Only the
student's own match finalisations and shop actions write to it, one at a time, so a plain
merge is safe.
"""
from datetime import datetime

from fastapi import HTTPException

from app.core.firebase import (
    firestore_arena_points, firestore_arena_profiles, firestore_classes,
    firestore_student_enrollments, firestore_subjects, firestore_users,
)
from app.services.arena import catalog

STAT_KEYS = (
    "matches", "wins", "losses", "draws", "correct", "answered", "win_streak", "best_win_streak",
    "perfect_matches", "human_matches", "global_matches", "bot_wins", "day_streak", "best_day_streak",
    "experiments",
)


def student_class_id(student_id: int) -> int | None:
    """
    The class a student battles and ranks in: their most recent enrollment.

    A student can hold more than one enrollment (a move between sections leaves the old
    row behind), and the store returns them in id order, so "the first" would be the
    oldest class - the one they have left.
    """
    enrollments = firestore_student_enrollments.query_documents("student_id", "==", student_id)
    if not enrollments:
        return None
    latest = max(enrollments, key=lambda e: (e.get("enrolled_at") or "", str(e.get("id"))))
    return latest.get("class_id")


def require_student_class(user) -> int:
    class_id = student_class_id(user.id)
    if class_id is None:
        raise HTTPException(status_code=409, detail="You are not enrolled in a class yet, so there is no class arena to play in.")
    return class_id


def _blank(student_id: int, class_id: int | None) -> dict:
    return {
        "student_id": student_id,
        "class_id": class_id,
        "xp": 0,
        "coins": 50,  # enough for a first avatar, so the shop means something on day one
        **{key: 0 for key in STAT_KEYS},
        "owned": list(dict.fromkeys([*catalog.STARTER_ITEMS.values(), *catalog.FREE_ITEMS])),
        "equipped": dict(catalog.STARTER_ITEMS),
        "badges": {},
        "subject_correct": {},
        "active_match_id": None,
        "created_at": datetime.utcnow().isoformat(),
    }


def get_profile(student_id: int, create: bool = True) -> dict | None:
    profile = firestore_arena_profiles.get_document(str(student_id))
    if profile is None:
        if not create:
            return None
        profile = _blank(student_id, student_class_id(student_id))
        firestore_arena_profiles.add_document(str(student_id), profile)
        profile = {**profile, "id": student_id}
    for key in STAT_KEYS:
        profile.setdefault(key, 0)
    profile.setdefault("owned", list(catalog.STARTER_ITEMS.values()))
    profile.setdefault("equipped", dict(catalog.STARTER_ITEMS))
    profile.setdefault("badges", {})
    _upgrade_starters(student_id, profile)
    return profile


def _upgrade_starters(student_id: int, profile: dict) -> None:
    """
    Every starter item is owned by everyone, including starters added after a profile was
    made; and a player still wearing the old default avatar gets the new default character.
    """
    owned = list(profile.get("owned") or [])
    missing = [i for i in dict.fromkeys([*catalog.STARTER_ITEMS.values(), *catalog.FREE_ITEMS]) if i not in owned]
    equipped = dict(profile.get("equipped") or {})
    swap = (equipped.get("avatar") is None or equipped.get("avatar") in catalog.LEGACY_STARTER_AVATARS)         and not profile.get("avatar_chosen")
    if not missing and not swap:
        return
    fields = {}
    if missing:
        fields["owned"] = owned + missing
    if swap:
        fields["equipped"] = {**equipped, "avatar": catalog.STARTER_ITEMS["avatar"]}
    if profile.get("id") is not None or firestore_arena_profiles.get_document(str(student_id)):
        save_profile(student_id, fields)
    profile.update(fields)


def save_profile(student_id: int, fields: dict) -> None:
    firestore_arena_profiles.add_document(str(student_id), fields)


def equipped_look(profile: dict | None) -> dict:
    """The four equipped items, resolved, for anywhere a player is drawn."""
    equipped = {**catalog.STARTER_ITEMS, **((profile or {}).get("equipped") or {})}
    look = {}
    for slot in catalog.SLOTS:
        item = catalog.ITEMS_BY_ID.get(equipped.get(slot)) or catalog.ITEMS_BY_ID[catalog.STARTER_ITEMS[slot]]
        look[slot] = {"id": item["id"], "name": item["name"], "emoji": item["emoji"],
                      "text": item["text"], "colors": item["colors"], "rarity": item["rarity"],
                      "character": item.get("character")}
    return look


def player_summary(user_doc: dict | None, profile: dict | None, class_doc: dict | None = None) -> dict:
    """The compact form used in leaderboards, classmates lists and battles."""
    xp = int((profile or {}).get("xp") or 0)
    return {
        "student_id": int(user_doc["id"]) if user_doc else (profile or {}).get("student_id"),
        "full_name": (user_doc or {}).get("full_name") or "Student",
        "photo_url": (user_doc or {}).get("photo_url"),
        "class_id": (class_doc or {}).get("id") or (profile or {}).get("class_id"),
        "class_name": (class_doc or {}).get("name"),
        "level": catalog.level_for_xp(xp),
        "xp": xp,
        "look": equipped_look(profile),
        "badge_count": len((profile or {}).get("badges") or {}),
    }


def badges_out(profile: dict) -> list[dict]:
    earned = profile.get("badges") or {}
    stats = {**{k: profile.get(k, 0) for k in STAT_KEYS},
             "level": catalog.level_for_xp(int(profile.get("xp") or 0)),
             "items_owned": len(profile.get("owned") or [])}
    out = []
    for badge in catalog.BADGES:
        got = earned.get(badge["id"])
        progress = None
        if badge["stat"] and badge["goal"]:
            progress = {"value": min(int(stats.get(badge["stat"]) or 0), badge["goal"]), "goal": badge["goal"]}
        out.append({**{k: badge[k] for k in ("id", "name", "emoji", "description", "rarity", "coins")},
                    "earned": bool(got), "earned_at": (got or {}).get("earned_at"), "progress": progress})
    return out


def stats_out(profile: dict) -> dict:
    answered = int(profile.get("answered") or 0)
    correct = int(profile.get("correct") or 0)
    return {
        **{k: int(profile.get(k) or 0) for k in STAT_KEYS},
        "accuracy": round(correct / answered, 4) if answered else None,
    }


def profile_card(student_id: int) -> dict:
    """Everything the profile card shows: look, level, stats, badges, collection, ranks."""
    user_doc = firestore_users.get_document(str(student_id))
    if not user_doc:
        raise HTTPException(status_code=404, detail="Student not found")
    profile = get_profile(student_id, create=False) or _blank(student_id, student_class_id(student_id))
    class_id = student_class_id(student_id)
    class_doc = firestore_classes.get_document(str(class_id)) if class_id is not None else None
    if class_doc:
        class_doc = {**class_doc, "id": class_id}

    subject_correct = profile.get("subject_correct") or {}
    top_subjects = []
    for subject_id, count in sorted(subject_correct.items(), key=lambda kv: -int(kv[1] or 0))[:3]:
        subject = firestore_subjects.get_document(str(subject_id))
        if subject:
            top_subjects.append({"subject_id": int(subject_id), "name": subject.get("name"), "correct": int(count)})

    owned = [catalog.ITEMS_BY_ID[i] for i in (profile.get("owned") or []) if i in catalog.ITEMS_BY_ID]
    return {
        **player_summary({**user_doc, "id": student_id}, profile, class_doc),
        **catalog.level_progress(int(profile.get("xp") or 0)),
        "stats": stats_out(profile),
        "badges": [b for b in badges_out(profile) if b["earned"]],
        "items": [{"id": i["id"], "slot": i["slot"], "name": i["name"], "emoji": i["emoji"],
                   "text": i["text"], "colors": i["colors"], "rarity": i["rarity"],
                   "character": i.get("character")} for i in owned],
        "top_subjects": top_subjects,
        "ranks": _ranks(student_id, class_id),
        "last_played_at": profile.get("last_played_at"),
    }


def _ranks(student_id: int, class_id: int | None) -> dict:
    """Where the student stands on the all-time boards they appear on."""
    from app.services.arena.leaderboard import rank_of

    ranks = {}
    if class_id is not None:
        ranks["class"] = rank_of(f"class:{class_id}", "all", student_id)
    ranks["global"] = rank_of("global", "all", student_id)
    return ranks


# ------------------------------------------------------------------------- shop and equip

def item_lock_reason(item: dict, profile: dict) -> str | None:
    level = catalog.level_for_xp(int(profile.get("xp") or 0))
    if item["min_level"] > level:
        return f"Reach level {item['min_level']}"
    if item["required_badge"] and item["required_badge"] not in (profile.get("badges") or {}):
        badge = catalog.BADGES_BY_ID.get(item["required_badge"])
        return f"Earn the {badge['name'] if badge else item['required_badge']} badge"
    return None


def catalog_for(profile: dict) -> dict:
    owned = set(profile.get("owned") or [])
    equipped = profile.get("equipped") or {}
    items = []
    for item in catalog.ITEMS:
        if item.get("hidden") and item["id"] not in owned:
            continue
        lock = None if item["id"] in owned else item_lock_reason(item, profile)
        items.append({**item, "owned": item["id"] in owned,
                      "equipped": equipped.get(item["slot"]) == item["id"],
                      "locked": lock is not None, "lock_reason": lock,
                      "affordable": int(profile.get("coins") or 0) >= item["price"]})
    return {
        "items": items,
        "badges": badges_out(profile),
        "emotes": catalog.EMOTES,
        "categories": catalog.GLOBAL_CATEGORIES,
        "slots": list(catalog.SLOTS),
        "rarities": list(catalog.RARITIES),
    }


def buy(student_id: int, item_id: str) -> dict:
    item = catalog.ITEMS_BY_ID.get(item_id)
    if not item or item.get("hidden"):
        raise HTTPException(status_code=404, detail="That item is not in the shop.")
    profile = get_profile(student_id)
    owned = list(profile.get("owned") or [])
    if item_id in owned:
        raise HTTPException(status_code=409, detail="You already own this.")
    lock = item_lock_reason(item, profile)
    if lock:
        raise HTTPException(status_code=403, detail=f"Locked: {lock}.")
    coins = int(profile.get("coins") or 0)
    if coins < item["price"]:
        raise HTTPException(status_code=402, detail=f"You need {item['price'] - coins} more coins.")
    owned.append(item_id)
    equipped = {**(profile.get("equipped") or {}), item["slot"]: item_id}
    fields = {"coins": coins - item["price"], "owned": owned, "equipped": equipped}
    if item["slot"] == "avatar":
        fields["avatar_chosen"] = True
    save_profile(student_id, fields)
    profile.update(fields)

    # Owning things is itself a badge.
    from app.services.arena.rewards import award_collection_badges
    award_collection_badges(student_id, profile)
    return get_profile(student_id)


def equip(student_id: int, item_id: str) -> dict:
    item = catalog.ITEMS_BY_ID.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="That item does not exist.")
    profile = get_profile(student_id)
    if item_id not in (profile.get("owned") or []):
        raise HTTPException(status_code=403, detail="Buy or unlock this first.")
    fields = {"equipped": {item["slot"]: item_id}}
    if item["slot"] == "avatar":
        fields["avatar_chosen"] = True
    save_profile(student_id, fields)
    return get_profile(student_id)


def my_profile_out(profile: dict, user, class_doc: dict | None = None) -> dict:
    """The student's own view: everything on the card plus coins and owned items."""
    class_id = profile.get("class_id") or student_class_id(user.id)
    if class_doc is None and class_id is not None:
        class_doc = firestore_classes.get_document(str(class_id))
    return {
        **player_summary({"id": user.id, "full_name": user.full_name, "photo_url": getattr(user, "photo_url", None)},
                         profile, {**class_doc, "id": class_id} if class_doc else None),
        **catalog.level_progress(int(profile.get("xp") or 0)),
        "coins": int(profile.get("coins") or 0),
        "stats": stats_out(profile),
        "owned": profile.get("owned") or [],
        "equipped": profile.get("equipped") or {},
        "badges_earned": len(profile.get("badges") or {}),
        "active_match_id": profile.get("active_match_id"),
    }


def points_rows_for(student_id: int) -> list[dict]:
    return firestore_arena_points.query_documents("student_id", "==", student_id)
