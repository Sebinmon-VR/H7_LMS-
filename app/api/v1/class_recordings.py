"""
Class recordings, as their teacher reviews and publishes them.

A finished recording is the teacher's to review first. Publishing files it into the class
library (study materials of type RECORDING) under its class and subject, where only students
of that class can watch it. Each teacher sets their own rule for new recordings: wait for
review (the default), or publish automatically. See app/services/recordings.py.
"""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.v1.dependencies import require_teacher
from app.core import azure_blob
from app.core.firebase import firestore_recording_log, firestore_users
from app.schemas.user import UserOut
from app.services import recordings

router = APIRouter(prefix="/recordings", tags=["Class Recordings"])


class RecordingFileOut(BaseModel):
    file_url: str | None = None
    name: str | None = None
    size_bytes: int | None = None
    started_at: str | None = None


class ClassRecordingOut(BaseModel):
    key: str
    title: str | None = None
    default_title: str
    status: str | None = None
    error: str | None = None
    class_id: int | None = None
    class_name: str | None = None
    subject_id: int | None = None
    subject_name: str | None = None
    meeting_id: int | None = None
    started_by: int | None = None
    started_by_name: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    duration_ms: int | None = None
    published: bool = False
    published_at: datetime | None = None
    material_ids: list[int] = []
    files: list[RecordingFileOut] = []


class PublishIn(BaseModel):
    title: str | None = None
    subject_id: int | None = None


class RecordingPreferences(BaseModel):
    # Publish each new recording to the class library as soon as it is ready, instead of
    # waiting for review. Applies to recordings this teacher makes.
    auto_publish: bool = False


def _require(key: str, user: UserOut) -> dict:
    record = recordings.get_record(key)
    if record is None or not recordings.may_manage(record, user):
        raise HTTPException(status_code=404, detail="Recording not found")
    return record


@router.get("/mine", response_model=List[ClassRecordingOut])
def my_recordings(user: UserOut = Depends(require_teacher)):
    """
    [Teacher] My class recordings, newest first: recording now, being prepared, ready to
    review, or published. Class teachers also see their class's; admins see all.
    """
    return [ClassRecordingOut(**recordings.present(r)) for r in recordings.recordings_for(user)]


@router.post("/{key}/publish", response_model=ClassRecordingOut)
def publish_recording(key: str, payload: PublishIn = PublishIn(), user: UserOut = Depends(require_teacher)):
    """
    [Teacher] Publish a ready recording to its class library, under its subject. The title
    and subject may be changed on the way; publishing again only renames it.
    """
    record = _require(key, user)
    try:
        updated = recordings.publish(record, actor_id=user.id, title=payload.title, subject_id=payload.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ClassRecordingOut(**recordings.present(updated))


@router.post("/{key}/unpublish", response_model=ClassRecordingOut)
def unpublish_recording(key: str, user: UserOut = Depends(require_teacher)):
    """[Teacher] Take a recording back out of the class library. The video is kept."""
    record = _require(key, user)
    return ClassRecordingOut(**recordings.present(recordings.unpublish(record)))


@router.delete("/{key}", status_code=204)
def discard_recording(key: str, user: UserOut = Depends(require_teacher)):
    """[Teacher] Delete a recording for good: out of the library, and the video itself."""
    record = _require(key, user)
    if record.get("status") == recordings.LOG_RECORDING:
        raise HTTPException(status_code=409, detail="Stop the recording before deleting it.")
    recordings.unpublish(record)
    for file in record.get("files") or []:
        name = azure_blob.blob_name_from(file.get("file_url"))
        if name:
            azure_blob.delete(name)
    firestore_recording_log.delete_document(key)
    return None


@router.get("/preferences", response_model=RecordingPreferences)
def get_preferences(user: UserOut = Depends(require_teacher)):
    profile = firestore_users.get_document(str(user.id)) or {}
    return RecordingPreferences(auto_publish=bool(profile.get("recording_auto_publish")))


@router.put("/preferences", response_model=RecordingPreferences)
def set_preferences(payload: RecordingPreferences, user: UserOut = Depends(require_teacher)):
    """[Teacher] My rule for new recordings: wait for my review, or publish at once."""
    firestore_users.add_document(str(user.id), {"recording_auto_publish": payload.auto_publish})
    return payload
