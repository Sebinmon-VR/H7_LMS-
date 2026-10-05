from collections.abc import Generator


def get_db() -> Generator[None, None, None]:
    """
    FastAPI dependency placeholder. The document store needs no per-request session; see
    `app.core.firebase` for the collections.
    """
    yield None
