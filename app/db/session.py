from collections.abc import Generator


def get_db() -> Generator[None, None, None]:
    """
    FastAPI dependency placeholder for Firebase-only usage.
    """
    yield None
