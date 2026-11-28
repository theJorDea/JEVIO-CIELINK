"""Exercise the actual socket protocol; gates (not adapter latency) order the race."""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
from threading import Event

import uvicorn
from websockets.asyncio.client import connect

from cielink.addresses import FixtureCoverageAdapter
from cielink.api import create_app
from cielink.crm import OperationStore
from cielink.review import ReviewStore


@asynccontextmanager
async def voice_client(tmp_path, monkeypatch):
    monkeypatch.setenv('CIELINK_CRM_URL', f'sqlite:///{tmp_path}/crm.db')
    monkeypatch.setenv('CIELINK_REVIEW_DB', str(tmp_path / 'review.db'))
    monkeypatch.setenv('CIELINK_TTS', 'text')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(), lifespan='off', log_level='error'))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with connect(f'ws://127.0.0.1:{port}/ws/voice') as ws:
                yield ws
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)


async def receive(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 4))


async def send(ws, kind, offset, **fields):
    await ws.send(json.dumps(dict(type=kind, offset_ms=offset, **fields)))


async def event(ws, kind, seq):
    msg = await receive(ws)
    assert msg['type'] == 'event', msg
    assert (msg['event']['type'], msg['event']['sequence']) == (kind, seq), msg
    return msg['event']


async def reply(ws, offset, seq):
    generated = await event(ws, 'reply_generated', seq)
    msg = await receive(ws)
    assert msg == dict(type='reply', text=generated['text'], reply_id=generated['reply_id'],
                       input_offset_ms=offset, route=generated['route'])
    return msg


def test_corrected_house_before_street_and_price_reach_review(tmp_path, monkeypatch):
    async def exercise():
        async with voice_client(tmp_path, monkeypatch) as ws:
            sid = (await receive(ws))['session_id']
            for i, (text, expected) in enumerate([
                ('Дом 1, корпус 3. Ой, нет, корпус 5.', 'дом 1 корпус 5'),
                ('Улицы Пушкина.', 'Улица Пушкина, дом 1, корпус 5'),
                ('Ну ладно, а сколько будет стоить всё?', '1700 рублей в месяц'),
            ]):
                offset = 2 * i + 1
                await send(ws, 'final', offset, text=text)
                await event(ws, 'final', 3 * i + 1)
                answer = await reply(ws, offset, 3 * i + 2)
                assert expected in answer['text']
                await send(ws, 'text_displayed', offset + 1, reply_id=answer['reply_id'])
                await event(ws, 'text_displayed', 3 * i + 3)
        detail = ReviewStore().detail(sid)
        assert detail['address'] == 'Улица Пушкина, дом 1, корпус 5'
        assert detail['offer']['house_id'] == 'demo-pushkina-1-k5'
        assert detail['coverage_status'] == 'available' and detail['receipt'] is None

    asyncio.run(exercise())


def test_correction_beats_uncancellable_read(tmp_path, monkeypatch):
    async def exercise():
        entered, cancelled, release, returned = (asyncio.Event() for _ in range(4))
        original = FixtureCoverageAdapter.check_coverage

        async def gated(self, house):
            if house == 'training-house-a':
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                returned.set()
            return await original(self, house)

        monkeypatch.setattr(FixtureCoverageAdapter, 'check_coverage', gated)
        async with voice_client(tmp_path, monkeypatch) as ws:
            sid = (await receive(ws))['session_id']
            try:
                await send(ws, 'final', 1, text='Учебная улица 18 корпус первый, сегодня до 2000')
                await event(ws, 'final', 1)
                await asyncio.wait_for(entered.wait(), 3)
                await send(ws, 'final', 2, text='стоп, корпус второй')
                await event(ws, 'final', 2)
                corrected = await reply(ws, 2, 3)
                assert 'корпус 2' in corrected['text'] and '1700' not in corrected['text']
                assert cancelled.is_set() and not returned.is_set()
            finally:
                release.set()
            await asyncio.wait_for(returned.wait(), 3)
            await send(ws, 'partial', 3, text='проверьте')
            await event(ws, 'partial', 4)
            # Completion of the obsolete playback must not authorize a write.
            await send(ws, 'text_displayed', 4, reply_id=corrected['reply_id'])
            await send(ws, 'final', 5, text='да проверьте адрес')
            await event(ws, 'final', 5)
            repeated = await reply(ws, 5, 6)
            assert repeated['text'] == corrected['text']
            await send(ws, 'text_displayed', 6, reply_id=repeated['reply_id'])
            await event(ws, 'text_displayed', 7)
            await send(ws, 'final', 7, text='да проверьте адрес')
            await event(ws, 'final', 8)
            assert 'Номер' in (await reply(ws, 7, 9))['text']
        detail = ReviewStore().detail(sid)
        assert detail['coverage_status'] == 'unknown' and detail['offer'] is None
        receipt = detail['receipt']
        assert receipt['operation_type'] == 'coverage_request' and receipt['sequence_no'] == 1
        terms = receipt['confirmed_terms']
        assert terms['address']['corpus'] == '2' and terms['budget_today'] == '2000'
        assert not any(e['offset_ms'] == 1 and e['type'] == 'reply_generated' for e in detail['events'])

    asyncio.run(exercise())


def test_confirmation_write_is_not_superseded(tmp_path, monkeypatch):
    async def exercise():
        entered, release = Event(), Event()
        writes = []
        original = OperationStore.record_operation

        def blocked(self, **kwargs):
            writes.append(kwargs['operation_key'])
            entered.set()
            assert release.wait(5)
            return original(self, **kwargs)

        monkeypatch.setattr(OperationStore, 'record_operation', blocked)
        async with voice_client(tmp_path, monkeypatch) as ws:
            sid = (await receive(ws))['session_id']
            await send(ws, 'final', 1, text='хочу оператора')
            await event(ws, 'final', 1)
            proposal = await reply(ws, 1, 2)
            await send(ws, 'text_displayed', 2, reply_id=proposal['reply_id'])
            await event(ws, 'text_displayed', 3)
            try:
                await send(ws, 'final', 3, text='да передайте специалисту')
                await event(ws, 'final', 4)
                assert await asyncio.to_thread(entered.wait, 3)
                await send(ws, 'final', 4, text='да передайте специалисту')
                # A protocol ping proves the second frame reached the server.
                await (await ws.ping())
            finally:
                release.set()
            first = await reply(ws, 3, 5)
            await event(ws, 'final', 6)
            second = await reply(ws, 4, 7)
            assert first['text'].split('Номер ')[1] == second['text'].split('Номер ')[1]
        assert len(writes) == 1 and ReviewStore().detail(sid)['outcome'] == 'handoff'

    asyncio.run(exercise())
