"""
Video walkthroughs for the in-app User Guide.

The guide's text ships with the web app; its videos are links an admin adds afterwards, so
recording a new walkthrough never needs a release. One document holds every link, keyed by
the guide topic it belongs to. Everyone signed in may read them (a student's guide plays its
videos); only an admin may change them.
"""

import re
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.dependencies import require_admin, require_any_authenticated
from app.core.firebase import firestore_guide_content
from app.schemas.user import UserOut

guide_router = APIRouter(prefix="/guide", tags=["User Guide"])
guide_admin_router = APIRouter(prefix="/admin/guide", tags=["User Guide (Admin)"])

VIDEOS_DOC = "videos"
TOPIC_ID = re.compile(r"^[a-z0-9-]{1,80}$")
# A video plays inside the guide only from these: a OneDrive or SharePoint embed address, a
# Google Drive file, or YouTube.
PLAYABLE = re.compile(
    r"^https://("
    r"onedrive\.live\.com/embed\?|[a-z0-9-]+\.sharepoint\.com/.+/embed\.aspx\?"
    r"|drive\.google\.com/|www\.youtube\.com/|youtube\.com/|youtu\.be/|m\.youtube\.com/)",
    re.I,
)


class GuideVideosIn(BaseModel):
    """Every topic's link. A topic left out, or given an empty link, has no video."""
    videos: Dict[str, str] = Field(default_factory=dict)


class GuideVideosOut(BaseModel):
    videos: Dict[str, str] = Field(default_factory=dict)
    updated_at: str | None = None


def _stored() -> dict:
    return firestore_guide_content.get_document(VIDEOS_DOC) or {}


@guide_router.get("/videos", response_model=GuideVideosOut)
def guide_videos(_: UserOut = Depends(require_any_authenticated)):
    """The video link for each guide topic that has one."""
    doc = _stored()
    return GuideVideosOut(videos=doc.get("videos") or {}, updated_at=doc.get("updated_at"))


@guide_admin_router.put("/videos", response_model=GuideVideosOut)
def save_guide_videos(payload: GuideVideosIn, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Replace the guide's video links.

    Each link must be a OneDrive or SharePoint embed address, a Google Drive file or a YouTube
    video - the kinds the guide can play in the page. Empty links are dropped, so clearing a box removes that topic's video.
    """
    cleaned: dict[str, str] = {}
    problems = []
    for topic, link in payload.videos.items():
        url = (link or "").strip()
        if not url:
            continue
        if not TOPIC_ID.match(topic):
            problems.append(f"'{topic}' is not a guide topic")
            continue
        if len(url) > 2000 or not PLAYABLE.match(url):
            problems.append(f"the link for '{topic}' is not a OneDrive embed, Google Drive or YouTube link")
            continue
        cleaned[topic] = url
    if problems:
        raise HTTPException(status_code=400, detail="Could not save: " + "; ".join(problems) + ".")

    moment = datetime.now(timezone.utc).isoformat()
    # Replaced whole, so a link removed in the form is gone here too.
    firestore_guide_content.delete_document(VIDEOS_DOC)
    firestore_guide_content.add_document(VIDEOS_DOC, {"videos": cleaned, "updated_at": moment, "updated_by": admin.id})
    return GuideVideosOut(videos=cleaned, updated_at=moment)
