"""Replay the training corpus against the actual loopback web service."""
import argparse
from collections import Counter
from contextlib import closing, contextmanager
import json
from math import ceil
import os
from pathlib import Path
import socket
import sqlite3
from statistics import median
import tempfile
from threading import Thread
from time import monotonic, perf_counter_ns, sleep
from unittest.mock import patch

import httpx
import uvicorn
from websockets.sync.client import connect

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures/replay-scenarios.json'
MODES = ('sequential', 'early', 'speculative')


@contextmanager
def server(mode, directory):
    env = {'CIELINK_MODE': 'training', 'CIELINK_STT': 'text', 'CIELINK_TTS': 'text',
           'CIELINK_LLM': 'rules', 'CIELINK_READ_MODE': mode,
           'CIELINK_REVIEW_DB': str(directory / 'review.db'),
           'CIELINK_CRM_URL': f'sqlite:///{directory / "crm.db"}'}
    with patch.dict(os.environ, env), socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        app = uvicorn.Server(uvicorn.Config('cielink.api:app', loop='asyncio', log_level='error', ws='websockets-sansio'))
        thread = Thread(target=app.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not app.started:
                if not thread.is_alive() or monotonic() > deadline:
                    raise RuntimeError('Local replay server did not start')
                sleep(.01)
            yield f'127.0.0.1:{port}'
        finally:
            app.should_exit = True
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError('Local replay server did not stop')


def receive(ws, kind):
    frame = json.loads(ws.recv(timeout=5))
    if frame['type'] != kind:
        raise ValueError(f'Expected {kind}, received {frame}')
    return frame


def play(host, client, crm, mode, case):
    failures, times, trace = [], [], []

    def check(ok, message):
        if not ok:
            failures.append(message)

    with connect(f'ws://{host}/ws/voice', proxy=None, open_timeout=5) as ws:
        ready = receive(ws, 'ready')
        check(ready['config']['mode'] == mode, 'wrong server mode')
        sid = ready['session_id']
        path = '/api/sessions/' + sid
        offset = 0

        def send(kind, **fields):
            nonlocal offset
            offset += 10  # logical ordering only, never used as a latency
            ws.send(json.dumps(dict(type=kind, offset_ms=offset, **fields)))
            event = receive(ws, 'event')['event']
            check(event['type'] == kind and event['session_id'] == sid, 'wrong event echo')

        receipt_texts = []
        for turn in case['turns']:
            for partial in turn.get('partials', []):
                send('partial', text=partial)
            start = perf_counter_ns()
            send('final', text=turn['text'])
            generated = receive(ws, 'event')['event']
            reply = receive(ws, 'reply')
            times.append((perf_counter_ns() - start) / 1e6)
            check(generated['type'] == 'reply_generated' and generated['reply_id'] == reply['reply_id'], 'wrong generated reply')
            trace = generated['reads']  # cumulative server trace, not a per-turn delta
            if 'contains' in turn:
                check(turn['contains'].casefold() in reply['text'].casefold(), f'after {turn["text"]}: expected {turn["contains"]!r} in reply')
            actual = generated.get('context', {})
            for field, value in turn.get('context', {}).items():
                check(field in actual and actual[field] == value,
                      f'after {turn["text"]}: expected {field}={value!r}, observed {actual.get(field)!r}')
            if turn.get('display', True):
                send('text_displayed', reply_id=reply['reply_id'])
            if turn.get('receipt'):
                receipt_texts.append(reply['text'])
            count = crm.execute('SELECT count(*) FROM crm_operations WHERE session_id=?', (sid,)).fetchone()[0]
            check(count == int(bool(receipt_texts)), f'after {turn["text"]}: {count} CRM rows')

    deadline = monotonic() + 5
    while True:
        card = client.get(path).raise_for_status().json()
        if card['status'] == 'finished' or monotonic() > deadline:
            break
        sleep(.01)
    check(card['status'] == 'finished', 'review card did not finish')
    check(card['outcome'] == case['outcome'], f'review outcome: {card["outcome"]}')
    check(card['coverage_status'] == case['coverage'], f'coverage: {card["coverage_status"]}')
    check([e['text'] for e in card['events'] if e['type'] == 'final'] == [t['text'] for t in case['turns']], 'review transcript differs')
    rows = crm.execute('SELECT receipt_id, spoken_reference, operation_type, confirmed_terms FROM crm_operations WHERE session_id=?', (sid,)).fetchall()
    if case.get('house'):
        check(len(rows) == 1, 'expected exactly one durable operation')
        if len(rows) == 1:
            rid, reference, kind, snapshot = rows[0]
            terms = json.loads(snapshot)
            receipt = card['receipt'] or {}
            check(kind == case['outcome'] and receipt.get('operation_type') == kind, 'operation type differs')
            check(receipt.get('receipt_id') == rid and receipt.get('spoken_reference') == reference,
                  'review/CRM receipt identifiers differ')
            check(receipt_texts and all(t.endswith(f'Номер {reference}.') and rid not in t for t in receipt_texts),
                  'spoken receipt number differs')
            check(receipt.get('confirmed_terms') == terms, 'review/CRM terms differ')
            check((terms['address'] or {}).get('house_id') == case['house'], 'wrong confirmed house')
            check((terms['offer'] or {}).get('offer_id') == case.get('offer'), 'wrong confirmed offer')
            check(terms['monthly_budget'] == case.get('monthly_budget'), 'wrong confirmed budget')
    else:
        check(not rows and card['receipt'] is None, 'unexpected operation or receipt')
    starts = {}
    read_ms = []
    for event in trace:
        key = event['revision'], event['operation']
        if event['event'] == 'start':
            starts[key] = event['offset_ms']
        elif event['event'] == 'ready' and key in starts:
            read_ms.append(event['offset_ms'] - starts[key])
    used = {(e['revision'], e['operation']) for e in trace if e['event'] == 'used'}
    counts = Counter(e['event'] for e in trace)
    if case['id'] == 'order':
        check(bool(counts['stable']) == (mode == 'speculative'), 'unexpected stable partial trace')
    return {'session_id': sid, 'scenario': case['id'], 'failures': failures, 'local_final_to_reply_ms': times,
            'local_server_read_ms': read_ms, 'unused_reads': len(starts.keys() - used),
            'read_events': dict(counts), 'crm_rows': len(rows)}


def run():
    cases = json.loads(CORPUS.read_text(encoding='utf-8'))
    results, aggregate = {}, {}
    for mode in MODES:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with server(mode, directory) as host, httpx.Client(base_url=f'http://{host}', trust_env=False) as client, closing(sqlite3.connect(directory / 'crm.db')) as crm:
                calls = []
                for case in cases:
                    try:
                        calls.append(play(host, client, crm, mode, case))
                    except Exception as exc:
                        calls.append({'scenario': case['id'], 'failures': [f'{type(exc).__name__}: {exc}']})
        results[mode] = calls
        samples = sorted(t for c in calls for t in c.get('local_final_to_reply_ms', []))
        aggregate[mode] = {'calls': len(calls), 'passed': sum(not c['failures'] for c in calls),
                           'measured_turns': len(samples),
                           'read_starts': sum(c.get('read_events', {}).get('start', 0) for c in calls),
                           'unused_reads': sum(c.get('unused_reads', 0) for c in calls),
                           'local_final_to_reply_p50_ms': round(median(samples), 3) if samples else None,
                           'local_final_to_reply_p95_ms': round(samples[ceil(.95 * len(samples)) - 1], 3) if samples else None,
                           'acoustic_latency_ms': None, 'mobile_latency_ms': None}
    return {'aggregate': aggregate, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='write UTF-8 JSON to this path instead of stdout')
    parser.add_argument('--demo-dir', type=Path, help='create a new directory with one followup_correction call and its review/CRM databases')
    args = parser.parse_args()
    if args.demo_dir:
        directory = args.demo_dir.resolve()
        if directory.exists():
            parser.error('--demo-dir requires a new directory, to avoid mixing demo and existing calls')
        directory.mkdir(parents=True)
        case = next(c for c in json.loads(CORPUS.read_text(encoding='utf-8')) if c['id'] == 'followup_correction')
        with server('speculative', directory) as host, httpx.Client(base_url=f'http://{host}', trust_env=False) as client, closing(sqlite3.connect(directory / 'crm.db')) as crm:
            call = play(host, client, crm, 'speculative', case)
        from .review import ReviewStore
        review = ReviewStore(directory / 'review.db')
        review.analyze_next()
        report = {'channel': 'web-text-loopback', 'review_db': str(directory / 'review.db'),
                  'call': call, 'card': review.detail(call['session_id'])}
    else:
        report = run()
    output = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        args.output.write_text(output + '\n', encoding='utf-8')
    else:
        print(output)
    if args.demo_dir:
        return int(bool(report['call']['failures']))
    return int(any(m['passed'] != m['calls'] for m in report['aggregate'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
