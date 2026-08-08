import os
import shutil
import logging
from pathlib import Path
from fastapi import UploadFile
from app.core.config import settings

logger = logging.getLogger("gcp_services")


class StorageService:
    """
    Unified Storage Service handling file uploads to Google Cloud Storage / Firebase Storage,
    with automatic fallback to local directory storage for local development.
    """

    def __init__(self):
        self.use_local = settings.USE_LOCAL_STORAGE
        self.upload_dir = Path(settings.LOCAL_STORAGE_DIR)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.gcp_bucket_name = settings.GCP_BUCKET_NAME
        self.gcp_client = None

        if not self.use_local:
            try:
                from google.cloud import storage
                if os.path.exists(settings.GOOGLE_APPLICATION_CREDENTIALS):
                    self.gcp_client = storage.Client.from_service_account_json(
                        settings.GOOGLE_APPLICATION_CREDENTIALS
                    )
                else:
                    self.gcp_client = storage.Client(project=settings.GCP_PROJECT_ID)
                logger.info("Initialized Google Cloud Storage SDK client successfully.")
            except Exception as e:
                logger.warning(f"Could not initialize Google Cloud Storage SDK ({e}). Using local storage fallback.")
                self.use_local = True

    async def save_file(self, file: UploadFile, folder: str = "notes") -> str:
        """
        Saves an uploaded file to Google Cloud Storage bucket or Local storage directory,
        and returns the accessible resource URL.
        """
        filename = file.filename or "unnamed_file"
        destination_path = f"{folder}/{filename}"

        if not self.use_local and self.gcp_client:
            try:
                bucket = self.gcp_client.bucket(self.gcp_bucket_name)
                blob = bucket.blob(destination_path)
                file.file.seek(0)
                blob.upload_from_file(file.file, content_type=file.content_type)
                public_url = f"https://storage.googleapis.com/{self.gcp_bucket_name}/{destination_path}"
                return public_url
            except Exception as e:
                logger.error(f"Failed to upload to Google Cloud Storage: {e}. Falling back to local storage.")

        # Local directory storage fallback
        target_folder = self.upload_dir / folder
        target_folder.mkdir(parents=True, exist_ok=True)
        file_path = target_folder / filename

        file.file.seek(0)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return f"/uploads/{folder}/{filename}"


storage_service = StorageService()
