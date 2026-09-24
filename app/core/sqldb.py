"""
Azure SQL as a document store, behind the same interface as the Firestore service.

Every service in this application reads and writes plain dicts through nine methods on
`FirestoreService` - get, get many, query by one field, list, add (merge), create (claim),
delete - and never anything Firestore-specific. That is the whole reason this module can
exist: it implements the same nine methods over Azure SQL, one table per collection, one
JSON document per row, and the fifty-odd files that use them do not change.

The database may be shared. Everything here lives inside one schema (`DB_SCHEMA`, "h7lms" by
default), and nothing outside it is created, altered or even read - a server that also hosts
another application's tables in `dbo` is the expected case, not a hazard.

**Why JSON rows rather than proper tables.** Because the services were written against a
document model and work: a relational schema would mean rewriting every one of them for no
functional gain, and the thing that actually costs money on Firestore - a per-read quota -
does not exist here. The fields each collection is queried by become persisted computed
columns with an index, so a lookup by `student_id` is a seek, not a scan of every document.

**Merge semantics.** `add_document` behaves like Firestore's `set(merge=True)`: nested maps
merge, everything else is replaced, absent fields are kept. It is a read-modify-write inside
a transaction with an update lock, which is what makes two concurrent partial updates to one
document both land.
"""

import json
import logging
import re
import threading
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable

from app.core.config import settings

logger = logging.getLogger("sqldb")

try:
    import pyodbc
except ImportError:  # pragma: no cover - only when the driver is not installed
    pyodbc = None


class DatabaseUnavailable(Exception):
    """The SQL server could not be reached or refused the operation. A 503, not a bug."""


