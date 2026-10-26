import io
import logging
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import UploadFile

from app.core import azure_blob
from app.core.config import settings

logger = logging.getLogger("storage")

PROVIDER_BLOB = "AZURE_BLOB"
PROVIDER_LOCAL = "LOCAL"
# Files uploaded before the move off Google. Their bytes were in the school's Google Cloud
# bucket or Shared Drive, which no longer exist; the records remain so history reads true.
LEGACY_GOOGLE_PROVIDERS = frozenset({"GCS", "DRIVE"})


class StorageError(Exception):
    """Raised when an upload cannot reach the configured provider and STORAGE_STRICT is on."""


class StorageService:
    """
    Where uploaded files go, backed by one of two providers:

      AZURE_BLOB - a private Azure Blob container (app.core.azure_blob). What callers store is
                   an LMS path, /files/<blob>, which the API redirects to a short-lived link.
      LOCAL      - an on-disk directory, for local development.

    The provider is resolved per request rather than once at import, so a storage outage at
    startup does not quietly downgrade the whole process to local disk until a restart.

    A cloud upload that fails still falls back to local disk so the file is never lost, but
    the caller is told which provider actually stored it and why, and STORAGE_STRICT=True
    turns that fallback into a hard error for deployments that would rather fail loudly.
    """

    def __init__(self):
        self.upload_dir = Path(settings.LOCAL_STORAGE_DIR)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        conflict = settings.storage_config_conflict
        if conflict:
            logger.warning("Storage configuration conflict: %s", conflict)

    @property
    def configured_provider(self) -> str:
        """The provider requested by configuration, before any runtime fallback."""
        return settings.resolved_storage_provider

    # Retained for existing callers and response payloads that read `storage_service.provider`.
    @property
    def provider(self) -> str:
        return self.configured_provider

    @property
    def use_local(self) -> bool:
        """Backwards-compatible flag retained for existing callers and response payloads."""
        return self.configured_provider == PROVIDER_LOCAL

    @staticmethod
    def _unique_filename(filename: str) -> str:
        """
        Prefixes a short random token so two teachers uploading 'notes.pdf' to the same
        class do not overwrite each other.
        """
        safe_name = Path(filename or "unnamed_file").name
        return f"{uuid.uuid4().hex[:8]}_{safe_name}"

    async def save_file(self, file: UploadFile, folder: str = "notes") -> str:
        """
        Saves an uploaded file to the configured provider and returns its accessible URL.
        """
        result = await self.save_file_detailed(file, folder)
        return result["url"]

    async def save_file_detailed(self, file: UploadFile, folder: str = "notes") -> dict:
        """
        Saves an uploaded file and returns:
          {"url", "provider", "requested_provider", "warning", "file_id"}

        `provider` is where the bytes actually landed; when it differs from
        `requested_provider`, `warning` says why. Callers persist `provider` alongside the
        material so deletion later knows which backend to talk to.
        """
        file.file.seek(0)
        return self.save_stream(file.file, file.filename, folder, file.content_type)

    def save_bytes(self, data: bytes, filename: str, folder: str,
                   content_type: str | None = None, unique: bool = True) -> dict:
        return self.save_stream(io.BytesIO(data), filename, folder, content_type, unique=unique)

    def save_stream(self, stream, filename: str | None, folder: str,
                    content_type: str | None = None, unique: bool = True) -> dict:
        """The work behind every save, for callers holding a stream rather than an upload."""
        # `folder` can come from a client; no segment of it may climb out of the store.
        folder = "/".join(
            part for part in (folder or "general").replace("\\", "/").split("/")
            if part not in ("", ".", "..")
        ) or "general"
        filename = self._unique_filename(filename) if unique else Path(filename or "file").name
        requested = self.configured_provider
        warning: str | None = None

        if requested == PROVIDER_BLOB:
            blob_name = f"{folder}/{filename}"
            try:
                azure_blob.upload(stream, blob_name, content_type)
                return {
                    "url": azure_blob.lms_path(blob_name),
                    "provider": PROVIDER_BLOB,
                    "requested_provider": requested,
                    "warning": None,
                    "file_id": blob_name,
                }
            except azure_blob.BlobStorageError as exc:
                warning = str(exc)
                logger.error(warning)
            except Exception as exc:  # never let an SDK surprise take down an upload
                warning = f"Azure Blob upload failed unexpectedly: {exc}"
                logger.exception("Unexpected blob upload failure for '%s'", filename)

        if warning and settings.STORAGE_STRICT:
            raise StorageError(warning)

        # Local directory storage fallback. The file is still saved so nothing is lost, but
        # the caller is told the cloud provider was not used.
        target_folder = self.upload_dir / folder
        target_folder.mkdir(parents=True, exist_ok=True)
        file_path = target_folder / filename

        try:
            stream.seek(0)
        except Exception:
            pass
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(stream, buffer)

        if warning:
            warning += " The file was saved to the API server's local disk instead."

        return {
            "url": f"/uploads/{folder}/{filename}",
            "provider": PROVIDER_LOCAL,
            "requested_provider": requested,
            "warning": warning,
            "file_id": None,
        }

    def describe(self) -> dict:
        """Non-secret snapshot of the storage configuration, for admin diagnostics."""
        provider = self.configured_provider
        info = {
            "provider": provider,
            "strict": settings.STORAGE_STRICT,
            "local_dir": str(self.upload_dir),
        }

        conflict = settings.storage_config_conflict
        if conflict:
            info["config_conflict"] = conflict

        if provider == PROVIDER_BLOB:
            blob = azure_blob.describe()
            info["container"] = blob["container"]
            info["account"] = blob.get("account")
            if blob["problems"]:
                info["problems"] = blob["problems"]

        return info

    def check_access(self) -> dict:
        """Live probe of the configured provider. Returns {"ok", "detail", ...}."""
        provider = self.configured_provider

        if provider == PROVIDER_LOCAL:
            writable = os.access(self.upload_dir, os.W_OK)
            return {
                "provider": PROVIDER_LOCAL,
                "ok": writable,
                "detail": (
                    f"Local storage directory '{self.upload_dir}' is writable."
                    if writable else
                    f"Local storage directory '{self.upload_dir}' is not writable."
                ),
            }

        return azure_blob.check_access()

    def delete_file(self, file_url: str, provider: str | None = None) -> bool:
        """
        Removes the stored file behind a URL.

        `provider` is the value recorded when the file was uploaded; when absent it is
        inferred from the URL shape. Returns False instead of raising, so deleting an LMS
        record is never blocked by a storage failure - which is also what a Google-era file
        gets, its bytes having gone with the Workspace.
        """
        if not file_url:
            return False

        resolved = (provider or self._infer_provider(file_url)).upper()

        if resolved in LEGACY_GOOGLE_PROVIDERS:
            return False

        if resolved == PROVIDER_BLOB:
            blob_name = azure_blob.blob_name_from(file_url)
            return azure_blob.delete(blob_name) if blob_name else False

        return self._delete_local_file(file_url)

    @staticmethod
    def _infer_provider(file_url: str) -> str:
        if azure_blob.blob_name_from(file_url):
            return PROVIDER_BLOB
        if "drive.google.com" in file_url:
            return "DRIVE"
        if "storage.googleapis.com" in file_url:
            return "GCS"
        return PROVIDER_LOCAL

    def _delete_local_file(self, file_url: str) -> bool:
        path_part = unquote(urlparse(file_url).path)
        if not path_part.startswith("/uploads/"):
            return False

        relative = path_part[len("/uploads/"):]
        target = (self.upload_dir / relative).resolve()

        # Refuse to follow a traversal payload outside the uploads directory.
        try:
            target.relative_to(self.upload_dir.resolve())
        except ValueError:
            logger.warning("Refusing to delete '%s' outside the uploads directory.", target)
            return False

        try:
            if target.is_file():
                target.unlink()
                return True
        except Exception as e:
            logger.warning(f"Could not delete local file '{target}': {e}")
        return False


storage_service = StorageService()

# Where uploaded profile photos live (POST /admin/users/{id}/photo).
PROFILE_PHOTO_PREFIXES = ("/files/photos/", "/uploads/photos/")


def forget_profile_photo(url: str | None) -> None:
    """Deletes an uploaded profile photo. A web address pasted in before uploads is not ours to delete."""
    if url and url.startswith(PROFILE_PHOTO_PREFIXES):
        storage_service.delete_file(url)
