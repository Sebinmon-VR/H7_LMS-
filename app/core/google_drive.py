"""
Google Drive storage backend for study materials.

Drive is the right home for documents teachers want to preview and co-edit in place;
Cloud Storage remains the cheaper, faster choice for plain file serving. Both are
supported side by side and selected per deployment via STORAGE_PROVIDER.

Two Drive layouts are supported, because a service account cannot own files in a My Drive
(it has zero personal storage quota, so uploads there fail with `storageQuotaExceeded`):

  Shared Drive  - GOOGLE_DRIVE_SHARED_DRIVE_ID. Files are owned by the organization, which
                  sidesteps the quota entirely. Add the service account as Content manager.
  Folder        - GOOGLE_DRIVE_FOLDER_ID, an ordinary folder. Only workable when the upload
                  is performed *as a real user* via domain-wide delegation
                  (GOOGLE_DRIVE_IMPERSONATION with GOOGLE_IMPERSONATION_FALLBACK), since
                  then the file is owned by that user, not the service account.

Setup checklist:
  1. Enable the Google Drive API on the GCP project.
  2. For impersonation, authorize the service account's numeric client ID for
     https://www.googleapis.com/auth/drive under Workspace domain-wide delegation.
  3. Point GOOGLE_DRIVE_SHARED_DRIVE_ID or GOOGLE_DRIVE_FOLDER_ID at the destination and
     grant the acting identity write access to it.

`check_access()` performs the whole chain live and reports exactly which step fails, so a
misconfiguration is diagnosable from the API instead of from server logs.
"""

import io
import logging

from app.core.config import settings
from app.core.firebase import FirestoreService

logger = logging.getLogger("google_drive")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Caches resolved per-class folder IDs so uploads don't re-query Drive every time.
_folder_cache = FirestoreService("drive_folders")


class GoogleDriveError(Exception):
    """Raised when a Drive operation cannot be completed."""


def _credentials_path() -> str | None:
    import os

    for path in (settings.FIREBASE_CREDENTIALS_PATH, settings.GOOGLE_APPLICATION_CREDENTIALS):
        if path and os.path.exists(path):
            return path
    return None


def normalize_drive_id(value: str | None) -> str:
    """
    Accepts either a bare Drive ID or the URL it was copied from.

    Pasting the address bar straight into GOOGLE_DRIVE_SHARED_DRIVE_ID is the obvious thing
    to do and produces a value Drive rejects with a bare 404, so the URL forms are unwrapped
    here rather than left as a configuration trap.
    """
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        return ""

    if "drive.google.com" not in value and "docs.google.com" not in value:
        return value

    for marker in ("/folders/", "/drive/u/0/folders/", "/file/d/", "/d/"):
        if marker in value:
            return value.split(marker, 1)[1].split("/", 1)[0].split("?", 1)[0]

    if "id=" in value:
        return value.split("id=", 1)[1].split("&", 1)[0]

    return value


