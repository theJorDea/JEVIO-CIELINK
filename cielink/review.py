"""Local training review store and API. Run `python -m cielink.review` after calls."""
import argparse
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
import math
import time
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', outcome TEXT,
 analysis_status TEXT NOT NULL DEFAULT 'pending', receipt TEXT, offer TEXT,
 coverage_status TEXT, findings TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 type TEXT NOT NULL, offset_ms INTEGER NOT NULL CHECK(offset_ms >= 0), text TEXT);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id,offset_ms);
CREATE TABLE IF NOT EXISTS tracks (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL,
 path TEXT NOT NULL, start_ms INTEGER NOT NULL CHECK(start_ms >= 0));
'''
_SUMMARY = 'id,status,outcome,analysis_status'


def _recording_root():
    return Path(os.getenv('CIELINK_RECORDINGS_DIR', 'local-data/recordings')).resolve()


def _wav(root, path):
    full = (root / path).resolve()
    if not full.is_relative_to(root) or full.suffix.lower() != '.wav':
        raise ValueError('Expected WAV under the recordings directory')
    return full


def _raw_enabled():
    return os.getenv('CIELINK_ALLOW_RAW_RECORDINGS') == '1'


class ReviewStore:
    def __init__(self, db=None):
        self.db = Path(db or os.getenv('CIELINK_REVIEW_DB', 'local-data/review.sqlite3'))
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.db, timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            yield conn

    def start_session(self, session_id=None):
        sid = uuid4().hex if session_id is None else session_id
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sid):
            raise ValueError('Invalid session id')
        with self._connect() as c:
            c.execute('INSERT INTO sessions(id) VALUES (?)', (sid,))
        return sid

    @contextmanager
    def _active(self, session_id):
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute("SELECT 1 FROM sessions WHERE id=? AND status='active'", (session_id,)).fetchone():
                raise ValueError('Session is absent or finished')
            yield c

    def append_event(self, session_id, type, offset_ms, text=None):
        with self._active(session_id) as c:
            c.execute('INSERT INTO events(session_id,type,offset_ms,text) VALUES (?,?,?,?)',
                      (session_id, type, offset_ms, text))

    def add_track(self, session_id, role, path, start_ms=0):
        root = _recording_root()
        relative = _wav(root, path).relative_to(root)
        with self._active(session_id) as c:
            c.execute('INSERT INTO tracks(session_id,role,path,start_ms) VALUES (?,?,?,?)',
                      (session_id, role, str(relative), start_ms))

    def finish_session(self, session_id, outcome, *, receipt=None, offer=None, coverage_status=None):
        with self._active(session_id) as c:
            c.execute("UPDATE sessions SET status='finished',outcome=?,receipt=?,offer=?,coverage_status=? WHERE id=?",
                      (outcome, json.dumps(receipt), json.dumps(offer), coverage_status, session_id))

    def list_sessions(self):
        with self._connect() as c:
            return [dict(row) for row in c.execute(f'SELECT {_SUMMARY} FROM sessions ORDER BY rowid DESC')]

    def detail(self, session_id):
        with self._connect() as c:
            c.execute('BEGIN')
            row = c.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            for key in ('receipt', 'offer', 'findings'):
                result[key] = json.loads(result[key]) if result[key] is not None else None
            result['events'] = [dict(e) for e in self._events(c, session_id)]
            result['tracks'] = [{'role': t['role'], 'start_ms': t['start_ms'],
                                 'url': f"/api/sessions/{session_id}/tracks/{t['id']}" if _raw_enabled() else None}
                                for t in c.execute('SELECT id,role,start_ms FROM tracks WHERE session_id=? ORDER BY start_ms,id', (session_id,))]
            return result

    @staticmethod
    def _events(c, sid):
        return c.execute('SELECT type,offset_ms,text FROM events WHERE session_id=? ORDER BY offset_ms,id', (sid,))

    def analyze_next(self):
        # Analyze one finished session transactionally. A terminated worker leaves it pending.
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT id,receipt FROM sessions WHERE status='finished' AND analysis_status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            sid = row['id']
            try:
                findings = _review(self._events(c, sid), json.loads(row['receipt'] or 'null'))
                status = 'complete' if findings else 'missing'
            except (TypeError, ValueError):
                findings, status = [], 'error'
            c.execute('UPDATE sessions SET findings=?,analysis_status=? WHERE id=?', (json.dumps(findings), status, sid))
            return status

    def track_path(self, sid, track_id):
        with self._connect() as c:
            row = c.execute('SELECT path FROM tracks WHERE session_id=? AND id=?', (sid, track_id)).fetchone()
        if row:
            return _wav(_recording_root(), row['path'])
        return None


_AMOUNT = r'(\d+(?:[.,]\d{1,2})?)'
_RUB = r'(?:рублей|рубля|рубль|руб|₽)'
_PRICE = re.compile(rf'(?:ежемесячно\s+{_AMOUNT}\s*{_RUB}|{_AMOUNT}\s*{_RUB}\s+в месяц)', re.I)
_OPS = {
    'заявка на подключение оформлена': 'order', 'оформлю заявку на подключение': 'order',
    'запрос на проверку дома отправлен': 'coverage_request', 'отправлю запрос на проверку дома': 'coverage_request',
    'вопрос передан специалисту': 'handoff', 'передам вопрос специалисту': 'handoff',
}


def _audit_delivery(text, offer, receipt):
    # Match complete affirmative clauses only: budgets, questions, negations and
    # generated-but-interrupted replies cannot establish a promise.
    clauses = re.split(r'(?<=[.!?])\s+', text.strip().casefold())
    for clause in clauses:
        clause = clause.rstrip('.!')
        price_clause = re.sub(r'^сегодня внести \d+(?:[.,]\d{1,2})? рублей,\s+', '', clause)
        price = _PRICE.fullmatch(price_clause)
        if price:
            amount = Decimal((price[1] or price[2]).replace(',', '.'))
            periods = offer.get('periods') if isinstance(offer, dict) else None
            try:
                rates = {Decimal(str(p['monthly_price'])) for p in periods or []}
            except (InvalidOperation, KeyError, TypeError):
                rates = set()
            if not rates or not all(rate.is_finite() and rate >= 0 for rate in rates):
                yield 'price_unverified', 'Нет проверенной цены для сравнения с озвученной суммой.'
            elif amount not in rates:
                expected = ', '.join(str(rate) for rate in sorted(rates))
                yield 'price_mismatch', f'Озвучено {amount} руб./мес.; в предложении: {expected} руб./мес.'
            elif len(rates) > 1:
                yield 'price_unverified', 'Цена зависит от периода; озвученная сумма не определяет период.'
            else:
                yield 'price_match', f'Озвученная цена {amount} руб./мес. совпадает с предложением.'
        operation = _OPS.get(clause)
        if operation:
            actual = receipt.get('operation_type') if isinstance(receipt, dict) else None
            if actual not in _OPS.values() or not receipt.get('receipt_id'):
                yield 'operation_unverified', f'Озвучена операция {operation}; нет проверяемой квитанции.'
            elif actual != operation:
                yield 'operation_mismatch', f'Озвучена операция {operation}; в квитанции: {actual}.'
            else:
                yield 'operation_match', f'Квитанция подтверждает озвученную операцию {actual}.'
    saved = re.fullmatch(r'(?:сохранено|операция уже сохранена)\. номер ([\w-]+)\.?', text.strip(), flags=re.I)
    if saved:
        actual_id = receipt.get('receipt_id') if isinstance(receipt, dict) else None
        if not actual_id:
            yield 'operation_unverified', 'Сообщено о сохранении, но подтверждающая квитанция отсутствует.'
        elif saved[1] != actual_id:
            yield 'receipt_mismatch', f'Озвучен номер {saved[1]}; номер квитанции: {actual_id}.'
        else:
            yield 'receipt_match', 'Озвученный номер совпадает с номером квитанции.'


def _review(events, receipt):
    findings = []
    bound = None
    for event in events:
        kind, offset, text = event
        if kind == 'offer_verified':
            bound = (offset, json.loads(text))
            continue
        if kind in ('playback_ended', 'text_displayed') and text:
            offer = bound[1] if bound and bound[0] == offset else None
            for code, message in _audit_delivery(text, offer, receipt):
                findings.append(dict(start_ms=offset, end_ms=offset, code=code, message=message))
        bound = None
        if kind == 'playback_interrupted':
            message = 'Воспроизведение ответа прервано; полная доставка не подтверждена.'
        elif kind == 'final' and text:
            if not isinstance(text, str):
                raise ValueError('Invalid final transcript')
            if not text.strip():
                continue
            message = 'Зафиксирована финальная реплика абонента: ' + text[:180]
        else:
            continue
        findings.append({'start_ms': offset, 'end_ms': offset, 'message': message})
    return findings


def _loopback(host):
    if host == 'localhost':
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _local(request: Request, response: Response):
    if (os.getenv('CIELINK_MODE', 'training') != 'training' or not request.client
            or not _loopback(request.client.host) or not _loopback(request.url.hostname)):
        raise HTTPException(403, 'Local training access only')
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/api/sessions', dependencies=[Depends(_local)])


@router.get('')
def sessions():
    return ReviewStore().list_sessions()


@router.get('/{session_id}')
def session_detail(session_id: str):
    result = ReviewStore().detail(session_id)
    if result is None:
        raise HTTPException(404, 'Session not found')
    return result


@router.get('/{session_id}/tracks/{track_id}')
def recording(session_id: str, track_id: int):
    if not _raw_enabled():
        raise HTTPException(403, 'Raw recording access disabled')
    try:
        path = ReviewStore().track_path(session_id, track_id)
    except (OSError, ValueError, RuntimeError):
        path = None
    if path is None or not path.is_file():
        raise HTTPException(404, 'Recording missing')
    return FileResponse(path, media_type='audio/wav', headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


def main(argv=None):
    parser = argparse.ArgumentParser(description='Drain pending finished calls using only local event evidence.')
    parser.add_argument('--db', help='SQLite review database (default: CIELINK_REVIEW_DB)')
    parser.add_argument('--watch', action='store_true', help='Keep polling for newly finished calls')
    parser.add_argument('--interval', type=float, default=2, help='Idle polling interval in seconds (default: 2)')
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error('--interval must be finite and positive')
    store = ReviewStore(args.db)
    failed = False
    try:
        while True:
            while (status := store.analyze_next()) is not None:
                failed |= status == 'error'
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
