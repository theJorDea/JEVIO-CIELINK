import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Barrier
from uuid import uuid4

import pytest

from cielink.crm import OperationConflictError, OperationStore


@pytest.fixture(params=['sqlite', 'postgres'])
def database_url(request):
    if request.param == 'sqlite':
        with NamedTemporaryFile(suffix='.sqlite3', delete=False) as db:
            path = Path(db.name)
        try:
            yield f'sqlite:///{path}'
        finally:
            path.unlink()
        return
    url = os.getenv('CIELINK_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('CIELINK_TEST_POSTGRES_URL not set')
    import psycopg

    migrations = Path(__file__).resolve().parents[1] / 'migrations'
    with psycopg.connect(url, autocommit=True) as conn:
        for name in ('002_operations.sql', '003_spoken_reference.sql'):
            conn.execute((migrations / name).read_text(encoding='utf-8'))
    yield url


@pytest.fixture
def payload():
    return dict(session_id=str(uuid4()), operation_key=str(uuid4()), operation_type='order',
                confirmed_terms={'house_id': 'test', 'offer': {'revision': 2, 'price': 399}})


@pytest.mark.parametrize('kind', ['order', 'coverage_request', 'handoff'])
def test_durable_snapshot_and_stable_receipt(database_url, payload, kind):
    payload['operation_type'] = kind
    store = OperationStore(database_url)
    receipt = store.record_operation(**payload)
    assert receipt.operation_type == kind
    assert receipt.operation_key == payload['operation_key']
    payload['confirmed_terms']['offer']['price'] = 0
    assert receipt.confirmed_terms['offer']['price'] == 399
    reopened = OperationStore(database_url)
    assert reopened.lookup_operation(receipt.operation_key) == receipt
    assert 1 <= receipt.spoken_reference <= 99999999
    assert reopened.lookup_reference(receipt.spoken_reference) == receipt
    payload['confirmed_terms'] = {'offer': {'price': 399, 'revision': 2}, 'house_id': 'test'}
    assert reopened.record_operation(**payload) == receipt


@pytest.mark.parametrize('change', [{'operation_type': 'handoff'}, {'confirmed_terms': {'price': 1}}])
def test_conflict_preserves_original(database_url, payload, change):
    store = OperationStore(database_url)
    original = store.record_operation(**payload)
    for attempt in (store.record_operation, store.recover_operation):
        with pytest.raises(OperationConflictError):
            attempt(**(payload | change))
    assert store.lookup_operation(payload['operation_key']) == original


@pytest.mark.parametrize('mixed', [False, True])
def test_simultaneous_inserts(database_url, payload, mixed):
    store = OperationStore(database_url)
    barrier = Barrier(8)

    def submit(index):
        kind = 'handoff' if mixed and index % 2 else 'order'
        barrier.wait(timeout=10)
        try:
            return store.record_operation(**(payload | {'operation_type': kind}))
        except OperationConflictError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(8)))
    persisted = store.lookup_operation(payload['operation_key'])
    assert persisted is not None
    assert results.count(persisted) == (4 if mixed else 8)
    assert results.count(None) == (4 if mixed else 0)


@pytest.mark.parametrize('committed', [False, True])
def test_timeout_lookup_then_retry_if_missing(database_url, payload, monkeypatch, committed):
    store = OperationStore(database_url)
    with pytest.raises(TimeoutError):
        if committed:
            original = store.record_operation(**payload)
        raise TimeoutError('response unavailable')
    reopened = OperationStore(database_url)
    if committed:
        monkeypatch.setattr(reopened, 'record_operation', lambda **kw: pytest.fail('unexpected retry'))
    receipt = reopened.recover_operation(**payload)
    if committed:
        assert receipt == original
    assert OperationStore(database_url).record_operation(**payload) == receipt


def test_session_order_and_scope(database_url, payload):
    store = OperationStore(database_url)
    barrier = Barrier(6)

    def submit(index):
        barrier.wait(timeout=10)
        return store.record_operation(**(payload | {'operation_key': str(uuid4())}))

    with ThreadPoolExecutor(max_workers=6) as executor:
        receipts = list(executor.map(submit, range(6)))
    assert sorted(r.sequence_no for r in receipts) == list(range(1, 7))
    assert all(r.session_id == payload['session_id'] for r in receipts)
    other = payload | {'session_id': str(uuid4())}
    for attempt in (store.record_operation, store.recover_operation):
        with pytest.raises(OperationConflictError):
            attempt(**(other | {'operation_key': receipts[0].operation_key}))
    assert store.record_operation(**other).sequence_no == 1


def test_reference_is_global_under_concurrent_writes(database_url, payload):
    store = OperationStore(database_url)
    barrier = Barrier(8)

    def submit(index):
        barrier.wait(timeout=10)
        return store.record_operation(**(payload | {'operation_key': str(uuid4()), 'session_id': str(uuid4())}))

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(submit, range(8)))
    assert len({r.spoken_reference for r in receipts}) == 8
    assert all(r.sequence_no == 1 and store.lookup_reference(r.spoken_reference) == r for r in receipts)


@pytest.mark.parametrize('change', [{'operation_type': 'unknown'}, {'operation_key': ' '},
                                    {'confirmed_terms': {1: 'invalid'}},
                                    {'confirmed_terms': {'price': float('nan')}}])
def test_invalid_payload_not_stored(database_url, payload, change):
    store = OperationStore(database_url)
    with pytest.raises(ValueError):
        store.record_operation(**(payload | change))
    assert store.lookup_operation(change.get('operation_key', payload['operation_key'])) is None
