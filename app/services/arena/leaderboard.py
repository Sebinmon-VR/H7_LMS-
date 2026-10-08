"""
Leaderboards: one row per (board, period, student), ranked by XP, then wins, then whoever
got there first.

Boards are `class:{id}` (class battles), `global` (every global battle) and
`challenge:{id}`. Periods are `all` and the school-timezone ISO week, `w2026-41`, so the
weekly board resets on Monday morning at school without anybody running a job.
"""
from datetime import datetime

from app.core.concurrency import run_parallel
from app.core.firebase import (
    firestore_arena_points, firestore_arena_profiles, firestore_classes, firestore_users,
)
from app.services.arena.profiles import player_summary

TOP_N = 50


def week_key(at: datetime | None = None) -> str:
    from app.services.timetable import now_local

    local = now_local() if at is None else at
    year, week, _ = local.isocalendar()
    return f"w{year}-{week:02d}"


def period_key(period: str) -> str:
    return "all" if period == "all" else week_key()


def _row_id(board: str, period: str, student_id: int) -> str:
    return f"{board}|{period}|{student_id}"


def add_points(boards: list[str], student_id: int, class_id: int | None, xp: int, won: bool) -> None:
    """Adds one finished battle to every board and period it counts on."""
    now = datetime.utcnow().isoformat()
    for board in boards:
        for period in ("all", week_key()):
            row_id = _row_id(board, period, student_id)
            current = firestore_arena_points.get_document(row_id) or {}
            firestore_arena_points.add_document(row_id, {
                "board": board,
                "period": period,
                "board_period": f"{board}|{period}",
                "student_id": student_id,
                "class_id": class_id,
                "xp": int(current.get("xp") or 0) + xp,
                "wins": int(current.get("wins") or 0) + (1 if won else 0),
                "matches": int(current.get("matches") or 0) + 1,
                # The tie-break: the earlier you reached your total, the higher you rank.
                "updated_at": now,
            })


def _ranked(board: str, period: str) -> list[dict]:
    rows = firestore_arena_points.query_documents("board_period", "==", f"{board}|{period}")
    rows = [r for r in rows if int(r.get("xp") or 0) > 0 or int(r.get("matches") or 0) > 0]
    rows.sort(key=lambda r: (-int(r.get("xp") or 0), -int(r.get("wins") or 0), r.get("updated_at") or ""))
    rank = 0
    previous = None
    for index, row in enumerate(rows, start=1):
        key = (int(row.get("xp") or 0), int(row.get("wins") or 0))
        if key != previous:
            rank = index
            previous = key
        row["rank"] = rank
    return rows


def rank_of(board: str, period: str, student_id: int) -> dict | None:
    rows = _ranked(board, period)
    for row in rows:
        if int(row.get("student_id")) == int(student_id):
            return {"rank": row["rank"], "of": len(rows), "xp": int(row.get("xp") or 0)}
    return None


def board(board_id: str, period: str, viewer_id: int | None = None) -> dict:
    """The top of a board, hydrated, plus the viewer's own row wherever it falls."""
    key = period_key(period)
    rows = _ranked(board_id, key)
    top = rows[:TOP_N]
    mine = None
    if viewer_id is not None:
        mine = next((r for r in rows if int(r.get("student_id")) == int(viewer_id)), None)

    wanted = {int(r["student_id"]) for r in top} | ({int(mine["student_id"])} if mine else set())
    class_ids = {r.get("class_id") for r in top if r.get("class_id") is not None}
    got = run_parallel({
        "users": lambda: firestore_users.get_documents(list(wanted)),
        "profiles": lambda: firestore_arena_profiles.get_documents(list(wanted)),
        "classes": lambda: firestore_classes.get_documents(list(class_ids)),
    })
    users, profiles, classes = got["users"] or {}, got["profiles"] or {}, got["classes"] or {}

    def entry(row: dict) -> dict:
        sid = str(row["student_id"])
        user = users.get(sid)
        class_doc = classes.get(str(row.get("class_id"))) if row.get("class_id") is not None else None
        return {
            **player_summary({**user, "id": int(sid)} if user else None, profiles.get(sid),
                             {**class_doc, "id": row.get("class_id")} if class_doc else None),
            "student_id": int(sid),
            "rank": row["rank"],
            "points": int(row.get("xp") or 0),
            "wins": int(row.get("wins") or 0),
            "matches": int(row.get("matches") or 0),
            "is_me": viewer_id is not None and int(sid) == int(viewer_id),
        }

    return {
        "board": board_id,
        "period": period,
        "period_key": key,
        "total": len(rows),
        "entries": [entry(r) for r in top],
        "me": entry(mine) if mine else None,
    }


# Topping a board of one is not an achievement.
LEADER_MIN_PLAYERS = 3


def is_weekly_leader(class_id: int, student_id: int) -> bool:
    rows = _ranked(f"class:{class_id}", week_key())
    return (len(rows) >= LEADER_MIN_PLAYERS and rows[0]["rank"] == 1
            and int(rows[0]["student_id"]) == int(student_id))
