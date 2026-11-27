import json
from contextlib import closing
from pathlib import Path
from shutil import rmtree
import sqlite3
from tempfile import gettempdir
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cielink.review import ReviewStore, main, router


@pytest.fixture
def scratch():
    directory = Path(gettempdir()) / ('review-' + uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        rmtree(directory)


@pytest.fixture
def store(scratch, monkeypatch):
    tmp_path = scratch
    monkeypatch.setenv('CIELINK_REVIEW_DB', str(tmp_path / 'calls.sqlite3'))
    monkeypatch.setenv('CIELINK_RECORDINGS_DIR', str(tmp_path))
    monkeypatch.delenv('CIELINK_ALLOW_RAW_RECORDINGS', raising=False)
    return ReviewStore()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, base_url='http://localhost', client=('127.0.0.1', 1234))


def test_persistence_and_post_call_review(store, client):
    sid = store.start_session('web-1')
    store.append_event(sid, 'partial', 2, 'uncertain')
    store.append_event(sid, 'final', 9, 'hello')
    store.append_event(sid, 'playback_interrupted', 21)
    assert store.analyze_next() is None
    store.finish_session(sid, 'handoff', receipt={'id': 4}, offer={'advance': 100}, coverage_status='available')
    assert client.get('/api/sessions').json() == [dict(id=sid, status='finished', outcome='handoff', analysis_status='pending')]
    with pytest.raises(ValueError):
        store.append_event(sid, 'final', 99, 'too late')
    assert main(['--db', str(store.db)]) == 0
    detail = client.get('/api/sessions/web-1').json()
    assert detail['analysis_status'] == 'complete'
    assert detail['events'][1] == dict(type='final', offset_ms=9, text='hello')
    assert detail['offer'] == {'advance': 100} and detail['receipt'] == {'id': 4}
    assert detail['coverage_status'] == 'available'
    assert [(f['start_ms'], f['end_ms']) for f in detail['findings']] == [(9, 9), (21, 21)]
    assert 'hello' in detail['findings'][0]['message']
    assert ReviewStore().analyze_next() is None
    assert client.get('/api/sessions/not-found').status_code == 404


def test_delivery_audit_uses_only_bound_verified_evidence(store):
    sid = store.start_session()
    offer = {'periods': [{'monthly_price': '780.00'}]}
    def deliver(offset, text, bound_offer):
        store.append_event(sid, 'offer_verified', offset, json.dumps(bound_offer))
        store.append_event(sid, 'playback_ended', offset, text)

    store.append_event(sid, 'reply_generated', 1, 'Ежемесячно 999 рублей.')
    store.append_event(sid, 'playback_interrupted', 2, 'Ежемесячно 999 рублей.')
    deliver(3, 'Сегодня внести 100 рублей, ежемесячно 700 рублей. Оформить заявку?', offer)
    deliver(4, '780,00 рублей в месяц.', offer)
    deliver(5, 'Ежемесячно 700 рублей.', None)
    deliver(6, 'Ежемесячно 700 рублей.', {'periods': [{'monthly_price': '700'}, {'monthly_price': '800'}]})
    deliver(7, 'Не передам вопрос специалисту. Передать вопрос специалисту?', None)
    deliver(8, 'Передам вопрос специалисту.', None)
    deliver(9, 'Сохранено. Номер abc.', None)
    store.append_event(sid, 'text_displayed', 10, 'Заявка на подключение оформлена.')
    deliver(11, 'Сохранено. Номер wrong.', None)
    store.finish_session(sid, 'order', offer=offer, receipt={'receipt_id': 'abc', 'operation_type': 'order'})
    assert store.analyze_next() == 'complete'
    findings = {f['start_ms']: f['code'] for f in store.detail(sid)['findings'] if 'code' in f}
    assert findings == {3: 'price_mismatch', 4: 'price_match', 5: 'price_unverified',
                        6: 'price_unverified', 8: 'operation_mismatch', 9: 'receipt_match',
                        10: 'operation_match', 11: 'receipt_mismatch'}


def test_watch_processes_later_call_without_inventing_receipt(store, monkeypatch):
    sid = store.start_session()
    calls = []
    def idle(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt
        store.append_event(sid, 'playback_ended', 5, 'Запрос на проверку дома отправлен.')
        store.finish_session(sid, 'disconnect')
    monkeypatch.setattr('cielink.review.time.sleep', idle)
    assert main(['--watch', '--interval', '0.5']) == 0
    assert calls == [0.5, 0.5]
    assert store.detail(sid)['findings'][0]['code'] == 'operation_unverified'


def test_missing_and_invalid_evidence(store):
    for sid in ('missing', 'invalid'):
        store.start_session(sid)
        store.append_event(sid, 'partial', 0, 'partial is not a final statement')
        if sid == 'invalid':
            # Simulate a pre-existing corrupt evidence row, not a model verdict.
            with closing(sqlite3.connect(store.db)) as c, c:
                c.execute("INSERT INTO events(session_id,type,offset_ms,text) VALUES ('invalid','final',0,?)", (b'corrupt',))
        store.finish_session(sid, 'disconnect')
    assert main([]) == 1
    for sid, status in [('missing', 'missing'), ('invalid', 'error')]:
        detail = store.detail(sid)
        assert detail['analysis_status'] == status and detail['findings'] == []


def test_recordings_scoped_local_opt_in(store, scratch, client, monkeypatch):
    tmp_path = scratch
    sid = store.start_session()
    (tmp_path / 'customer.wav').write_bytes(b'RIFF')
    store.add_track(sid, 'customer', 'customer.wav', 420)
    with pytest.raises(ValueError):
        store.add_track(sid, 'customer', '../outside.wav')
    url = f'/api/sessions/{sid}'
    assert client.get(url).json()['tracks'] == [dict(role='customer', url=None, start_ms=420)]
    assert client.get(url + '/tracks/1').status_code == 403
    monkeypatch.setenv('CIELINK_ALLOW_RAW_RECORDINGS', '1')
    track_url = client.get(url).json()['tracks'][0]['url']
    response = client.get(track_url)
    assert response.content == b'RIFF' and response.headers['cache-control'] == 'no-store'
    assert client.get('/api/sessions/other/tracks/1').status_code == 404
    assert client.get(track_url, headers={'Host': 'untrusted.example'}).status_code == 403
    (tmp_path / 'customer.wav').unlink()
    assert client.get(track_url).status_code == 404
    remote = TestClient(client.app, base_url='http://localhost', client=('192.0.2.1', 1234))
    assert remote.get(url, headers={'X-Forwarded-For': '127.0.0.1'}).status_code == 403
    monkeypatch.setenv('CIELINK_MODE', 'production')
    assert client.get(url).status_code == 403
