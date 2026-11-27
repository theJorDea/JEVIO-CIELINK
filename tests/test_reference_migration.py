"""Upgrade a populated v2 database, then reopen it without renumbering."""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from cielink.crm import OperationStore

MIGRATIONS = Path(__file__).resolve().parents[1] / 'migrations'
V2 = (MIGRATIONS / '002_operations.sql').read_text(encoding='utf-8')
SEED = "INSERT INTO crm_operations VALUES ('one','order','{}','uuid1','s',1), ('two','handoff','{}','uuid2','s',2)"


def test_sqlite_upgrade(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute(V2)
        db.execute(SEED)
    def migrate(_):
        store = OperationStore(f'sqlite:///{path}')
        return [store.lookup_operation(key) for key in ('one', 'two')]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(migrate, range(4)))
    first, second = results[0]
    assert results.count(results[0]) == 4 and first.spoken_reference != second.spoken_reference
    assert (first.receipt_id, second.receipt_id) == ('uuid1', 'uuid2')
    store = OperationStore(f'sqlite:///{path}')
    assert store.lookup_reference(first.spoken_reference) == first
    new = store.record_operation(session_id='s', operation_type='order', operation_key='three', confirmed_terms={})
    assert new.sequence_no == 3 and new.spoken_reference not in (first.spoken_reference, second.spoken_reference)


def test_postgres_upgrade():
    if not (url := os.getenv('CIELINK_TEST_POSTGRES_URL')):
        pytest.skip('CIELINK_TEST_POSTGRES_URL not set')
    import psycopg
    schema = 'migration_' + uuid4().hex
    migration = (MIGRATIONS / '003_spoken_reference.sql').read_text(encoding='utf-8')
    with psycopg.connect(url, autocommit=True) as db:
        db.execute(f'CREATE SCHEMA {schema}')
        try:
            db.execute(f'SET search_path TO {schema}')
            db.execute(V2)
            db.execute(SEED)
            db.execute(migration)
            before = db.execute('SELECT receipt_id, spoken_reference FROM crm_operations ORDER BY receipt_id').fetchall()
            db.execute(migration)
            assert before == db.execute('SELECT receipt_id, spoken_reference FROM crm_operations ORDER BY receipt_id').fetchall()
            assert [r[0] for r in before] == ['uuid1', 'uuid2'] and before[0][1] != before[1][1]
            new, = db.execute("INSERT INTO crm_operations (operation_key,operation_type,confirmed_terms,receipt_id,session_id,sequence_no) VALUES ('three','order','{}','uuid3','s',3) RETURNING spoken_reference").fetchone()
            assert new not in [r[1] for r in before]
        finally:
            db.execute('ROLLBACK')  # migration failure may have left its transaction aborted
            db.execute(f'DROP SCHEMA {schema} CASCADE')
