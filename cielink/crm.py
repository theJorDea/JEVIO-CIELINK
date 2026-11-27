"""Durable local CRM operations; a receipt is returned only after commit."""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

OperationType = Literal["order", "coverage_request", "handoff"]
_TYPES = ("order", "coverage_request", "handoff")
_SCHEMA = Path(__file__).resolve().parents[1] / "migrations" / "002_operations.sql"
_SELECT = """SELECT receipt_id, session_id, sequence_no, operation_type, confirmed_terms
             FROM crm_operations WHERE operation_key = ?"""


class OperationConflictError(ValueError):
    """The key is already bound to a different confirmed operation."""


@dataclass(frozen=True)
class OperationReceipt:
    receipt_id: str
    operation_key: str
    session_id: str
    sequence_no: int
    operation_type: OperationType
    confirmed_terms: dict


def _snapshot(terms: dict) -> str:
    if not isinstance(terms, dict):
        raise ValueError("confirmed_terms must be a JSON object")
    snapshot = json.dumps(terms, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if json.loads(snapshot) != terms:
        raise ValueError("confirmed_terms must contain only JSON values and string keys")
    return snapshot


class OperationStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        if database_url.startswith("sqlite:///"):
            path = database_url.removeprefix("sqlite:///")
            if not path or path == ":memory:":
                raise ValueError("SQLite needs a durable file path")
            self._sqlite_path = Path(path).resolve()
            with self._connection() as conn:
                conn.execute(_SCHEMA.read_text(encoding="utf-8"))
        elif database_url.startswith(("postgresql://", "postgres://")):
            self._sqlite_path = None
        else:
            raise ValueError("expected sqlite:///path or postgresql:// DSN")

    @contextmanager
    def _connection(self):
        if self._sqlite_path is not None:
            with closing(sqlite3.connect(self._sqlite_path)) as conn, conn:
                yield conn
        else:
            import psycopg

            with psycopg.connect(self.database_url) as conn:
                yield conn

    def _sql(self, query: str) -> str:
        return query if self._sqlite_path is not None else query.replace("?", "%s")

    def lookup_operation(self, operation_key: str) -> OperationReceipt | None:
        with self._connection() as conn:
            row = conn.execute(self._sql(_SELECT), (operation_key,)).fetchone()
        if row is None:
            return None
        receipt_id, session_id, number, kind, snapshot = row
        return OperationReceipt(receipt_id, operation_key, session_id, number, kind, json.loads(snapshot))

    def record_operation(self, *, session_id: str, operation_key: str, operation_type: OperationType,
                         confirmed_terms: dict) -> OperationReceipt:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a nonempty string")
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("operation_key must be a nonempty string")
        if operation_type not in _TYPES:
            raise ValueError("unknown operation_type")
        snapshot = _snapshot(confirmed_terms)
        with self._connection() as conn:
            if self._sqlite_path is not None:
                conn.execute('BEGIN IMMEDIATE')
            else:
                conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (session_id,))
            conn.execute(self._sql("""
                INSERT INTO crm_operations (operation_key, operation_type, confirmed_terms,
                                            receipt_id, session_id, sequence_no)
                SELECT ?, ?, ?, ?, ?, COALESCE(MAX(sequence_no), 0) + 1
                FROM crm_operations WHERE session_id = ?
                ON CONFLICT (operation_key) DO NOTHING
            """), (operation_key, operation_type, snapshot, str(uuid4()), session_id, session_id))
            receipt_id, stored_session, number, stored_type, stored_snapshot = conn.execute(
                self._sql(_SELECT), (operation_key,)
            ).fetchone()
            if (stored_session, stored_type, stored_snapshot) != (session_id, operation_type, snapshot):
                raise OperationConflictError("operation_key already used for a different payload")
        return OperationReceipt(receipt_id, operation_key, session_id, number, stored_type, json.loads(stored_snapshot))

    def recover_operation(self, *, session_id: str, operation_key: str, operation_type: OperationType,
                          confirmed_terms: dict) -> OperationReceipt:
        """After an ambiguous timeout, look up first; retry only a missing key."""
        receipt = self.lookup_operation(operation_key)
        if receipt is None:
            return self.record_operation(session_id=session_id, operation_key=operation_key,
                                         operation_type=operation_type, confirmed_terms=confirmed_terms)
        if (receipt.session_id, receipt.operation_type, _snapshot(receipt.confirmed_terms)) != (
            session_id, operation_type, _snapshot(confirmed_terms)
        ):
            raise OperationConflictError("operation_key already used for a different payload")
        return receipt
