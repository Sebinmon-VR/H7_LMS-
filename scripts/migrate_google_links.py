"""
Moves the data that pointed at Google over to its Azure equivalents.

When the Workspace was retired, three kinds of stored link stopped working:

  * Meet rooms and session links the LMS generated. These are re-pointed at the LMS's own
    call pages (/call/class/<id>, /call/meeting/<id>, /call/tuition/<id>); the ACS room behind
    each is made the first time somebody joins.
  * Files on Google Drive or Cloud Storage. Their bytes went with the Workspace, so they are
    reported (and, with --mark-lost, labelled on the record so the app can say why a file
    will not open). Re-upload them from wherever the teacher still has a copy.
  * Files saved to the API server's disk (/uploads/...), which is where uploads landed while
    Drive was failing. With --upload-local those found on this machine's LOCAL_STORAGE_DIR are
    copied into Blob storage and their records re-pointed. Run it on the App Service (SSH)
    to reach the files that live there.

Links a person pasted by hand (meet_status MANUAL) are left alone by default: they may be a
personal Meet that still works. --convert-manual-meet moves any hand-pasted meet.google.com
link to the LMS call page as well.

Usage:
    python -m scripts.migrate_google_links                 # report only
    python -m scripts.migrate_google_links --apply         # re-point generated links
    python -m scripts.migrate_google_links --apply --upload-local --mark-lost
    python -m scripts.migrate_google_links --apply --convert-manual-meet

Idempotent: anything already moved is skipped on the next run.
"""

