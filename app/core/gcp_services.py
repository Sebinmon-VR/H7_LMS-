import os
import shutil
import logging
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import UploadFile

from app.core import google_drive
from app.core.config import settings

logger = logging.getLogger("gcp_services")

PROVIDER_GCS = "GCS"
PROVIDER_DRIVE = "DRIVE"
PROVIDER_LOCAL = "LOCAL"


class StorageError(Exception):
    """Raised when an upload cannot reach the configured provider and STORAGE_STRICT is on."""


class StorageService:
    """
    Unified storage service for study materials, backed by one of three providers:

      GCS    - Google Cloud Storage bucket. Cheapest and fastest for plain file serving.
      DRIVE  - Google Workspace Shared Drive or folder. Use when teachers need in-place
               preview and collaborative editing.
      LOCAL  - On-disk directory, for local development.

    The provider is chosen by STORAGE_PROVIDER and resolved *per request*, not once at
    import: a Drive or GCS outage during startup used to downgrade the whole process to
    local disk until someone restarted it, which is exactly how uploads end up silently
    landing on the API server instead of in Drive.

    A cloud upload that fails still falls back to local disk so the file is never lost, but
    the caller is told which provider actually stored it and why, and STORAGE_STRICT=True
    turns that fallback into a hard error for deployments that would rather fail loudly.
    """

    def __init__(self):
        self.upload_dir = Path(settings.LOCAL_STORAGE_DIR)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.gcp_bucket_name = settings.GCP_BUCKET_NAME
        self.gcp_client = None
        self._gcs_init_error: str | None = None

        conflict = settings.storage_config_conflict
        if conflict:
            logger.warning("Storage configuration conflict: %s", conflict)

        if self.configured_provider == PROVIDER_GCS:
            self._init_gcs_client()

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

    def _init_gcs_client(self) -> bool:
        try:
            from google.cloud import storage
            if os.path.exists(settings.GOOGLE_APPLICATION_CREDENTIALS):
                self.gcp_client = storage.Client.from_service_account_json(
                    settings.GOOGLE_APPLICATION_CREDENTIALS
                )
            else:
                self.gcp_client = storage.Client(project=settings.GCP_PROJECT_ID)
            self._gcs_init_error = None
            logger.info("Initialized Google Cloud Storage SDK client successfully.")
            return True
        except Exception as e:
            self._gcs_init_error = str(e)
            logger.warning("Could not initialize Google Cloud Storage SDK (%s).", e)
            return False

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
        material so deletion later knows which backend to talk to, letting GCS-era,
        Drive-era, and local files coexist.
        """
        filename = self._unique_filename(file.filename)
        requested = self.configured_provider
        warning: str | None = None

        if requested == PROVIDER_DRIVE:
            try:
                file.file.seek(0)
                uploaded = google_drive.upload_file(
                    file_bytes=file.file.read(),
                    filename=filename,
                    content_type=file.content_type,
                    folder=folder,
                )
                url = uploaded.get("web_view_link") or uploaded.get("web_content_link")
                if url:
                    return {
                        "url": url,
                        "provider": PROVIDER_DRIVE,
                        "requested_provider": requested,
                        "warning": None,
                        "file_id": uploaded.get("file_id"),
                    }
                warning = f"Drive accepted '{filename}' but returned no shareable link."
                logger.error(warning)
            except google_drive.GoogleDriveError as e:
                warning = f"Google Drive upload failed: {e}"
                logger.error(warning)
            except Exception as e:  # never let an SDK surprise take down an upload
                warning = f"Google Drive upload failed unexpectedly: {e}"
                logger.exception("Unexpected Drive upload failure for '%s'", filename)

        if requested == PROVIDER_GCS:
            if not self.gcp_client:
                self._init_gcs_client()

            if self.gcp_client:
                destination_path = f"{folder}/{filename}"
                try:
                    bucket = self.gcp_client.bucket(self.gcp_bucket_name)
                    blob = bucket.blob(destination_path)
                    file.file.seek(0)
                    blob.upload_from_file(file.file, content_type=file.content_type)
                    public_url = f"https://storage.googleapis.com/{self.gcp_bucket_name}/{destination_path}"
                    return {
                        "url": public_url,
                        "provider": PROVIDER_GCS,
                        "requested_provider": requested,
                        "warning": None,
                        "file_id": destination_path,
                    }
                except Exception as e:
                    warning = f"Google Cloud Storage upload failed: {e}"
                    logger.error(warning)
            else:
                warning = (
                    "Google Cloud Storage client is unavailable: "
                    f"{self._gcs_init_error or 'unknown error'}"
                )

        if warning and settings.STORAGE_STRICT:
            raise StorageError(warning)

        # Local directory storage fallback. The file is still saved so nothing is lost, but
        # the caller is told the cloud provider was not used.
        target_folder = self.upload_dir / folder
        target_folder.mkdir(parents=True, exist_ok=True)
        file_path = target_folder / filename

        file.file.seek(0)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

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

        if provider == PROVIDER_GCS:
            info["bucket"] = self.gcp_bucket_name
            info["client_ready"] = self.gcp_client is not None
            if self._gcs_init_error:
                info["problems"] = [self._gcs_init_error]
        elif provider == PROVIDER_DRIVE:
            info["drive"] = google_drive.describe_configuration()

        return info

    def check_access(self) -> dict:
        """
        Live probe of the configured provider. Returns {"ok", "detail", ...}.
        Nothing is written for Drive; GCS is verified by reading bucket metadata.
        """
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

        if provider == PROVIDER_DRIVE:
            return {"provider": PROVIDER_DRIVE, **google_drive.check_access()}

        if not self.gcp_client:
            self._init_gcs_client()
        if not self.gcp_client:
            return {
                "provider": PROVIDER_GCS,
                "ok": False,
                "bucket": self.gcp_bucket_name,
                "detail": (
                    "Could not initialize the Cloud Storage client: "
                    f"{self._gcs_init_error or 'unknown error'}"
                ),
            }

        try:
            bucket = self.gcp_client.get_bucket(self.gcp_bucket_name)
            return {
                "provider": PROVIDER_GCS,
                "ok": True,
                "bucket": bucket.name,
                "detail": f"Bucket '{bucket.name}' is reachable.",
            }
        except Exception as e:
            return {
                "provider": PROVIDER_GCS,
                "ok": False,
                "bucket": self.gcp_bucket_name,
                "detail": f"Could not read bucket '{self.gcp_bucket_name}': {e}",
            }

    def delete_file(self, file_url: str, provider: str | None = None) -> bool:
        """
        Removes the stored file behind a URL.

        `provider` is the value recorded when the file was uploaded; when absent it is
        inferred from the URL shape so materials predating that field still clean up.
        Returns False instead of raising, so deleting an LMS record is never blocked by a
        storage failure.
        """
        if not file_url:
            return False

        resolved = (provider or self._infer_provider(file_url)).upper()

        if resolved == PROVIDER_DRIVE:
            return google_drive.delete_file_by_url(file_url)

        if resolved == PROVIDER_GCS:
            return self._delete_gcs_object(file_url)

        return self._delete_local_file(file_url)

    @staticmethod
    def _infer_provider(file_url: str) -> str:
        if "drive.google.com" in file_url:
            return PROVIDER_DRIVE
        if "storage.googleapis.com" in file_url:
            return PROVIDER_GCS
        return PROVIDER_LOCAL

    def _delete_gcs_object(self, file_url: str) -> bool:
        if not self.gcp_client:
            self._init_gcs_client()
        if not self.gcp_client:
            return False

        prefix = f"https://storage.googleapis.com/{self.gcp_bucket_name}/"
        if not file_url.startswith(prefix):
            logger.warning("URL '%s' does not belong to bucket '%s'.", file_url, self.gcp_bucket_name)
            return False

        blob_path = unquote(file_url[len(prefix):])
        try:
            self.gcp_client.bucket(self.gcp_bucket_name).blob(blob_path).delete()
            return True
        except Exception as e:
            logger.warning(f"Could not delete GCS object '{blob_path}': {e}")
            return False

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
