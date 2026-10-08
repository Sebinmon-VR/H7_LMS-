"""
What the arena hands out: levels, items, badges, bots and the global categories.

Kept in code rather than the database so a release can rebalance prices or add a badge
without a data migration, and so the frontend and backend never disagree about what an
item id means - the frontend reads all of this from GET /arena/catalog.
"""

# ---------------------------------------------------------------------------------- levels

MAX_LEVEL = 99


def xp_for_level(level: int) -> int:
    """Total XP needed to reach `level`: L2 = 100, L3 = 300, L5 = 1000, L10 = 4500."""
    level = max(1, level)
    return 50 * level * (level - 1)


def level_for_xp(xp: int) -> int:
    level = 1
    while level < MAX_LEVEL and xp >= xp_for_level(level + 1):
        level += 1
    return level


def level_progress(xp: int) -> dict:
    level = level_for_xp(xp)
    floor = xp_for_level(level)
    ceiling = xp_for_level(level + 1) if level < MAX_LEVEL else floor
    span = max(1, ceiling - floor)
    return {
        "level": level,
        "xp": xp,
        "level_xp": floor,
        "next_level_xp": ceiling,
        "progress": 1.0 if level >= MAX_LEVEL else round((xp - floor) / span, 4),
    }


# ------------------------------------------------------------------------------ categories

GLOBAL_CATEGORIES = [
    {"id": "GENERAL_KNOWLEDGE", "name": "General Knowledge", "emoji": "🌍"},
    {"id": "SCIENCE", "name": "Science", "emoji": "🔬"},
    {"id": "MATHS", "name": "Maths", "emoji": "➗"},
    {"id": "ENGLISH", "name": "English", "emoji": "📝"},
    {"id": "HISTORY", "name": "History", "emoji": "🏛️"},
    {"id": "GEOGRAPHY", "name": "Geography", "emoji": "🗺️"},
    {"id": "TECHNOLOGY", "name": "Technology", "emoji": "💻"},
    {"id": "SPORTS", "name": "Sports", "emoji": "🏏"},
    {"id": "ARTS", "name": "Arts & Culture", "emoji": "🎨"},
    {"id": "NATURE", "name": "Nature & Animals", "emoji": "🌿"},
]
CATEGORY_IDS = {c["id"] for c in GLOBAL_CATEGORIES}

# --------------------------------------------------------------------------------- bots

# accuracy: chance of a right answer; delay: seconds to answer, drawn uniformly.
BOTS = {
    "EASY": {"name": "Byte", "avatar": "🐣", "accuracy": 0.55, "delay": (6.0, 11.0), "level": 2},
    "MEDIUM": {"name": "Nova", "avatar": "🤖", "accuracy": 0.72, "delay": (4.0, 9.0), "level": 8},
    "HARD": {"name": "Titan", "avatar": "🦾", "accuracy": 0.88, "delay": (2.5, 6.5), "level": 20},
}

# --------------------------------------------------------------------------------- items

SLOTS = ("avatar", "frame", "title", "banner")
RARITIES = ("COMMON", "RARE", "EPIC", "LEGENDARY")


def _item(item_id, slot, name, rarity, price, *, emoji=None, text=None, colors=None,
          min_level=1, badge=None, character=None, hidden=False):
    return {
        "id": item_id, "slot": slot, "name": name, "rarity": rarity, "price": price,
        "emoji": emoji, "text": text, "colors": colors or [],
        "min_level": min_level, "required_badge": badge,
        # A hero's art key (/arena/heroes/{character}-full.webp); emoji is the fallback.
        "character": character,
        # Retired items stay equippable for whoever owns them but leave the shop.
        "hidden": hidden,
    }


def _hero(item_id, name, rarity, price, emoji, min_level=1):
    return _item(item_id, "avatar", name, rarity, price, emoji=emoji, min_level=min_level,
                 character=item_id.removeprefix("hero_"))


