# Arena: quiz battles, rewards and leaderboards

Students battle each other (or a bot) on questions from their own class syllabus, or on the
school-wide global question bank. Battles earn XP and coins; coins unlock profile items;
milestones award badges; XP fills a class leaderboard (class battles) and a global leaderboard
(global challenges). Everything here is LMS-side (school), not tuition.

## 1. Content

### Syllabus chapters (`syllabus_chapters`)
One ordered list of chapters per class and subject.

| field | |
|---|---|
| `class_id`, `subject_id` | the class-subject pair it belongs to |
| `title`, `description` | |
| `order` | position in the list, 1-based |
| `is_active` | inactive chapters are hidden from students |
| `created_by`, `created_at`, `updated_at` | |

Teachers manage the pairs they are mapped to in `teacher_subject_class_mappings`; admins
manage every class and subject. "Copy chapters" duplicates a pair's chapters (and optionally
questions) into another class.

### Question bank (`question_bank`)
| field | |
|---|---|
| `scope` | `CLASS` (belongs to a chapter) or `GLOBAL` (school-wide pool) |
| `class_id`, `subject_id`, `chapter_id` | for CLASS questions |
| `category` | for GLOBAL questions: one of the fixed categories below |
| `question_type` | `MCQ` (2-6 options, one correct) or `TRUE_FALSE` |
| `text`, `options[{key,text}]`, `correct_key`, `explanation` | |
| `difficulty` | 1 easy, 2 medium, 3 hard |
| `is_active`, `created_by`, `created_at`, `seed_key` | `seed_key` marks starter-pack rows |

Global categories: `GENERAL_KNOWLEDGE, SCIENCE, MATHS, ENGLISH, HISTORY, GEOGRAPHY,
TECHNOLOGY, SPORTS, ARTS, NATURE`.

Only admins write GLOBAL questions. Answer keys never leave the server until a round is over.

### Global challenges (`arena_challenges`)
Admin-made themed competitions drawing on the global bank: `title, description, emoji,
categories[] (empty = all), difficulty (null = mixed), rounds, starts_at?, ends_at?,
is_active`. Each has its own leaderboard on top of the overall global one. A starter pack
(admin button) installs ~80 global questions and four challenges.

## 2. Battles

### Modes
| mode | how it starts |
|---|---|
| `BOT` | instant 1v1 against a bot: Easy (55%, slow), Medium (70%), Hard (88%, fast) |
| `CHALLENGE` | invite a classmate; starts the moment they accept |
| `ROOM` | private room with a 6-letter code, 2-8 players, host presses start |
| `QUICK` | matchmaking with anyone waiting on the same pool; offer a bot after ~20 s |

`scope` is `CLASS` (subject and chapter optional; everyone must be in the same class) or
`GLOBAL` (a challenge; any student in the school).

### Timeline (derived, never stored)
The match stores `started_at` and the questions; each answer is its own document. The phase
is computed from those and the clock, so two players answering at once can never conflict and
any server worker can answer any poll.

```
started_at ─ 3 s countdown ─ Q1 (15 s, ends early once everyone answered) ─ 4 s reveal ─ Q2 ...
```

Clients poll `GET /arena/matches/{id}` about once a second and interpolate the timer from
`server_now`. Answers are timestamped by the server, so polling delay never costs points.

### Scoring
* Correct answer: `500 + 500 × time_left / 15 s`, plus a streak bonus of `+50` per consecutive
  correct answer before it (max `+200`). Wrong or no answer: 0.
* Highest score wins; equal scores share the place.

### Rewards (once per match, claimed atomically when the match ends)
* **XP**: `10 × difficulty` per correct answer, `+30` for a win against people (`+15` against
  a bot), `+20` for a perfect match. XP drives levels and leaderboards.
* **Coins**: `5 + 3 × correct`, `+20` for a win against people (`+10` against a bot, `+10/+5`
  for a draw). Badges add a one-off coin prize. Coins only buy cosmetics.
* **Level**: reaching level L needs `50 × L × (L − 1)` XP in total (L2 = 100, L3 = 300,
  L5 = 1000, L10 = 4500).

## 3. Profiles, items and badges

`arena_profiles` (id = student id): XP, coins, level, match stats, streaks, owned items,
equipped items (`avatar`, `frame`, `title`, `banner`), badges, per-subject correct counts, the
active match.

Items live in a catalogue in code (`app/services/arena/catalog.py`): four slots, each item with
a rarity, a coin price, and optionally a minimum level or a badge needed to unlock it. A free
starter item in each slot is owned by everyone.