def _describe_api_error(exc: Exception) -> str:
    """
    Turns a googleapiclient HttpError into a message that names the actual cause.

    The default repr buries the reason inside a JSON blob, which is why Drive failures have
    been landing in the logs as unreadable noise.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = ""
    try:  # error_details is populated by google-api-python-client >= 2.x
        details = getattr(exc, "error_details", None)
        if details:
            first = details[0] if isinstance(details, list) and details else details
            if isinstance(first, dict):
                reason = first.get("reason") or first.get("message") or ""
    except Exception:  # pragma: no cover - diagnostics must never raise
        reason = ""

    text = str(exc)

    hint = ""
    if "invalid_grant" in text or "unauthorized_client" in text:
        # No HTTP status here: the token exchange itself failed, before any Drive call.
        subject = impersonated_identity() or "the impersonated user"
        return (
            f"Domain-wide delegation was refused for '{subject}'. Either the service "
            "account's numeric Client ID is not authorized for "
            f"{DRIVE_SCOPES[0]} in the Workspace admin console (Security -> Access and data "
            f"control -> API controls -> Domain-wide delegation), or '{subject}' is not a "
            f"real user in '{settings.GOOGLE_WORKSPACE_DOMAIN or 'the domain'}'. On a "
            "project without Google Workspace, set GOOGLE_DRIVE_IMPERSONATION=False and use "
            f"a Shared Drive the service account is a Content manager on. ({text})"
        )

    if reason in {"accessNotConfigured", "SERVICE_DISABLED"} or "accessNotConfigured" in text:
        # Distinct from a permissions problem: the API is switched off project-wide.
        project = settings.GCP_PROJECT_ID
        return (
            f"The Google Drive API is not enabled on GCP project '{project}'. Enable it at "
            f"https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project={project} "
            f"and retry in a minute or two. ({text})"
        )

    if reason in {"storageQuotaExceeded", "quotaExceeded"}:
        hint = (
            " The service account has no personal Drive quota. Upload into a Shared Drive "
            "(GOOGLE_DRIVE_SHARED_DRIVE_ID) or enable GOOGLE_DRIVE_IMPERSONATION so a real "
            "Workspace user owns the file."
        )
    elif reason in {"notFound", "fileNotFound"} or status == 404:
        hint = (
            " The target Drive ID does not exist or the acting identity cannot see it. "
            "Check GOOGLE_DRIVE_SHARED_DRIVE_ID / GOOGLE_DRIVE_FOLDER_ID and that the "
            "service account (or impersonated user) was granted access to it."
        )
    elif reason in {"insufficientFilePermissions", "forbidden"} or status == 403:
        hint = (
            " The acting identity lacks write access to the target. Add it as a Content "
            "manager on the Shared Drive, or as an Editor on the folder."
        )
    elif status == 401:
        hint = (
            " Authentication was rejected. If impersonation is on, confirm the service "
            "account's client ID is authorized for the drive scope under Workspace "
            "domain-wide delegation."
        )

    label = f"HTTP {status}" if status else type(exc).__name__
    return f"{label}: {reason or exc}{hint}"


def _build_drive_service():
    """
    Builds a Drive API client.

    Impersonation is used when GOOGLE_DRIVE_IMPERSONATION is on and a fallback identity is
    configured, so files are owned by a real Workspace user rather than the bare service
    account (which cannot own files at all outside a Shared Drive).
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise GoogleDriveError(
            f"Google API client libraries not installed ({exc}). "
            "Install google-api-python-client and google-auth."
        ) from exc

    cred_path = _credentials_path()
    if not cred_path:
        raise GoogleDriveError(
            "No service account credentials file found for Drive access. Set "
            "FIREBASE_CREDENTIALS_PATH or GOOGLE_APPLICATION_CREDENTIALS to a readable file."
        )

    try:
        credentials = service_account.Credentials.from_service_account_file(
            cred_path, scopes=DRIVE_SCOPES
        )
        subject = impersonated_identity()
        if subject:
            credentials = credentials.with_subject(subject)
    except GoogleDriveError:
        raise
    except Exception as exc:
        raise GoogleDriveError(f"Could not build Drive credentials: {exc}") from exc

    try:
        return build("drive", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise GoogleDriveError(f"Could not build the Drive client: {_describe_api_error(exc)}") from exc


def impersonated_identity() -> str | None:
    """The Workspace user Drive calls act as, or None when acting as the service account."""
    if not settings.GOOGLE_DRIVE_IMPERSONATION:
        return None
    return (settings.GOOGLE_IMPERSONATION_FALLBACK or "").strip() or None


def _drive_root() -> tuple[str, bool]:
    """
    Returns (root_folder_id, is_shared_drive) for the configured destination.

    A Shared Drive wins when both are set: it is the layout that works without any personal
    Drive quota, so it is the safer default when a deployment has configured both.
    """
    shared_drive_id = normalize_drive_id(settings.GOOGLE_DRIVE_SHARED_DRIVE_ID)
    if shared_drive_id:
        return shared_drive_id, True

    folder_id = normalize_drive_id(settings.GOOGLE_DRIVE_FOLDER_ID)
    if folder_id:
        return folder_id, False

    raise GoogleDriveError(
        "No Drive destination configured. Set GOOGLE_DRIVE_SHARED_DRIVE_ID (recommended) "
        "or GOOGLE_DRIVE_FOLDER_ID."
    )


def is_configured() -> bool:
    """True when a Drive destination and service account credentials are both present."""
    if not _credentials_path():
        return False
    return bool(
        normalize_drive_id(settings.GOOGLE_DRIVE_SHARED_DRIVE_ID)
        or normalize_drive_id(settings.GOOGLE_DRIVE_FOLDER_ID)
    )


def configuration_problems() -> list[str]:
    """
    Static configuration issues, without touching the network.
    Returns an empty list when the settings look coherent.
    """
    problems: list[str] = []

    if not _credentials_path():
        problems.append(
            "No service account credentials file found "
            f"(looked for '{settings.FIREBASE_CREDENTIALS_PATH}' and "
            f"'{settings.GOOGLE_APPLICATION_CREDENTIALS}')."
        )

    shared_drive_id = normalize_drive_id(settings.GOOGLE_DRIVE_SHARED_DRIVE_ID)
    folder_id = normalize_drive_id(settings.GOOGLE_DRIVE_FOLDER_ID)
    if not shared_drive_id and not folder_id:
        problems.append(
            "Neither GOOGLE_DRIVE_SHARED_DRIVE_ID nor GOOGLE_DRIVE_FOLDER_ID is set, so "
            "there is nowhere to upload to."
        )

    if folder_id and not shared_drive_id and not impersonated_identity():
        problems.append(
            "GOOGLE_DRIVE_FOLDER_ID points at an ordinary folder but impersonation is off, "
            "so uploads will be rejected with storageQuotaExceeded: a service account "
            "cannot own files outside a Shared Drive. Set GOOGLE_IMPERSONATION_FALLBACK "
            "and GOOGLE_DRIVE_IMPERSONATION=True, or use a Shared Drive instead."
        )

    return problems


def describe_configuration() -> dict:
    """Non-secret snapshot of how Drive is wired up, for the admin diagnostics endpoint."""
    shared_drive_id = normalize_drive_id(settings.GOOGLE_DRIVE_SHARED_DRIVE_ID)
    folder_id = normalize_drive_id(settings.GOOGLE_DRIVE_FOLDER_ID)
    return {
        "configured": is_configured(),
        "destination": "SHARED_DRIVE" if shared_drive_id else ("FOLDER" if folder_id else None),
        "shared_drive_id": shared_drive_id or None,
        "folder_id": folder_id or None,
        "root_folder_name": settings.GOOGLE_DRIVE_ROOT_FOLDER_NAME or None,
        "impersonating": impersonated_identity(),
        "link_sharing": settings.GOOGLE_DRIVE_LINK_SHARING,
        "credentials_file": _credentials_path(),
        "problems": configuration_problems(),
    }


def _list_scope_kwargs(is_shared_drive: bool, root_id: str) -> dict:
    """
    Extra files.list() arguments needed to see the target.

    `corpora`/`driveId` are valid only for a Shared Drive; sending them while querying an
    ordinary folder makes Drive return an empty result set, which previously showed up as
    duplicate folders being created on every upload.
    """
    if is_shared_drive:
        return {
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "corpora": "drive",
            "driveId": root_id,
        }
    return {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


def _find_or_create_folder(service, name: str, parent_id: str, is_shared_drive: bool, root_id: str) -> str:
    """
    Returns the ID of the named folder under `parent_id`, creating it when absent.

    Results are cached in Firestore to avoid a lookup round-trip per upload. A cached ID
    whose folder has since been trashed or deleted is detected and re-resolved rather than
    breaking every subsequent upload to that class.
    """
    cache_key = f"{parent_id}__{name}".replace("/", "_")
    cached = _folder_cache.get_document(cache_key)
    if cached and cached.get("folder_id"):
        folder_id = cached["folder_id"]
        if _folder_exists(service, folder_id):
            return folder_id
        logger.warning("Cached Drive folder '%s' (%s) is gone; re-resolving.", name, folder_id)
        _folder_cache.delete_document(cache_key)

    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped}' and mimeType = '{FOLDER_MIME_TYPE}' "
        f"and '{parent_id}' in parents and trashed = false"
    )

    try:
        response = service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=1,
            **_list_scope_kwargs(is_shared_drive, root_id),
        ).execute()
    except Exception as exc:
        raise GoogleDriveError(
            f"Could not list Drive folder '{name}'. {_describe_api_error(exc)}"
        ) from exc

    files = response.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        try:
            created = service.files().create(
                body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            raise GoogleDriveError(
                f"Could not create Drive folder '{name}'. {_describe_api_error(exc)}"
            ) from exc
        folder_id = created["id"]

    _folder_cache.add_document(cache_key, {"folder_id": folder_id, "name": name, "parent_id": parent_id})
    return folder_id


def _folder_exists(service, folder_id: str) -> bool:
    """Cheap liveness check for a cached folder ID."""
    try:
        meta = service.files().get(
            fileId=folder_id, fields="id, trashed", supportsAllDrives=True
        ).execute()
        return not meta.get("trashed", False)
    except Exception:
        return False


def _resolve_target_folder(service, folder_path: str) -> str:
    """
    Walks a 'class_10/notes' style path, creating each segment under the configured root.
    GOOGLE_DRIVE_ROOT_FOLDER_NAME, when set, becomes the first segment so LMS uploads stay
    grouped inside a Shared Drive that may hold other content too.
    """
    root_id, is_shared_drive = _drive_root()

    segments = []
    root_name = (settings.GOOGLE_DRIVE_ROOT_FOLDER_NAME or "").strip()
    if root_name:
        segments.append(root_name)
    segments += [s.strip() for s in folder_path.split("/") if s.strip()]

    parent_id = root_id
    for segment in segments:
        parent_id = _find_or_create_folder(service, segment, parent_id, is_shared_drive, root_id)
    return parent_id


def _share_file(service, file_id: str) -> None:
    """
    Opens the uploaded file up so students can actually read it.

    Both grants are best-effort: Shared Drive membership may already cover access, and a
    Workspace policy can forbid link sharing outright. Neither case should fail an upload
    that has already succeeded.
    """
    if settings.GOOGLE_DRIVE_LINK_SHARING:
        try:
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
            return
        except Exception as exc:
            logger.warning(
                "Could not enable link sharing on '%s': %s", file_id, _describe_api_error(exc)
            )

    if settings.GOOGLE_WORKSPACE_DOMAIN:
        try:
            service.permissions().create(
                fileId=file_id,
                body={"type": "domain", "role": "reader", "domain": settings.GOOGLE_WORKSPACE_DOMAIN},
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            logger.warning(
                "Could not set domain reader permission on '%s': %s", file_id, _describe_api_error(exc)
            )


def upload_file(file_bytes: bytes, filename: str, content_type: str | None, folder: str = "") -> dict:
    """
    Uploads bytes into the configured Drive destination under `folder`.

    Returns {"file_id", "web_view_link", "web_content_link", "download_link"}.
    Raises GoogleDriveError - carrying the real Drive reason - so the caller can report it
    or fall back to another provider.
    """
    problems = configuration_problems()
    if problems:
        raise GoogleDriveError("Drive storage is not configured. " + " ".join(problems))

    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ModuleNotFoundError as exc:
        raise GoogleDriveError(f"google-api-python-client not installed: {exc}") from exc

    service = _build_drive_service()
    parent_id = _resolve_target_folder(service, folder)

    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=content_type or "application/octet-stream",
        resumable=True,
    )

    try:
        created = service.files().create(
            body={"name": filename, "parents": [parent_id]},
            media_body=media,
            fields="id, webViewLink, webContentLink",
            supportsAllDrives=True,
        ).execute()
    except Exception as exc:
        raise GoogleDriveError(
            f"Drive upload failed for '{filename}'. {_describe_api_error(exc)}"
        ) from exc

    file_id = created["id"]
    _share_file(service, file_id)

    return {
        "file_id": file_id,
        "web_view_link": created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view",
        "web_content_link": created.get("webContentLink"),
        "download_link": f"https://drive.google.com/uc?export=download&id={file_id}",
    }


def check_access() -> dict:
    """
    Live end-to-end probe: build the client, read the destination, confirm write access.

    Returns {"ok", "detail", ...}. Used by the admin integrations endpoint so a broken Drive
    setup can be diagnosed without reading server logs or attempting a real upload.
    """
    result = describe_configuration()

    if result["problems"]:
        return {**result, "ok": False, "detail": " ".join(result["problems"])}

    try:
        root_id, is_shared_drive = _drive_root()
        service = _build_drive_service()
        meta = service.files().get(
            fileId=root_id,
            fields="id, name, mimeType, capabilities/canAddChildren",
            supportsAllDrives=True,
        ).execute()
    except GoogleDriveError as exc:
        return {**result, "ok": False, "detail": str(exc)}
    except Exception as exc:
        return {
            **result,
            "ok": False,
            "detail": f"Could not read the Drive destination. {_describe_api_error(exc)}",
        }

    can_write = (meta.get("capabilities") or {}).get("canAddChildren")
    if can_write is False:
        return {
            **result,
            "ok": False,
            "target_name": meta.get("name"),
            "detail": (
                f"'{meta.get('name')}' is reachable but the acting identity cannot add files "
                "to it. Grant Content manager (Shared Drive) or Editor (folder) access."
            ),
        }

    identity = impersonated_identity() or "the service account"
    return {
        **result,
        "ok": True,
        "target_name": meta.get("name"),
        "detail": f"Drive is reachable and writable as {identity} ('{meta.get('name')}').",
    }


def delete_file_by_url(file_url: str) -> bool:
    """Deletes a Drive file given a link previously returned by upload_file."""
    file_id = extract_file_id(file_url)
    if not file_id:
        return False
    return delete_file(file_id)


def delete_file(file_id: str) -> bool:
    """
    Removes a Drive file by ID. Returns False rather than raising when it cannot.

    Permanent deletion inside a Shared Drive is reserved for Managers; a Content manager -
    the role the setup instructions ask for - gets `canDelete: false` and a bare 404 from
    files.delete, which is why deleting a material used to leave the file behind. Moving it
    to the trash is permitted at that level and achieves the same thing from the LMS's point
    of view, so it is used as the fallback.
    """
    if not is_configured() or not file_id:
        return False

    try:
        service = _build_drive_service()
    except GoogleDriveError as exc:
        logger.warning("Could not delete Drive file '%s': %s", file_id, exc)
        return False

    try:
        service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        return True
    except Exception as exc:
        delete_error = _describe_api_error(exc)

    try:
        service.files().update(
            fileId=file_id, body={"trashed": True}, supportsAllDrives=True
        ).execute()
        logger.info("Trashed Drive file '%s' (permanent delete not permitted).", file_id)
        return True
    except Exception as exc:
        logger.warning(
            "Could not delete or trash Drive file '%s'. delete: %s | trash: %s",
            file_id, delete_error, _describe_api_error(exc),
        )
        return False


def extract_file_id(file_url: str) -> str | None:
    """
    Pulls the file ID out of a Drive URL.
    Handles /file/d/<id>/view, ?id=<id>, and /open?id=<id> shapes.
    """
    if not file_url or "drive.google.com" not in file_url:
        return None

    if "/file/d/" in file_url:
        remainder = file_url.split("/file/d/", 1)[1]
        return remainder.split("/", 1)[0].split("?", 1)[0] or None

    if "id=" in file_url:
        return file_url.split("id=", 1)[1].split("&", 1)[0] or None

    return None
