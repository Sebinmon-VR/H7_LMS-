"""
Closing a battle: standings, XP and coins, stats, badges and leaderboard points.

Runs exactly once per match - whichever poll first sees the battle over claims
`finalize:{match}` with an atomic create - and writes the outcome onto the match so every
player's results screen reads the same numbers.
"""
import logging
from datetime import datetime, timedelta

from app.core.firebase import firestore_arena_claims, firestore_arena_matches
from app.services.arena import catalog
from app.services.arena.leaderboard import add_points, is_weekly_leader
from app.services.arena.profiles import STAT_KEYS, get_profile, save_profile, student_class_id

logger = logging.getLogger("arena")

RECENT_KEPT = 12
# A claim this old with no results written belongs to a worker that died mid-way.
CLAIM_STALE = timedelta(seconds=30)


def _school_today():
    from app.services.timetable import now_local

    return now_local().date()


def _stat_badges(profile: dict) -> list[str]:
    stats = {**{k: int(profile.get(k) or 0) for k in STAT_KEYS},
             "level": catalog.level_for_xp(int(profile.get("xp") or 0)),
             "items_owned": len(profile.get("owned") or [])}
    return [b["id"] for b in catalog.BADGES
            if b["stat"] and b["goal"] and int(stats.get(b["stat"]) or 0) >= b["goal"]]


def _grant(profile: dict, badge_ids: list[str]) -> tuple[list[dict], int]:
    """Adds the badges not yet held; returns the new ones and the coins they carry."""
    held = dict(profile.get("badges") or {})
    new, coins = [], 0
    now = datetime.utcnow().isoformat()
    for badge_id in dict.fromkeys(badge_ids):
        if badge_id in held or badge_id not in catalog.BADGES_BY_ID:
            continue
        badge = catalog.BADGES_BY_ID[badge_id]
        held[badge_id] = {"earned_at": now}
        coins += badge["coins"]
        new.append({k: badge[k] for k in ("id", "name", "emoji", "description", "rarity", "coins")})
    profile["badges"] = held
    return new, coins


def award_collection_badges(student_id: int, profile: dict) -> list[dict]:
    new, coins = _grant(profile, _stat_badges(profile))
    if new:
        save_profile(student_id, {"badges": profile["badges"],
                                  "coins": int(profile.get("coins") or 0) + coins})
    return new


def finalize(match: dict, rounds: list[dict]) -> dict:
    from app.services.arena.engine import FINISHED, _humans, _players, scores, standings

    claim_id = f"finalize:{match['id']}"
    if not firestore_arena_claims.create_document(claim_id, {"at": datetime.utcnow().isoformat()}):
        claim = firestore_arena_claims.get_document(claim_id) or {}
        claimed = datetime.fromisoformat(claim["at"]) if claim.get("at") else datetime.utcnow()
        fresh = firestore_arena_matches.get_document(str(match["id"])) or match
        if fresh.get("status") == FINISHED or datetime.utcnow() - claimed < CLAIM_STALE:
            return fresh
        firestore_arena_claims.delete_document(claim_id)
        if not firestore_arena_claims.create_document(claim_id, {"at": datetime.utcnow().isoformat()}):
            return fresh

    players = _players(match)
    totals = scores(match, rounds)
    table = standings(match, totals)
    rank_of = {s["user_id"]: s["rank"] for s in table}
    left = match.get("left") or {}
    # Whoever forfeited cannot win; if everyone else left, the one who stayed wins.
    staying = [s for s in table if not left.get(s["user_id"])]
    top_score = staying[0]["score"] if staying else None
    leaders = [s["user_id"] for s in staying if s["score"] == top_score]
    bots = [p for p in players.values() if p.get("is_bot")]
    vs_bot = bool(bots)
    humans = _humans(match)
    questions = match.get("questions") or []
    boards = ([f"class:{match['class_id']}"] if match["scope"] == "CLASS"
              else ["global", f"challenge:{match['challenge_id']}"])
    # Scores going into the last question, for the comeback badge.
    before_last = scores(match, rounds, max(0, len(rounds) - 1)) if len(rounds) > 1 else totals

    results = {}
    for uid in humans:
        try:
            results[uid] = _reward_player(
                match, uid, rounds, questions, totals, rank_of, leaders, left, vs_bot, bots, humans,
                players, boards, before_last,
            )
        except Exception:  # noqa: BLE001 - one player's failure must not cost the others their rewards
            logger.exception("Arena rewards failed for player %s in match %s", uid, match["id"])

    fields = {"status": FINISHED, "finished_at": datetime.utcnow().isoformat(),
              "results": results, "standings": table}
    firestore_arena_matches.add_document(str(match["id"]), fields)
    return {**match, **fields}


