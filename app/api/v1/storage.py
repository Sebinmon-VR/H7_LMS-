from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException

from app.api.v1.dependencies import require_teacher
from app.core.gcp_services import StorageError, storage_service
from app.schemas.user import UserOut

router = APIRouter(prefix="/storage", tags=["Storage & Cloud Integration"])


@router.post("/upload")
async def upload_file_to_cloud(
    folder: str = Form("general", description="Subfolder path in cloud storage bucket or local uploads"),
    file: UploadFile = File(...),
    _: UserOut = Depends(require_teacher)
):
    """
    [Teacher/Admin Only] Directly upload any file (notes, books, syllabus, recordings) to the
    configured backend: Google Cloud Storage, a Workspace Shared Drive, or local disk.

    `storage_provider` is where the bytes actually landed. When it differs from
    `requested_provider` the cloud upload did not work and `warning` says why - the file is
    still saved locally so nothing is lost. Set STORAGE_STRICT=True to get a 502 instead.
    """
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    try:
        stored = await storage_service.save_file_detailed(file=file, folder=folder)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "access_url": stored["url"],
        "storage_provider": stored["provider"],
        "requested_provider": stored["requested_provider"],
        "warning": stored.get("warning"),
    }


@router.get("/status")
def get_storage_status(_: UserOut = Depends(require_teacher)):
    """
    [Teacher/Admin Only] Which storage backend is active and whether it is reachable.

    A quick way for a teacher to tell whether "my upload isn't going to Drive" is a
    per-file problem or a server configuration one. Admins get the full picture, including
    Google Meet, from `GET /admin/integrations`.
    """
    return storage_service.check_access()
