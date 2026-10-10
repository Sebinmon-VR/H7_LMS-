"""
Azure Blob Storage, the home of every uploaded file and class recording.

The container is private. A file's address in the database is an LMS path, `/files/<blob>`,
and the API answers that path with a redirect to a read link signed for
AZURE_BLOB_LINK_MINUTES (see `read_url` and the `/files` route in app/main.py). Two things
follow: the storage account never has to allow anonymous access, and a link copied out of
the app expires instead of working for ever.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import BinaryIO
from urllib.parse import quote

from app.core.config import settings

logger = logging.getLogger("azure_blob")

FILES_PREFIX = "/files/"

# Folders whose files are never handed out on the open /files/ path. A class recording is
# watched only by the people allowed to see it: the endpoints that list it for them sign a
# link as they answer (`sign_private`), and /files/ refuses these paths outright.
# Admission documents - birth certificates, identity cards, payment screenshots - are read
# only by the office, through links the admin endpoints sign as they answer.
PRIVATE_PREFIXES = ("recordings/", "admissions/")
# Recordings are long; a link signed when the list was loaded must outlast the watching.
PRIVATE_LINK_MINUTES = 240

_client = None
_container_ready = False
_lock = threading.Lock()


class BlobStorageError(Exception):
    """Blob storage is unconfigured or refused the request."""


def is_configured() -> bool:
    return bool(settings.AZURE_STORAGE_CONNECTION_STRING.strip())


def configuration_problems() -> list[str]:
    problems = []
    if not is_configured():
        problems.append(
            "AZURE_STORAGE_CONNECTION_STRING is not set. Copy it from the storage account's "
            "Security + networking -> Access keys."
        )
    if not (settings.AZURE_STORAGE_CONTAINER or "").strip():
        problems.append("AZURE_STORAGE_CONTAINER is blank.")
    return problems


def _service():
    global _client
    if _client is not None:
        return _client
    if not is_configured():
        raise BlobStorageError("Azure Blob Storage is not configured (AZURE_STORAGE_CONNECTION_STRING).")
    with _lock:
        if _client is None:
            from azure.storage.blob import BlobServiceClient

            _client = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
    return _client


def _container():
    """The container client, creating the (private) container the first time it is needed."""
    global _container_ready
    container = _service().get_container_client(settings.AZURE_STORAGE_CONTAINER)
    if not _container_ready:
        from azure.core.exceptions import ResourceExistsError

        try:
            container.create_container()
            logger.info("Created blob container '%s'.", settings.AZURE_STORAGE_CONTAINER)
        except ResourceExistsError:
            pass
        _container_ready = True
    return container


def lms_path(blob_name: str) -> str:
    """The address stored in the database for a blob."""
    return FILES_PREFIX + blob_name


def blob_name_from(url: str | None) -> str | None:
    """The blob behind an LMS `/files/...` address, or None for anything else."""
    if not url:
        return None
    from urllib.parse import unquote, urlparse

    path = unquote(urlparse(url).path)
    if not path.startswith(FILES_PREFIX):
        return None
    name = path[len(FILES_PREFIX):]
    return name or None


def upload(stream: BinaryIO, blob_name: str, content_type: str | None = None) -> str:
    """Uploads a stream, overwriting any blob of that name. Returns the blob name."""
    from azure.storage.blob import ContentSettings

    try:
        _container().upload_blob(
            name=blob_name,
            data=stream,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type or "application/octet-stream"),
        )
    except BlobStorageError:
        raise
    except Exception as exc:
        raise BlobStorageError(f"Blob upload of '{blob_name}' failed: {exc}") from exc
    return blob_name


def delete(blob_name: str) -> bool:
    try:
        _container().delete_blob(blob_name, delete_snapshots="include")
        return True
    except Exception as exc:
        logger.warning("Could not delete blob '%s': %s", blob_name, exc)
        return False


def size_of(blob_name: str) -> int | None:
    try:
        return int(_container().get_blob_client(blob_name).get_blob_properties().size)
    except Exception:
        return None


def is_private(blob_name: str | None) -> bool:
    return bool(blob_name) and blob_name.startswith(PRIVATE_PREFIXES)


def sign_private(url: str | None) -> str | None:
    """
    A private file's stored /files/ address, turned into a signed link for somebody the
    caller has already decided may see it. Any other address comes back unchanged, and so
    does a private one when storage is unreachable - the viewer then gets a refusal from
    /files/ rather than the whole list failing.
    """
    blob_name = blob_name_from(url)
    if not is_private(blob_name):
        return url
    try:
        return read_url(blob_name, minutes=PRIVATE_LINK_MINUTES)
    except Exception as exc:
        logger.warning("Could not sign a link for '%s': %s", blob_name, exc)
        return url


def read_url(blob_name: str, download: bool = False, minutes: int | None = None) -> str:
    """A read link for one blob, valid for AZURE_BLOB_LINK_MINUTES unless `minutes` says."""
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    service = _service()
    credential = service.credential
    account_key = getattr(credential, "account_key", None)
    if not account_key:
        raise BlobStorageError(
            "The storage connection string carries no account key, so read links cannot be "
            "signed. Use the connection string from Access keys."
        )

    disposition = None
    if download:
        filename = blob_name.rsplit("/", 1)[-1]
        disposition = f"attachment; filename*=UTF-8''{quote(filename)}"

    lifetime = minutes if minutes is not None else settings.AZURE_BLOB_LINK_MINUTES
    expiry = datetime.now(timezone.utc) + timedelta(minutes=max(int(lifetime), 1))
    sas = generate_blob_sas(
        account_name=service.account_name,
        container_name=settings.AZURE_STORAGE_CONTAINER,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
        # A few minutes' slack for a client whose clock runs ahead of Azure's.
        start=datetime.now(timezone.utc) - timedelta(minutes=5),
        content_disposition=disposition,
    )
    blob_url = service.get_blob_client(settings.AZURE_STORAGE_CONTAINER, blob_name).url
    return f"{blob_url}?{sas}"


def check_access() -> dict:
    """Live probe: can the container be reached (and created if absent)?"""
    problems = configuration_problems()
    info = {
        "provider": "AZURE_BLOB",
        "container": settings.AZURE_STORAGE_CONTAINER,
        "problems": problems,
    }
    if problems:
        return {**info, "ok": False, "detail": problems[0]}
    try:
        container = _container()
        container.get_container_properties()
        return {
            **info,
            "ok": True,
            "account": _service().account_name,
            "detail": f"Container '{settings.AZURE_STORAGE_CONTAINER}' is reachable.",
        }
    except Exception as exc:
        return {**info, "ok": False, "detail": f"Could not reach the blob container: {exc}"}


def describe() -> dict:
    info = {
        "provider": "AZURE_BLOB",
        "configured": is_configured(),
        "container": settings.AZURE_STORAGE_CONTAINER,
        "link_minutes": settings.AZURE_BLOB_LINK_MINUTES,
        "problems": configuration_problems(),
    }
    if is_configured():
        try:
            info["account"] = _service().account_name
        except Exception as exc:
            info["problems"].append(str(exc))
    return info
