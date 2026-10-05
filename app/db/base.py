class Base:
    """
    Lightweight base class for model objects. Nothing uses an ORM: the document store
    (`app.core.firebase`, Azure SQL underneath) is read and written as plain dicts.
    """
    pass