# Fields each collection is queried by (`query_documents` / `get_document_by_field` in the
# services). Each becomes a persisted computed column with an index. A collection absent
# here is still queryable on any field - it just scans.
INDEXED_FIELDS: dict[str, list[str]] = {
    "users": ["role", "email", "firebase_uid", "academic_year_id", "admission_category_id"],
    "class_rooms": ["code"],
    "subjects": ["code"],
    "teacher_subject_class_mappings": ["class_id", "teacher_id"],
    "class_teacher_mappings": ["class_id", "teacher_id"],
    "student_enrollments": ["class_id", "student_id"],
    "attendance_records": ["class_id", "student_id"],
    "topics_covered": ["class_id"],
    "live_meetings": ["class_id", "status", "teacher_id"],
    "study_materials": ["class_id", "teacher_id"],
    "exam_grades": ["student_id"],
    "timetable_entries": ["class_id", "subject_id", "teacher_id"],
    "reminder_log": ["user_id"],
    "recording_log": ["meeting_id"],
    "exams": ["class_id", "enrollment_id", "program", "student_id", "teacher_id"],
    "exam_submissions": ["exam_id"],
    "report_cards": ["class_id", "student_id"],
    "tuition_enrollments": ["student_id", "teacher_id"],
    "tuition_slots": ["enrollment_id", "student_id", "teacher_id"],
    "tuition_sessions": ["enrollment_id", "slot_id", "status", "student_id", "teacher_id"],
    "tuition_package_assignments": ["package_id", "student_id"],
    "tuition_invoices": ["student_id"],
    "admission_requests": ["status", "program", "academic_year_id", "class_id"],
    "parent_links": ["parent_id", "student_id"],
    "notice_reads": ["notice_id", "user_id"],
    "fee_invoices": ["student_id"],
    "fee_receipts": ["student_id", "academic_year_id"],
    "payment_intents": ["invoice_id"],
    "extra_class_requests": ["requested_by"],
    "leave_requests": ["teacher_id"],
    "homework_assignments": ["teacher_id"],
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISS = object()


def _ident(name: str) -> str:
    """A validated SQL identifier, bracket-quoted. Never interpolate anything else."""
    if not _IDENT.match(name or ""):
        raise ValueError(f"Invalid identifier: {name!r}")
    return f"[{name}]"


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(document: dict) -> str:
    return json.dumps(document, default=_json_default, ensure_ascii=False)


def scalar_text(value: Any) -> str | None:
    """
    A Python value as `JSON_VALUE` renders it, for an equality comparison.

    JSON_VALUE returns text: `123` for the number, `true` for the boolean. Comparing as
    text is what makes a stored integer match a query by integer without the query having
    to know how the field was stored - and, as a side effect, lets an id stored as "123"
    by an older write match too, which Firestore would not have done.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return scalar_text(value.value)
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return str(value)


def deep_merge(base: dict, updates: dict) -> dict:
    """Firestore's `set(merge=True)`: nested maps merge, everything else is replaced."""
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------------------

def connection_string() -> str:
    if settings.DB_CONNECTION_STRING:
        return settings.DB_CONNECTION_STRING
    if not (settings.DB_SERVER and settings.DB_NAME and settings.DB_USER):
        return ""
    server = settings.DB_SERVER
    if not server.startswith("tcp:"):
        server = f"tcp:{server}"
    if "," not in server:
        server = f"{server},1433"
    return (
        f"Driver={{{settings.DB_DRIVER}}};Server={server};Database={settings.DB_NAME};"
        f"Uid={settings.DB_USER};Pwd={settings.DB_PASSWORD};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )


def is_configured() -> bool:
    return pyodbc is not None and bool(connection_string())


_local = threading.local()

# SQLSTATEs that mean the connection itself is gone, and one reconnect is worth a try.
_CONNECTION_LOST = ("08S01", "08001", "08003", "08007", "HYT00", "HYT01")


def _connect():
    if pyodbc is None:
        raise DatabaseUnavailable("pyodbc is not installed; cannot use the Azure SQL backend.")
    cs = connection_string()
    if not cs:
        raise DatabaseUnavailable("Azure SQL is not configured (DB_SERVER, DB_NAME, DB_USER).")
    try:
        cn = pyodbc.connect(cs, autocommit=True)
    except pyodbc.Error as exc:
        raise DatabaseUnavailable(f"Could not connect to Azure SQL: {exc}") from exc
    cn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-16le")
    cn.setencoding(encoding="utf-16le")
    return cn


def connection():
    """
    The calling thread's connection, opened on first use.

    Per thread rather than pooled: pyodbc connections are not thread-safe, the request
    handlers and the background sweeps each run on their own threads, and a connection per
    thread is the simplest arrangement that never shares one.
    """
    cn = getattr(_local, "cn", None)
    if cn is None:
        cn = _connect()
        _local.cn = cn
    return cn


def _drop_connection() -> None:
    cn = getattr(_local, "cn", None)
    _local.cn = None
    if cn is not None:
        try:
            cn.close()
        except Exception:  # pragma: no cover
            pass


def run(sql: str, params: tuple = (), *, fetch: str | None = None):
    """
    Executes one statement, reconnecting once if the connection has dropped.

    `fetch` is "all", "one" or None. Every other pyodbc error - a bad query, a constraint -
    surfaces as `DatabaseUnavailable` too, because from the caller's point of view the
    document could not be read or written and the reason is in the log.
    """
    for attempt in (1, 2):
        try:
            cursor = connection().cursor()
            cursor.execute(sql, params)
            if fetch == "all":
                return cursor.fetchall()
            if fetch == "one":
                return cursor.fetchone()
            return cursor.rowcount
        except DatabaseUnavailable:
            raise
        except pyodbc.Error as exc:
            state = str(getattr(exc, "args", [""])[0])
            if attempt == 1 and state in _CONNECTION_LOST:
                logger.warning("SQL connection lost (%s); reconnecting.", state)
                _drop_connection()
                continue
            _drop_connection()
            raise DatabaseUnavailable(f"SQL error {state}: {exc}") from exc
    raise DatabaseUnavailable("SQL statement did not run.")  # pragma: no cover


# ---------------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------------

_prepared: set[str] = set()
_prepare_lock = threading.Lock()


def _schema() -> str:
    return _ident(settings.DB_SCHEMA)


def _table(collection: str) -> str:
    return f"{_schema()}.{_ident(collection)}"


def ensure_schema() -> None:
    """Creates the application's schema if it is missing. Touches nothing else."""
    run(
        "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = ?) "
        f"EXEC('CREATE SCHEMA {_schema()}')",
        (settings.DB_SCHEMA,),
    )


def ensure_table(collection: str) -> None:
    """
    Creates a collection's table, its computed lookup columns and their indexes, once per
    process. Idempotent and additive: an existing table gains any newly listed index and
    loses nothing.
    """
    if collection in _prepared:
        return
    with _prepare_lock:
        if collection in _prepared:
            return
        ensure_schema()
        table = _table(collection)
        qualified = f"{settings.DB_SCHEMA}.{collection}"
        run(
            f"IF OBJECT_ID(?, 'U') IS NULL CREATE TABLE {table} ("
            "  id NVARCHAR(128) NOT NULL PRIMARY KEY,"
            "  data NVARCHAR(MAX) NOT NULL CHECK (ISJSON(data) = 1),"
            "  created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),"
            "  updated_at DATETIME2 NULL"
            ")",
            (qualified,),
        )
        for field in INDEXED_FIELDS.get(collection, []):
            column = f"k_{field}"
            # CAST to a bounded width: JSON_VALUE is NVARCHAR(4000), too wide to index.
            run(
                f"IF COL_LENGTH(?, ?) IS NULL ALTER TABLE {table} ADD {_ident(column)} AS "
                f"CAST(JSON_VALUE(data, '$.{field}') AS NVARCHAR(400)) PERSISTED",
                (qualified, column),
            )
            index = f"IX_{collection}_{field}"
            run(
                "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = ? AND object_id = OBJECT_ID(?)) "
                f"EXEC('CREATE INDEX {_ident(index)} ON {table} ({_ident(column)})')",
                (index, qualified),
            )
        _prepared.add(collection)


def prepare_schema(collections: list[str]) -> None:
    """Creates every table up front - at startup, and before a migration."""
    for collection in collections:
        ensure_table(collection)


def health() -> dict:
    """What the health endpoint reports for this backend."""
    try:
        version = run("SELECT @@VERSION", fetch="one")[0].split("\n")[0]
        tables = run(
            "SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ?",
            (settings.DB_SCHEMA,), fetch="one",
        )[0]
        return {"available": True, "server": settings.DB_SERVER, "database": settings.DB_NAME,
                "schema": settings.DB_SCHEMA, "tables": int(tables), "version": version}
    except DatabaseUnavailable as exc:
        return {"available": False, "server": settings.DB_SERVER, "database": settings.DB_NAME,
                "schema": settings.DB_SCHEMA, "error": str(exc)}


# ---------------------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------------------

class SqlDocumentService:
    """
    One collection as a table of JSON rows. Same methods, same return shapes, same cache
    behaviour as `FirestoreService`; see that class for what each method promises.
    """

    def __init__(self, collection_name: str, cacheable: bool = False, *,
                 cache=None, miss: Any = _MISS, ttl: Callable[[], float] | None = None,
                 id_factory: Callable[[], int] | None = None):
        self.collection_name = collection_name
        self.cacheable = cacheable
        self._cache = cache
        # The cache's own "nothing stored" marker. It is the cache's object, not ours, so
        # it is handed in rather than assumed.
        self._miss = miss
        self._ttl_getter = ttl or (lambda: settings.REFERENCE_CACHE_TTL_SECONDS)
        self._id_factory = id_factory

    # ----------------------------------------------------------- plumbing

    @property
    def is_available(self) -> bool:
        return is_configured()

    @property
    def _ttl(self) -> float:
        return self._ttl_getter()

    def _table(self) -> str:
        ensure_table(self.collection_name)
        return _table(self.collection_name)

    def _row_to_doc(self, doc_id: str, data: str) -> dict:
        document = json.loads(data)
        document["id"] = doc_id
        return self._normalize_document(document)

    @staticmethod
    def _normalize_document(data: dict) -> dict:
        if data is None:
            return data
        normalized = dict(data)
        if "id" in normalized and isinstance(normalized["id"], str) and normalized["id"].isdigit():
            normalized["id"] = int(normalized["id"])
        return normalized

    @staticmethod
    def _payload(data: dict) -> dict:
        # The id is the row key, never part of the document body.
        return {k: v for k, v in dict(data).items() if k != "id"}

    def _cache_get(self, key: str):
        if not (self.cacheable and self._cache):
            return _MISS
        cached = self._cache.get(self.collection_name, key, self._ttl)
        return _MISS if cached is self._miss else cached

    def _cache_put(self, key: str, value) -> None:
        if self.cacheable and self._cache:
            self._cache.put(self.collection_name, key, value)

    def _cache_drop(self) -> None:
        if self.cacheable and self._cache:
            self._cache.invalidate(self.collection_name)

    # -------------------------------------------------------------- writes

    def add_document(self, doc_id: str, data: dict) -> dict:
        """Creates or merges. Firestore's `set(merge=True)`, inside a transaction."""
        self._cache_drop()
        table = self._table()
        payload = self._payload(data)
        doc_id = str(doc_id)

        cn = connection()
        cursor = cn.cursor()
        try:
            cursor.execute("BEGIN TRANSACTION")
            row = cursor.execute(
                f"SELECT data FROM {table} WITH (UPDLOCK, HOLDLOCK) WHERE id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                cursor.execute(
                    f"INSERT INTO {table} (id, data) VALUES (?, ?)", (doc_id, dumps(payload))
                )
                stored = payload
            else:
                stored = deep_merge(json.loads(row[0]), payload)
                cursor.execute(
                    f"UPDATE {table} SET data = ?, updated_at = SYSUTCDATETIME() WHERE id = ?",
                    (dumps(stored), doc_id),
                )
            cursor.execute("COMMIT TRANSACTION")
        except pyodbc.Error as exc:
            try:
                cursor.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            except Exception:  # pragma: no cover
                pass
            _drop_connection()
            raise DatabaseUnavailable(f"SQL write failed: {exc}") from exc

        normalized = self._normalize_document(dict(payload))
        normalized["id"] = doc_id
        return normalized

    def put_document(self, doc_id: str, data: dict) -> None:
        """Replaces a document outright - what a migration wants, not what a service does."""
        self._cache_drop()
        table = self._table()
        body = dumps(self._payload(data))
        run(
            f"MERGE {table} AS t USING (SELECT ? AS id) AS s ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET data = ?, updated_at = SYSUTCDATETIME() "
            "WHEN NOT MATCHED THEN INSERT (id, data) VALUES (s.id, ?);",
            (str(doc_id), body, body),
        )

    def create_document(self, doc_id: str, data: dict) -> bool:
        """Creates only if the id is free - the atomic claim the reminder sweeps rely on."""
        self._cache_drop()
        table = self._table()
        try:
            run(f"INSERT INTO {table} (id, data) VALUES (?, ?)",
                (str(doc_id), dumps(self._payload(data))))
            return True
        except DatabaseUnavailable as exc:
            # 2627 / 2601 is a duplicate key: the expected, uninteresting case.
            if "2627" in str(exc) or "2601" in str(exc) or "23000" in str(exc):
                return False
            logger.warning("Could not create '%s/%s': %s", self.collection_name, doc_id, exc)
            return False

    def delete_document(self, doc_id: str) -> bool:
        self._cache_drop()
        run(f"DELETE FROM {self._table()} WHERE id = ?", (str(doc_id),))
        return True

    # --------------------------------------------------------------- reads

    def get_document(self, doc_id: str) -> dict | None:
        doc_id = str(doc_id)
        cached = self._cache_get(doc_id)
        if cached is not _MISS:
            return cached
        row = run(f"SELECT id, data FROM {self._table()} WHERE id = ?", (doc_id,), fetch="one")
        result = self._row_to_doc(row[0], row[1]) if row else None
        self._cache_put(doc_id, result)
        return result

    def get_documents(self, doc_ids: list) -> dict[str, dict]:
        resolved: dict[str, dict] = {}
        wanted = {str(d) for d in doc_ids if d is not None}
        if not wanted:
            return resolved

        outstanding = set(wanted)
        for doc_id in list(outstanding):
            cached = self._cache_get(doc_id)
            if cached is not _MISS:
                outstanding.discard(doc_id)
                if cached is not None:
                    resolved[doc_id] = cached
        if not outstanding:
            return resolved

        table = self._table()
        ids = sorted(outstanding)
        returned: set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in run(f"SELECT id, data FROM {table} WHERE id IN ({marks})", tuple(chunk),
                           fetch="all"):
                document = self._row_to_doc(row[0], row[1])
                resolved[row[0]] = document
                returned.add(row[0])
                self._cache_put(row[0], document)
        for missing in outstanding - returned:
            self._cache_put(missing, None)
        return resolved

    def get_document_by_field(self, field: str, value: Any) -> dict | None:
        matches = self.query_documents(field, "==", value)
        return matches[0] if matches else None

    def get_next_numeric_id(self) -> int:
        if self._id_factory is None:
            raise RuntimeError("SqlDocumentService needs an id_factory")
        return self._id_factory()

    def query_documents(self, field: str, op: str, value: Any) -> list[dict]:
        if op != "==":
            raise ValueError(f"Only equality queries are supported on the SQL backend (got {op!r}).")
        if not _IDENT.match(field or ""):
            raise ValueError(f"Invalid field name: {field!r}")

        key = f"__query__:{field}:{op}:{value!r}"
        cached = self._cache_get(key)
        if cached is not _MISS and cached is not None:
            return json.loads(json.dumps(cached))

        table = self._table()
        indexed = field in INDEXED_FIELDS.get(self.collection_name, [])
        column = _ident(f"k_{field}") if indexed else f"JSON_VALUE(data, '$.{field}')"
        text = scalar_text(value)
        if text is None:
            rows = run(f"SELECT id, data FROM {table} WHERE {column} IS NULL ORDER BY id",
                       fetch="all")
        else:
            rows = run(f"SELECT id, data FROM {table} WHERE {column} = ? ORDER BY id", (text,),
                       fetch="all")
        results = [self._row_to_doc(r[0], r[1]) for r in rows]
        self._cache_put(key, json.loads(json.dumps(results)))
        return results

    def list_all(self) -> list[dict]:
        cached = self._cache_get("__list__")
        if cached is not _MISS and cached is not None:
            return json.loads(json.dumps(cached))
        rows = run(f"SELECT id, data FROM {self._table()} ORDER BY id", fetch="all")
        results = [self._row_to_doc(r[0], r[1]) for r in rows]
        self._cache_put("__list__", json.loads(json.dumps(results)))
        return results

    def count(self) -> int:
        return int(run(f"SELECT COUNT(*) FROM {self._table()}", fetch="one")[0])