Badges are also catalogued in code and checked after every match: first battle and first win,
win counts, win streaks, flawless matches, lightning answers, correct-answer milestones,
levels, beating the Hard bot, giant slaying, comebacks, global play, daily streaks, collecting,
and topping the class weekly board.

The profile card (any leaderboard row, any opponent) shows avatar and frame, banner, title,
name, class, level and progress, stats, badges and the item collection.

## 4. Leaderboards

`arena_points`: one row per (board, period, student), id `{board}|{period}|{student_id}`.

| board | who earns on it | who can see it |
|---|---|---|
| `class:{class_id}` | CLASS battles of that class | its students; its teachers; admins |
| `global` | every GLOBAL battle | every student, teacher and admin |
| `challenge:{id}` | battles in that challenge | same as global |

Periods: `all` and `w{ISO-year}-{week}` (school timezone, weeks start Monday). Rank by XP,
then wins, then whoever got there first. Each board returns the top 50 and the viewer's own
row.

Students only ever see their own class's board; the class comes from their enrollment, never
from the request.

## 5. API

Student (`/arena`, students and admins):

| method | path | |
|---|---|---|
| GET | `/arena/me` | profile, playable subjects and chapters, challenges, invites, active match, recent matches |
| GET | `/arena/catalog` | items and badges with my lock state |
| POST | `/arena/shop/buy` | `{item_id}` |
| POST | `/arena/equip` | `{item_id}` |
| GET | `/arena/classmates` | online state, level, avatar |
| POST | `/arena/matches` | `{mode, scope, subject_id?, chapter_id?, challenge_id?, bot_level?, opponent_id?, rounds?}` |
| POST | `/arena/matches/join` | `{code}` |
| GET | `/arena/matches/{id}` | the viewer's live view |
| POST | `/arena/matches/{id}/start` | host, ROOM |
| POST | `/arena/matches/{id}/answer` | `{round, option_key}` → own correctness at once |
| POST | `/arena/matches/{id}/emote` | `{emote}` |
| POST | `/arena/matches/{id}/leave` | |
| POST | `/arena/matches/{id}/rematch` | new match, same settings, same people |
| POST | `/arena/invites/{id}/accept`, `/decline` | |
| POST/GET/DELETE | `/arena/queue` | quick match |
| GET | `/arena/matches` | my recent matches |
| GET | `/arena/leaderboard` | `board=class\|global\|challenge`, `period=week\|all`, `challenge_id?`, `class_id?` (staff) |
| GET | `/arena/profiles/{student_id}` | profile card |

Teacher and admin (`/arena/manage`):

| method | path | |
|---|---|---|
| GET | `/arena/manage/scopes` | class-subject pairs I can manage |
| GET/POST | `/arena/manage/chapters` | list by `class_id`+`subject_id`; create |
| PUT/DELETE | `/arena/manage/chapters/{id}` | |
| POST | `/arena/manage/chapters/reorder`, `/copy` | |
| GET/POST | `/arena/manage/questions` | by `chapter_id`, or `scope=GLOBAL&category=` |
| POST | `/arena/manage/questions/bulk` | many at once (paste/CSV on the client) |
| PUT/DELETE | `/arena/manage/questions/{id}` | |
| GET/POST/PUT/DELETE | `/arena/manage/challenges[/{id}]` | admin |
| POST | `/arena/manage/starter-pack` | admin, idempotent |

## 6. Screens

Student, under **Arena** in the sidebar:
* **Arena home**: profile strip (avatar, level bar, coins), incoming challenges, the play
  modes (class battle by subject/chapter, global challenges, play with friends, practice
  against a bot), resume an active battle, recent battles.
* **Battle**: lobby (players, room code, invites, start), countdown, question with a draining
  timer and big colour-coded answer tiles, opponents' "answered" ticks, reveal with everyone's
  picks and points, emotes, final podium with XP, coins, level-ups and new badges, rematch.
* **Leaderboards**: Class and Global tabs, This week / All time, challenge filter, top three
  podium, my row pinned; tapping anyone opens their profile card.
* **Shop & locker**: items per slot with rarity, price and lock reason; buy and equip, with
  a live preview of my card.
* **Badges**: earned and locked, with progress.

Teacher: **Syllabus & questions** (their class-subject pairs) and **Arena leaderboards**
(their classes). Admin: the same for every class, plus **Global questions**, **Global
challenges** and the starter pack.

## 7. Later
* WebSockets (Azure Web PubSub) instead of polling once usage grows.
* AI-drafted questions from chapter text, reviewed by the teacher.
* More question types (type-the-answer, ordering, images).
* Seasons that reset ranks and award season badges.