def _reward_player(match, uid, rounds, questions, totals, rank_of, leaders, left, vs_bot, bots,
                   humans, players, boards, before_last) -> dict:
    student_id = int(uid)
    profile = get_profile(student_id)
    level_before = catalog.level_for_xp(int(profile.get("xp") or 0))
    forfeit = bool(left.get(uid))
    picks = [r["picks"].get(uid) for r in rounds]
    correct_rounds = [i for i, p in enumerate(picks) if p and p["correct"]]
    correct = len(correct_rounds)
    answered = sum(1 for p in picks if p)

    if forfeit:
        outcome = "FORFEIT"
    elif uid in leaders and len(leaders) > 1:
        outcome = "DRAW"
    elif uid in leaders:
        outcome = "WIN"
    else:
        outcome = "LOSS"
    won = outcome == "WIN"
    perfect = len(questions) >= 5 and correct == len(questions)

    xp = sum(10 * int(questions[i].get("difficulty") or 1) for i in correct_rounds)
    coins = 0
    if not forfeit:
        coins = 5 + 3 * correct
        if won:
            xp += 15 if vs_bot else 30
            coins += 10 if vs_bot else 20
        elif outcome == "DRAW":
            coins += 5 if vs_bot else 10
        if perfect:
            xp += 20

    # ---- stats
    stats = {k: int(profile.get(k) or 0) for k in STAT_KEYS}
    stats["matches"] += 1
    stats["correct"] += correct
    stats["answered"] += answered
    if won:
        stats["wins"] += 1
        stats["win_streak"] += 1
        if vs_bot:
            stats["bot_wins"] += 1
    else:
        stats["win_streak"] = 0
        if outcome == "DRAW":
            stats["draws"] += 1
        else:
            stats["losses"] += 1
    stats["best_win_streak"] = max(stats["best_win_streak"], stats["win_streak"])
    if perfect:
        stats["perfect_matches"] += 1
    if len(humans) > 1:
        stats["human_matches"] += 1
    if match["scope"] == "GLOBAL":
        stats["global_matches"] += 1

    today = _school_today()
    last_day = profile.get("last_play_date")
    if last_day != today.isoformat():
        yesterday = (today - timedelta(days=1)).isoformat()
        stats["day_streak"] = stats["day_streak"] + 1 if last_day == yesterday else 1
    stats["best_day_streak"] = max(stats["best_day_streak"], stats["day_streak"])

    subject_correct = dict(profile.get("subject_correct") or {})
    for i in correct_rounds:
        sid = questions[i].get("subject_id")
        if sid is not None:
            subject_correct[str(sid)] = int(subject_correct.get(str(sid)) or 0) + 1

    new_xp = int(profile.get("xp") or 0) + xp
    profile.update(stats)
    profile["xp"] = new_xp

    # ---- leaderboards, before the badges so "top of the class" sees this battle. A global
    # battle can mix classes, so each row carries the player's own class, not the host's.
    own_class = student_class_id(student_id)
    add_points(boards, student_id, own_class, xp, won)

    # ---- badges
    feats = []
    if any(p and p["correct"] and p["elapsed"] < 2.0 for p in picks):
        feats.append("lightning")
    if perfect:
        feats.append("flawless")
    if won and any(b.get("bot_level") == "HARD" for b in bots):
        feats.append("bot_breaker")
    if won:
        my_level = players[uid].get("level") or 1
        if any(not p.get("is_bot") and pid != uid and (p.get("level") or 1) >= my_level + 5
               for pid, p in players.items()):
            feats.append("giant_slayer")
        if len(rounds) > 1:
            mine = before_last.get(uid, {}).get("score", 0)
            if any(v["score"] > mine for pid, v in before_last.items() if pid != uid):
                feats.append("comeback")
        if len(players) >= 4:
            feats.append("party_winner")
    if match["scope"] == "CLASS" and match.get("class_id") is not None \
            and is_weekly_leader(match["class_id"], student_id):
        feats.append("top_of_class")
    new_badges, badge_coins = _grant(profile, feats + _stat_badges(profile))

    level_after = catalog.level_for_xp(new_xp)
    opponents = [p.get("name") for pid, p in players.items() if pid != uid]
    recent = [{
        "match_id": int(match["id"]), "title": match.get("title"), "mode": match.get("mode"),
        "scope": match.get("scope"), "outcome": outcome, "rank": rank_of.get(uid),
        "players": len(players), "score": totals.get(uid, {}).get("score", 0), "correct": correct,
        "rounds": len(questions), "xp": xp, "coins": coins + badge_coins, "opponents": opponents,
        "finished_at": datetime.utcnow().isoformat(),
    }] + list(profile.get("recent") or [])

    save_profile(student_id, {
        **stats,
        "xp": new_xp,
        "coins": int(profile.get("coins") or 0) + coins + badge_coins,
        "badges": profile["badges"],
        "subject_correct": subject_correct,
        "last_play_date": today.isoformat(),
        "last_played_at": datetime.utcnow().isoformat(),
        "recent": recent[:RECENT_KEPT],
        "class_id": own_class,
        **({"active_match_id": None} if profile.get("active_match_id") in (None, int(match["id"])) else {}),
    })
    return {
        "outcome": outcome, "rank": rank_of.get(uid), "score": totals.get(uid, {}).get("score", 0),
        "correct": correct, "rounds": len(questions), "xp": xp, "coins": coins,
        "badge_coins": badge_coins, "new_badges": new_badges,
        "level_before": level_before, "level_after": level_after, "level_up": level_after > level_before,
        **{f"progress_{k}": v for k, v in catalog.level_progress(new_xp).items()},
    }
