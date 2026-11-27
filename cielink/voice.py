"""Local browser bridge. No external model or synthetic CRM side effects."""
import asyncio
import json
from .consent import ConfirmedDialogue
from .crm import OperationStore
from .review import ReviewStore
from dataclasses import asdict
from anyio import CancelScope
import os
from .consultation import Consultation
from .quotes import FixtureQuoteAdapter
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import Field, NonNegativeInt, TypeAdapter, ValidationError

from .domain import Contract, SessionContext, SessionState, PartialInput, FinalInput, handle_input

STATIC = Path(__file__).with_name('static')
router = APIRouter()


class VoiceConfig(Contract):
    stt: Literal['browser', 'text']
    tts: Literal['browser', 'text']
    llm: Literal['rules']
    mode: Literal['sequential', 'early', 'speculative']

    @classmethod
    def load(cls):
        return cls(stt=os.getenv('CIELINK_STT', 'browser'), tts=os.getenv('CIELINK_TTS', 'browser'),
                   llm=os.getenv('CIELINK_LLM', 'rules'), mode=os.getenv('CIELINK_READ_MODE', 'speculative'))


class Playback(Contract):
    type: Literal['playback_started', 'playback_ended', 'playback_interrupted', 'text_displayed']
    reply_id: str
    offset_ms: NonNegativeInt


INPUT = TypeAdapter(Annotated[Playback | PartialInput | FinalInput, Field(discriminator='type')])


@router.get('/voice', include_in_schema=False)
def voice_page():
    return FileResponse(STATIC / 'voice.html')


@router.get('/static/voice.js', include_in_schema=False)
def voice_script():
    return FileResponse(STATIC / 'voice.js', media_type='text/javascript')


@router.websocket('/ws/voice')
async def voice_socket(ws: WebSocket):
    # Browsers must not connect to the local training service from another site.
    origin = ws.headers.get('origin')
    if origin and origin not in {f"http://{ws.headers['host']}", f"https://{ws.headers['host']}"}:
        await ws.close(code=1008)
        return
    config = VoiceConfig.load()
    await ws.accept()
    sid = uuid4().hex
    state = SessionState(context=SessionContext(session_id=sid, channel='web'))
    review = await asyncio.to_thread(ReviewStore)
    await asyncio.to_thread(review.start_session, sid)
    await ws.send_json({'type': 'ready', 'session_id': sid, 'config': config.model_dump()})
    dsn = os.getenv('CIELINK_CRM_URL')
    if not dsn:
        Path('local-data').mkdir(exist_ok=True)
        dsn = 'sqlite:///local-data/crm.sqlite3'
    store = await asyncio.to_thread(OperationStore, dsn)
    rules = Consultation(FixtureQuoteAdapter(), config.mode)
    consultation = ConfirmedDialogue(rules, store)
    active_reply = None
    started = False
    spoken_text = None
    spoken_offer = None
    async def record(kind, offset, text=None):
        await asyncio.to_thread(review.append_event, sid, kind, offset, text)
    seq = 0
    try:
        while True:
            raw = await ws.receive_text()
            try:
                if len(raw) > 8192:
                    raise ValueError('message too long')
                inp = INPUT.validate_python(json.loads(raw))
                if isinstance(inp, Playback):
                    if inp.reply_id != active_reply or (inp.type == 'playback_started' and started):
                        continue  # late callbacks from a cancelled utterance
                    if inp.type == 'playback_ended' and not started:
                        continue
                    if inp.type == 'text_displayed' and config.tts != 'text':
                        continue
                    if inp.type in ('playback_ended', 'text_displayed'):
                        consultation.delivered()
                        await record('offer_verified', inp.offset_ms, json.dumps(spoken_offer, ensure_ascii=False))
                    started = inp.type == 'playback_started'
                    if not started:
                        active_reply = None
                    await record(inp.type, inp.offset_ms, spoken_text if inp.type in ('playback_ended', 'text_displayed') else None)
                    seq += 1
                    await ws.send_json({'type': 'event', 'event': {**inp.model_dump(), 'session_id': sid, 'sequence': seq}})
                    continue
                result = handle_input(state, inp)
            except (ValidationError, ValueError):
                await ws.send_json({'type': 'error', 'message': 'Некорректное событие или время реплики.'})
                continue
            await record(inp.type, inp.offset_ms, inp.text)
            state = result.state
            seq += 1
            await ws.send_json({'type': 'event', 'event': {**result.event.model_dump(), 'sequence': seq}})
            if isinstance(inp, PartialInput) and not consultation.finished and not consultation.receipt:
                await rules.partial(state, inp.text)
            if isinstance(inp, FinalInput):
                active_reply, started = uuid4().hex, False
                text = await consultation.respond(state, inp.text)
                spoken_text = text
                spoken_offer = state.active_offer.model_dump(mode='json') if state.active_offer else None
                await record('reply_generated', inp.offset_ms, text)
                seq += 1
                await ws.send_json({'type': 'event', 'event': {'type': 'reply_generated', 'sequence': seq,
                                    'session_id': sid, 'reply_id': active_reply, 'offset_ms': inp.offset_ms, 'text': text,
                                    'reads': list(rules.reads.trace)}})
                await ws.send_json({'type': 'reply', 'text': text, 'reply_id': active_reply,
                                    'input_offset_ms': inp.offset_ms})
    except WebSocketDisconnect:
        return
    finally:
        # The ASGI host may cancel the socket scope immediately on disconnect.
        # Persist the terminal state before draining cancellable speculative reads.
        with CancelScope(shield=True):
            receipt = asdict(consultation.receipt) if consultation.receipt else None
            outcome = receipt['operation_type'] if receipt else ('no_action' if consultation.finished else 'disconnected')
            await asyncio.to_thread(review.finish_session, sid, outcome, receipt=receipt,
                                    offer=spoken_offer, coverage_status=rules.coverage_status)
        await rules.reads.close()
