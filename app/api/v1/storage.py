from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from app.api.v1.dependencies import require_teacher
from app.core.gcp_services import storage_service
from app.schemas.user import UserOut

router = APIRouter(prefix="/storage", tags=["Storage & Cloud Integration"])


@router.post("/upload")
async def upload_file_to_cloud(
    folder: str = Form("general", description="Subfolder path in cloud storage bucket or local uploads"),
    file: UploadFile = File(...),
    _: UserOut = Depends(require_teacher)
):
    """
    [Teacher/Admin Only] Directly upload any file (notes, books, syllabus, recordings) to Google Cloud Storage / Firebase Storage or Local fallback.
    Returns the cloud URL or relative access URL.
    """
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    access_url = await storage_service.save_file(file=file, folder=folder)
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "access_url": access_url,
        "storage_provider": "Google Cloud Storage / Firebase" if not storage_service.use_local else "Local Storage"
    }