ITEMS = [
    # Heroes: the frosted-jelly art-toy line - who you play as, everywhere.
    _hero("hero_ace", "Ace", "COMMON", 0, "🏀"),
    _hero("hero_nova", "Nova", "COMMON", 0, "🧑‍🚀"),
    _hero("hero_glitch", "Glitch", "RARE", 150, "🎧", min_level=2),
    _hero("hero_shade", "Shade", "RARE", 220, "🥷", min_level=3),
    _hero("hero_valor", "Valor", "RARE", 260, "🛡️", min_level=4),
    _hero("hero_arcane", "Arcane", "EPIC", 340, "🧙", min_level=5),
    _hero("hero_captain", "Captain", "EPIC", 380, "🏴‍☠️", min_level=6),
    _hero("hero_titan", "Titan", "EPIC", 450, "🤖", min_level=7),
    _hero("hero_frost", "Frost", "LEGENDARY", 650, "❄️", min_level=8),
    _hero("hero_blaze", "Blaze", "LEGENDARY", 800, "🐉", min_level=10),

    # Earlier characters, retired from the shop.
    _item("avatar_student", "avatar", "Student", "COMMON", 0, emoji="🧑‍🎓", hidden=True),
    _item("avatar_technologist", "avatar", "Coder", "COMMON", 60, emoji="🧑‍💻", hidden=True),
    _item("avatar_artist", "avatar", "Artist", "COMMON", 60, emoji="🧑‍🎨", hidden=True),
    _item("avatar_scientist", "avatar", "Scientist", "COMMON", 80, emoji="🧑‍🔬", hidden=True),
    _item("avatar_detective", "avatar", "Detective", "COMMON", 100, emoji="🕵️", hidden=True),
    _item("avatar_pilot", "avatar", "Pilot", "COMMON", 100, emoji="🧑‍✈️", hidden=True),
    _item("avatar_firefighter", "avatar", "Firefighter", "COMMON", 100, emoji="🧑‍🚒", hidden=True),
    _item("avatar_guard", "avatar", "Royal Guard", "RARE", 140, emoji="💂", min_level=2, hidden=True),
    _item("avatar_ninja", "avatar", "Ninja", "RARE", 200, emoji="🥷", min_level=3, hidden=True),
    _item("avatar_astronaut", "avatar", "Astronaut", "RARE", 250, emoji="🧑‍🚀", min_level=4, hidden=True),
    _item("avatar_elf", "avatar", "Elf", "RARE", 260, emoji="🧝", min_level=4, hidden=True),
    _item("avatar_fairy", "avatar", "Fairy", "RARE", 260, emoji="🧚", min_level=4, hidden=True),
    _item("avatar_prince", "avatar", "Prince", "EPIC", 320, emoji="🤴", min_level=5, hidden=True),
    _item("avatar_princess", "avatar", "Princess", "EPIC", 320, emoji="👸", min_level=5, hidden=True),
    _item("avatar_zombie", "avatar", "Zombie", "EPIC", 360, emoji="🧟", min_level=6, hidden=True),
    _item("avatar_superhero", "avatar", "Superhero", "EPIC", 420, emoji="🦸", min_level=6, hidden=True),
    _item("avatar_vampire", "avatar", "Vampire", "EPIC", 450, emoji="🧛", min_level=7, hidden=True),
    _item("avatar_genie", "avatar", "Genie", "EPIC", 480, emoji="🧞", min_level=7, hidden=True),
    _item("avatar_troll", "avatar", "Troll", "LEGENDARY", 600, emoji="🧌", min_level=8, hidden=True),
    _item("avatar_supervillain", "avatar", "Supervillain", "LEGENDARY", 900, emoji="🦹", min_level=10, hidden=True),

    # Creatures: the original avatar line, still collectible.
    _item("avatar_owl", "avatar", "Wise Owl", "COMMON", 40, emoji="🦉", hidden=True),
    _item("avatar_fox", "avatar", "Clever Fox", "COMMON", 50, emoji="🦊", hidden=True),
    _item("avatar_panda", "avatar", "Panda", "COMMON", 50, emoji="🐼", hidden=True),
    _item("avatar_frog", "avatar", "Frog", "COMMON", 50, emoji="🐸", hidden=True),
    _item("avatar_penguin", "avatar", "Penguin", "COMMON", 60, emoji="🐧", hidden=True),
    _item("avatar_koala", "avatar", "Koala", "COMMON", 60, emoji="🐨", hidden=True),
    _item("avatar_tiger", "avatar", "Tiger", "RARE", 120, emoji="🐯", min_level=3, hidden=True),
    _item("avatar_octopus", "avatar", "Octopus", "RARE", 140, emoji="🐙", min_level=3, hidden=True),
    _item("avatar_robot", "avatar", "Robot", "RARE", 160, emoji="🤖", min_level=4, hidden=True),
    _item("avatar_lion", "avatar", "Lion", "RARE", 200, emoji="🦁", min_level=5, hidden=True),
    _item("avatar_alien", "avatar", "Alien", "EPIC", 260, emoji="👽", min_level=6, hidden=True),
    _item("avatar_unicorn", "avatar", "Unicorn", "EPIC", 300, emoji="🦄", min_level=7, hidden=True),
    _item("avatar_trex", "avatar", "T-Rex", "EPIC", 360, emoji="🦖", min_level=8, hidden=True),
    _item("avatar_wizard", "avatar", "Wizard", "EPIC", 450, emoji="🧙", min_level=10, hidden=True),
    _item("avatar_dragon", "avatar", "Dragon", "LEGENDARY", 800, emoji="🐉", min_level=15, hidden=True),
    _item("avatar_eagle", "avatar", "Golden Eagle", "LEGENDARY", 1000, emoji="🦅", min_level=12,
          badge="streak_10"),

    # Frames: the ring round the avatar. Two colours make the gradient.
    _item("frame_basic", "frame", "Classic", "COMMON", 0, colors=["#94a3b8", "#cbd5e1"]),
    _item("frame_bronze", "frame", "Bronze", "COMMON", 60, colors=["#b45309", "#f59e0b"]),
    _item("frame_mint", "frame", "Mint", "COMMON", 80, colors=["#10b981", "#6ee7b7"]),
    _item("frame_silver", "frame", "Silver", "RARE", 150, colors=["#64748b", "#e2e8f0"], min_level=3),
    _item("frame_ruby", "frame", "Ruby", "RARE", 220, colors=["#be123c", "#fb7185"], min_level=4),
    _item("frame_sapphire", "frame", "Sapphire", "RARE", 220, colors=["#1d4ed8", "#60a5fa"], min_level=4),
    _item("frame_gold", "frame", "Gold", "EPIC", 350, colors=["#ca8a04", "#fde047"], min_level=6),
    _item("frame_rainbow", "frame", "Rainbow", "EPIC", 500, colors=["#ef4444", "#eab308", "#22c55e", "#3b82f6", "#a855f7"], min_level=8),
    _item("frame_flame", "frame", "Flame", "EPIC", 600, colors=["#dc2626", "#f97316", "#facc15"], badge="streak_5"),
    _item("frame_galaxy", "frame", "Galaxy", "LEGENDARY", 900, colors=["#312e81", "#7c3aed", "#ec4899"], min_level=12),
    _item("frame_legend", "frame", "Legend", "LEGENDARY", 1500, colors=["#0f172a", "#facc15", "#0f172a"], badge="wins_100"),

    # Titles: the line under the name.
    _item("title_rookie", "title", "Rookie", "COMMON", 0, text="Rookie"),
    _item("title_quiz_kid", "title", "Quiz Kid", "COMMON", 40, text="Quiz Kid"),
    _item("title_bookworm", "title", "Bookworm", "COMMON", 80, text="Bookworm"),
    _item("title_brainiac", "title", "Brainiac", "RARE", 150, text="Brainiac", min_level=4),
    _item("title_whiz", "title", "Whiz", "RARE", 150, text="Whiz", min_level=4),
    _item("title_speed", "title", "Speed Demon", "RARE", 200, text="Speed Demon", badge="lightning"),
    _item("title_flawless", "title", "Flawless", "EPIC", 300, text="Flawless", badge="flawless"),
    _item("title_mastermind", "title", "Mastermind", "EPIC", 350, text="Mastermind", min_level=8),
    _item("title_professor", "title", "Professor", "EPIC", 400, text="Professor", badge="correct_500"),
    _item("title_legend", "title", "Legend", "LEGENDARY", 1000, text="Legend", badge="wins_100"),

    # Banners: the background of the profile card.
    _item("banner_sky", "banner", "Sky", "COMMON", 0, colors=["#38bdf8", "#818cf8"]),
    _item("banner_sunset", "banner", "Sunset", "COMMON", 80, colors=["#f97316", "#ec4899"]),
    _item("banner_ocean", "banner", "Ocean", "COMMON", 80, colors=["#0ea5e9", "#14b8a6"]),
    _item("banner_forest", "banner", "Forest", "COMMON", 80, colors=["#16a34a", "#84cc16"]),
    _item("banner_candy", "banner", "Candy", "RARE", 140, colors=["#f472b6", "#a78bfa"], min_level=3),
    _item("banner_night", "banner", "Night Sky", "RARE", 220, colors=["#1e1b4b", "#4338ca"], min_level=5),
    _item("banner_lava", "banner", "Lava", "EPIC", 400, colors=["#7f1d1d", "#ea580c"], min_level=8),
    _item("banner_aurora", "banner", "Aurora", "EPIC", 450, colors=["#064e3b", "#22d3ee", "#a855f7"], min_level=10),
    _item("banner_gold", "banner", "Hall of Fame", "LEGENDARY", 1200, colors=["#78350f", "#facc15", "#fef08a"], min_level=15),
]
ITEMS_BY_ID = {item["id"]: item for item in ITEMS}
STARTER_ITEMS = {"avatar": "hero_ace", "frame": "frame_basic", "title": "title_rookie", "banner": "banner_sky"}
# Earlier starter avatars. Profiles still wearing one (and never having picked an avatar)
# are moved to the current starter, and keep the old one.
LEGACY_STARTER_AVATARS = {"avatar_owl", "avatar_student"}
# Everything free and on sale is owned by everyone from the start.
FREE_ITEMS = [i["id"] for i in ITEMS if i["price"] == 0 and not i["hidden"]]

