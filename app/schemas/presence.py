from datetime import datetime

from pydantic import BaseModel


class UserPresenceOut(BaseModel):
    """
    One user's last sign of life. `is_online` is a touch within the last three minutes:
    a request, or the heartbeat an open tab sends once a minute. Users never seen since
    tracking began have no row at all.
    """
    user_id: int
    last_seen_at: datetime | None = None
    is_online: bool = False
