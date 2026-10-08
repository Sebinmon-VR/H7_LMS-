"""
External links shared into a library: a web page, or a YouTube video the student can watch
in place.

Only http(s) addresses are accepted. A YouTube address in any of its usual shapes (watch,
youtu.be, shorts, live, embed, the mobile and music sites) is recognised and its video id
kept, so the library can show a thumbnail and play it in an embedded player instead of
sending the student off to youtube.com.
"""
import re
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

LINK_KIND_YOUTUBE = "YOUTUBE"
LINK_KIND_WEB = "WEB"

_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/", "/e/")


def youtube_video_id(url: str) -> str | None:
    """The 11-character video id in a YouTube address, or None for anything else."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    candidate = None

    if host in _SHORT_HOSTS:
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path in ("/watch", "/watch/"):
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            for prefix in _PATH_PREFIXES:
                if parsed.path.startswith(prefix):
                    candidate = parsed.path[len(prefix):].split("/")[0]
                    break

    return candidate if candidate and _VIDEO_ID.match(candidate) else None


def parse_link(url: str | None) -> dict:
    """
    Validates a shared address and describes it.

    Returns {external_url, link_kind, youtube_id}; raises 400 for anything that is not a
    plain http(s) address, so a typo or a javascript: link never reaches a student.
    """
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="A link address is required.")
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "." not in parsed.hostname:
        raise HTTPException(status_code=400, detail="Enter a full web address, such as https://example.com/page.")

    video_id = youtube_video_id(url)
    return {
        "external_url": url,
        "link_kind": LINK_KIND_YOUTUBE if video_id else LINK_KIND_WEB,
        "youtube_id": video_id,
    }