# Battle reactions. Free for everyone; sent with POST /arena/matches/{id}/emote.
EMOTES = ["👍", "😎", "🔥", "😮", "😂", "👏", "🤝", "GG"]

# ---------------------------------------------------------------------------------- badges

# `stat` and `goal` describe progress for locked badges (a value on the profile, or None for
# one-off feats). `coins` is the one-time prize.
def _badge(badge_id, name, emoji, description, rarity, coins, stat=None, goal=None):
    return {
        "id": badge_id, "name": name, "emoji": emoji, "description": description,
        "rarity": rarity, "coins": coins, "stat": stat, "goal": goal,
    }


BADGES = [
    _badge("first_battle", "First Battle", "⚔️", "Play your first battle.", "COMMON", 20, "matches", 1),
    _badge("first_win", "First Victory", "🏆", "Win a battle.", "COMMON", 25, "wins", 1),
    _badge("wins_10", "Warrior", "🛡️", "Win 10 battles.", "RARE", 50, "wins", 10),
    _badge("wins_50", "Champion", "👑", "Win 50 battles.", "EPIC", 150, "wins", 50),
    _badge("wins_100", "Legend", "🌟", "Win 100 battles.", "LEGENDARY", 300, "wins", 100),
    _badge("streak_3", "On Fire", "🔥", "Win 3 battles in a row.", "COMMON", 30, "best_win_streak", 3),
    _badge("streak_5", "Unstoppable", "⚡", "Win 5 battles in a row.", "RARE", 75, "best_win_streak", 5),
    _badge("streak_10", "Godlike", "💫", "Win 10 battles in a row.", "LEGENDARY", 250, "best_win_streak", 10),
    _badge("flawless", "Flawless", "💎", "Answer every question right in a battle of 5 or more.", "RARE", 60),
    _badge("lightning", "Lightning", "⚡", "Answer correctly in under 2 seconds.", "COMMON", 30),
    _badge("correct_100", "Scholar", "📘", "Answer 100 questions correctly.", "RARE", 50, "correct", 100),
    _badge("correct_500", "Professor", "🎓", "Answer 500 questions correctly.", "EPIC", 150, "correct", 500),
    _badge("correct_1000", "Genius", "🧠", "Answer 1,000 questions correctly.", "LEGENDARY", 300, "correct", 1000),
    _badge("level_5", "Rising Star", "⭐", "Reach level 5.", "COMMON", 40, "level", 5),
    _badge("level_10", "Expert", "🌠", "Reach level 10.", "RARE", 100, "level", 10),
    _badge("level_25", "Grandmaster", "🏅", "Reach level 25.", "LEGENDARY", 300, "level", 25),
    _badge("bot_breaker", "Bot Breaker", "🦾", "Beat Titan, the hard bot.", "RARE", 60),
    _badge("giant_slayer", "Giant Slayer", "🗡️", "Beat a player 5 or more levels above you.", "EPIC", 100),
    _badge("comeback", "Comeback Kid", "🔄", "Win after trailing going into the last question.", "RARE", 60),
    _badge("friendly_rival", "Friendly Rival", "🤝", "Play 10 battles against classmates.", "COMMON", 40, "human_matches", 10),
    _badge("party_winner", "Party King", "🎉", "Win a room of 4 or more players.", "EPIC", 100),
    _badge("world_explorer", "World Explorer", "🌍", "Play 10 global challenge battles.", "RARE", 50, "global_matches", 10),
    _badge("dedicated", "Dedicated", "📅", "Battle 3 days in a row.", "COMMON", 30, "best_day_streak", 3),
    _badge("devoted", "Devoted", "🗓️", "Battle 7 days in a row.", "EPIC", 120, "best_day_streak", 7),
    _badge("collector", "Collector", "🎒", "Own 10 items.", "RARE", 60, "items_owned", 10),
    _badge("lab_first", "First Experiment", "🧪", "Complete an experiment in the virtual lab.", "COMMON", 25, "experiments", 1),
    _badge("lab_scientist", "Lab Scientist", "🔬", "Complete 5 different lab experiments.", "RARE", 80, "experiments", 5),
    _badge("lab_master", "Master Chemist", "⚗️", "Complete 12 different lab experiments.", "EPIC", 200, "experiments", 12),
    _badge("top_of_class", "Top of the Class", "🥇", "Reach number 1 on your class's weekly board.", "EPIC", 150),
]
BADGES_BY_ID = {badge["id"]: badge for badge in BADGES}