import argparse
import logging
import mimetypes
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import acs  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.firebase import (  # noqa: E402
    firestore_classes, firestore_exam_submissions, firestore_exams, firestore_materials,
    firestore_meetings, firestore_topics, firestore_tuition_library, firestore_tuition_sessions,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

LOST_WARNING = (
    "This file was stored on the school's Google Drive / Cloud Storage, which no longer exists. "
    "Upload it again."
)

MEET_FIELDS_CLEARED = {
    "google_event_id": None, "google_calendar_id": None, "meet_space_name": None,
    "meet_meeting_code": None, "meet_owner_email": None, "meet_access_type": None,
    "meet_access_error": None, "meet_error": None, "acs_room_id": None,
}


def is_meet(link) -> bool:
    return "meet.google.com" in str(link or "").lower()


def is_google_file(url) -> bool:
    text = str(url or "").lower()
    return any(host in text for host in ("drive.google.com", "docs.google.com", "storage.googleapis.com"))


class Migration:
    def __init__(self, apply: bool, convert_manual: bool, upload_local: bool, mark_lost: bool):
        self.apply = apply
        self.convert_manual = convert_manual
        self.upload_local = upload_local
        self.mark_lost = mark_lost
        self.counts: dict[str, int] = {}

    def note(self, key: str, detail: str | None = None) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1
        if detail:
            print(f"  {key:<34} {detail}")

    # -- rooms and links -----------------------------------------------------------------
    def classes(self) -> None:
        for room in firestore_classes.list_all():
            if room.get("room_provider") != "GOOGLE_MEET" and not is_meet(room.get("room_link")):
                continue
            self.note("class room -> LMS room", room.get("name") or f"class {room['id']}")
            if self.apply:
                firestore_classes.add_document(str(room["id"]), {
                    "room_link": acs.call_link(acs.KIND_CLASS, room["id"]),
                    "room_provider": acs.PROVIDER,
                    "room_status": "CREATED",
                    "room_error": None,
                    "room_acs_room_id": None,
                    "room_acs_valid_until": None,
                    "room_event_id": None, "room_calendar_id": None,
                    "room_owner_id": None, "room_owner_email": None,
                    "room_space_name": None, "room_meeting_code": None,
                    "room_guest_emails": None, "room_guest_error": None,
                    "room_access_type": None, "room_access_error": None,
                    "room_recording_status": "ARMED" if room.get("room_auto_record") else "NOT_REQUESTED",
                    "room_recording_error": None,
                })

    def meetings(self) -> None:
        for meeting in firestore_meetings.list_all():
            link = meeting.get("meeting_link")
            if not is_meet(link):
                continue
            status = meeting.get("meet_status")
            if status == "MANUAL" and not self.convert_manual:
                self.note("session link pasted by hand (kept)")
                continue
            if meeting.get("uses_class_room") and meeting.get("class_id") is not None:
                updates = {"meeting_link": acs.call_link(acs.KIND_CLASS, meeting["class_id"]),
                           "meet_status": "CLASS_ROOM"}
                label = "session -> class room"
            else:
                updates = {"meeting_link": acs.call_link(acs.KIND_MEETING, meeting["id"]),
                           "meet_status": "CREATED"}
                label = "session -> LMS room"
            if meeting.get("recording_status") == "ARM_FAILED":
                updates["recording_status"] = "ARMED" if meeting.get("auto_record") else "NOT_REQUESTED"
            self.note(label, f"{meeting.get('title')} ({str(meeting.get('scheduled_time'))[:16]})")
            if self.apply:
                firestore_meetings.add_document(str(meeting["id"]), {**MEET_FIELDS_CLEARED, **updates})

    def tuition(self) -> None:
        for session in firestore_tuition_sessions.list_all():
            if not is_meet(session.get("meeting_link")):
                continue
            if session.get("meet_status") == "MANUAL" and not self.convert_manual:
                self.note("tuition link pasted by hand (kept)")
                continue
            self.note("tuition class -> LMS room", f"session {session['id']}")
            if self.apply:
                firestore_tuition_sessions.add_document(str(session["id"]), {
                    **MEET_FIELDS_CLEARED,
                    "meeting_link": acs.call_link(acs.KIND_TUITION, session["id"]),
                    "meet_status": "CREATED",
                })

    # -- files ---------------------------------------------------------------------------
    def _local_to_blob(self, url: str) -> str | None:
        """Uploads one /uploads/... file found on this machine; returns its /files/ address."""
        from app.core.storage import PROVIDER_BLOB, storage_service

        relative = unquote(url)[len("/uploads/"):]
        path = (Path(settings.LOCAL_STORAGE_DIR) / relative).resolve()
        try:
            path.relative_to(Path(settings.LOCAL_STORAGE_DIR).resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        folder, _, name = relative.rpartition("/")
        with open(path, "rb") as handle:
            stored = storage_service.save_stream(
                handle, name, folder or "general",
                content_type=mimetypes.guess_type(name)[0], unique=False,
            )
        return stored["url"] if stored["provider"] == PROVIDER_BLOB else None

    def _file(self, label: str, url, move) -> None:
        """One stored file reference: `move(new_url_or_None, lost)` writes the change."""
        if is_google_file(url):
            self.note(f"{label}: on Google (lost)", str(url)[:90])
            if self.apply and self.mark_lost:
                move(None, True)
        elif str(url or "").startswith("/uploads/"):
            if not self.upload_local:
                self.note(f"{label}: on server disk")
                return
            new_url = self._local_to_blob(str(url)) if self.apply else None
            if new_url:
                self.note(f"{label}: disk -> Blob", new_url)
                move(new_url, False)
            elif self.apply:
                self.note(f"{label}: not on this machine", str(url))
            else:
                self.note(f"{label}: on server disk (would copy if present)")

    def files(self) -> None:
        for material in firestore_materials.list_all():
            def move(new_url, lost, m=material):
                updates = {"storage_warning": LOST_WARNING} if lost else {
                    "file_url": new_url, "storage_provider": "AZURE_BLOB", "storage_warning": None}
                firestore_materials.add_document(str(m["id"]), updates)
            self._file("study material", material.get("file_url"), move)

        for item in firestore_tuition_library.list_all():
            def move(new_url, lost, i=item):
                updates = {"storage_warning": LOST_WARNING} if lost else {
                    "file_url": new_url, "storage_provider": "AZURE_BLOB", "storage_warning": None}
                firestore_tuition_library.add_document(str(i["id"]), updates)
            self._file("library item", item.get("file_url"), move)

        for exam in firestore_exams.list_all():
            def move(new_url, lost, e=exam):
                updates = {"question_paper_warning": LOST_WARNING} if lost else {
                    "question_paper_url": new_url, "question_paper_provider": "AZURE_BLOB",
                    "question_paper_warning": None}
                firestore_exams.add_document(str(e["id"]), updates)
            self._file("question paper", exam.get("question_paper_url"), move)

        for collection, label, key in (
            (firestore_topics, "topic attachment", "attachments"),
            (firestore_exam_submissions, "answer sheet", "attachments"),
        ):
            for record in collection.list_all():
                attachments = list(record.get(key) or [])
                changed = False
                for index, attachment in enumerate(attachments):
                    def move(new_url, lost, i=index):
                        nonlocal changed
                        if lost:
                            attachments[i] = {**attachments[i], "storage_warning": LOST_WARNING}
                        else:
                            attachments[i] = {**attachments[i], "file_url": new_url,
                                              "storage_provider": "AZURE_BLOB", "provider": "AZURE_BLOB",
                                              "storage_warning": None}
                        changed = True
                    self._file(label, attachment.get("file_url"), move)
                if changed and self.apply:
                    collection.add_document(str(record["id"]), {key: attachments})

        for meeting in firestore_meetings.list_all():
            if is_google_file(meeting.get("recording_url")):
                self.note("class recording: on Google (lost)", meeting.get("title"))

    def run(self) -> None:
        print("\nRooms and class links")
        self.classes()
        self.meetings()
        self.tuition()
        print("\nFiles")
        self.files()
        print("\nSummary")
        for key, count in sorted(self.counts.items()):
            print(f"  {key:<42} {count}")
        if not self.counts:
            print("  Nothing points at Google any more.")
        if not self.apply:
            print("\nDry run - nothing written. Re-run with --apply.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the changes.")
    parser.add_argument("--convert-manual-meet", action="store_true",
                        help="Also move hand-pasted meet.google.com links to the LMS call page.")
    parser.add_argument("--upload-local", action="store_true",
                        help="Copy /uploads files found on this machine into Blob storage.")
    parser.add_argument("--mark-lost", action="store_true",
                        help="Label records whose file was on Google so the app says why it will not open.")
    args = parser.parse_args()
    Migration(args.apply, args.convert_manual_meet, args.upload_local, args.mark_lost).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
