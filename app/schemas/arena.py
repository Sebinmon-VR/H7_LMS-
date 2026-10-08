"""Request bodies for the arena. Responses are plain dicts shaped in app/services/arena."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

QuestionTypeLiteral = Literal["MCQ", "TRUE_FALSE"]
MatchMode = Literal["BOT", "CHALLENGE", "ROOM"]
MatchScope = Literal["CLASS", "GLOBAL"]
BotLevel = Literal["EASY", "MEDIUM", "HARD"]


# ------------------------------------------------------------------------------- content

class ChapterCreate(BaseModel):
    class_id: int
    subject_id: int
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)


class ChapterUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)
    is_active: bool | None = None


class ChapterReorder(BaseModel):
    class_id: int
    subject_id: int
    chapter_ids: list[int] = Field(..., min_length=1)


class ChapterCopy(BaseModel):
    from_class_id: int
    subject_id: int
    to_class_id: int
    include_questions: bool = True


class QuestionOptionIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=8)
    text: str = Field(..., min_length=1, max_length=300)


class QuestionIn(BaseModel):
    """
    One question. A CLASS question names its `chapter_id`; a GLOBAL one names a `category`.
    TRUE_FALSE questions may omit `options` - TRUE and FALSE are filled in.
    """
    chapter_id: int | None = None
    category: str | None = None
    question_type: QuestionTypeLiteral = "MCQ"
    text: str = Field(..., min_length=1, max_length=600)
    options: list[QuestionOptionIn] = Field(default_factory=list)
    correct_key: str = Field(..., min_length=1, max_length=8)
    explanation: str | None = Field(None, max_length=1000)
    difficulty: int = Field(1, ge=1, le=3)
    is_active: bool = True

    @model_validator(mode="after")
    def _check(self) -> "QuestionIn":
        if self.question_type == "TRUE_FALSE":
            self.options = [QuestionOptionIn(key="TRUE", text="True"),
                            QuestionOptionIn(key="FALSE", text="False")]
            self.correct_key = self.correct_key.strip().upper()
            if self.correct_key in ("T", "YES"):
                self.correct_key = "TRUE"
            if self.correct_key in ("F", "NO"):
                self.correct_key = "FALSE"
        else:
            if not 2 <= len(self.options) <= 6:
                raise ValueError("A multiple-choice question needs 2 to 6 options.")
            keys = [o.key for o in self.options]
            if len(set(keys)) != len(keys):
                raise ValueError("Option keys must be unique.")
            if len({o.text.strip().lower() for o in self.options}) != len(self.options):
                raise ValueError("Two options have the same text.")
        if self.correct_key not in {o.key for o in self.options}:
            raise ValueError("The correct answer must be one of the options.")
        return self


class QuestionUpdate(BaseModel):
    question_type: QuestionTypeLiteral | None = None
    text: str | None = Field(None, min_length=1, max_length=600)
    options: list[QuestionOptionIn] | None = None
    correct_key: str | None = None
    explanation: str | None = Field(None, max_length=1000)
    difficulty: int | None = Field(None, ge=1, le=3)
    category: str | None = None
    is_active: bool | None = None


class QuestionBulk(BaseModel):
    """Many questions for one chapter (or one global category, per question)."""
    chapter_id: int | None = None
    category: str | None = None
    questions: list[QuestionIn] = Field(..., min_length=1, max_length=500)


class ChallengeIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=1000)
    emoji: str = Field("🌍", max_length=16)
    categories: list[str] = Field(default_factory=list)
    difficulty: int | None = Field(None, ge=1, le=3)
    rounds: int = Field(7, ge=3, le=15)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = True


class ChallengeUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=1000)
    emoji: str | None = Field(None, max_length=16)
    categories: list[str] | None = None
    difficulty: int | None = Field(None, ge=1, le=3)
    rounds: int | None = Field(None, ge=3, le=15)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None


# ------------------------------------------------------------------------------- battles

class MatchCreate(BaseModel):
    mode: MatchMode
    scope: MatchScope = "CLASS"
    subject_id: int | None = None
    chapter_id: int | None = None
    challenge_id: int | None = None
    bot_level: BotLevel = "MEDIUM"
    # CHALLENGE: the classmate(s) invited.
    opponent_ids: list[int] = Field(default_factory=list, max_length=7)
    rounds: int = Field(7, ge=3, le=15)

    @model_validator(mode="after")
    def _check(self) -> "MatchCreate":
        if self.scope == "GLOBAL" and self.challenge_id is None:
            raise ValueError("A global battle needs a challenge.")
        if self.mode == "CHALLENGE" and not self.opponent_ids:
            raise ValueError("Choose who to challenge.")
        return self


class QueueJoin(BaseModel):
    scope: MatchScope = "CLASS"
    subject_id: int | None = None
    chapter_id: int | None = None
    challenge_id: int | None = None


class JoinByCode(BaseModel):
    code: str = Field(..., min_length=4, max_length=8)


class AnswerIn(BaseModel):
    round: int = Field(..., ge=0)
    option_key: str = Field(..., min_length=1, max_length=8)


class EmoteIn(BaseModel):
    emote: str = Field(..., min_length=1, max_length=8)


class ItemChoice(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=64)
