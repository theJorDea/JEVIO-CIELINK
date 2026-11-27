"""Local browser bridge. No external model or synthetic CRM side effects."""
import json
import os
import re
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

    @classmethod
    def load(cls):
        return cls(stt=os.getenv('CIELINK_STT', 'browser'), tts=os.getenv('CIELINK_TTS', 'browser'),
                   llm=os.getenv('CIELINK_LLM', 'rules'))


class Playback(Contract):
    type: Literal['playback_started', 'playback_ended', 'playback_interrupted']
    reply_id: str
    offset_ms: NonNegativeInt


INPUT = TypeAdapter(Annotated[Playback | PartialInput | FinalInput, Field(discriminator='type')])


@router.get('/voice', include_in_schema=False)
def voice_page():
    return FileResponse(STATIC / 'voice.html')


@router.get('/static/voice.js', include_in_schema=False)
def voice_script():
    return FileResponse(STATIC / 'voice.js', media_type='text/javascript')


def training_reply(text: str, fallback: str) -> str:
    # Stand-only fixtures, no inference of real provider coverage.
    match = re.search(r'\bкорпус\s+(первый|второй|один|два|[12])\b', text.lower())
    if not match:
        return fallback
    if match[1] in ('два', 'второй', '2'):
        return 'Учебный корпус два. Данных о покрытии нет. Уточнить адрес для проверки?'
    return 'Учебный корпус один. В учебной базе подключение доступно.'


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
    await ws.send_json({'type': 'ready', 'session_id': sid, 'config': config.model_dump()})
    active_reply = None
    started = False
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
                    started = inp.type == 'playback_started'
                    if not started:
                        active_reply = None
                    seq += 1
                    await ws.send_json({'type': 'event', 'event': {**inp.model_dump(), 'session_id': sid, 'sequence': seq}})
                    continue
                result = handle_input(state, inp)
            except (ValidationError, ValueError):
                await ws.send_json({'type': 'error', 'message': 'Некорректное событие или время реплики.'})
                continue
            state = result.state
            seq += 1
            await ws.send_json({'type': 'event', 'event': {**result.event.model_dump(), 'sequence': seq}})
            if isinstance(inp, FinalInput):
                active_reply, started = uuid4().hex, False
                text = training_reply(inp.text, result.next_action.text or 'Уточните адрес подключения.')
                seq += 1
                await ws.send_json({'type': 'event', 'event': {'type': 'reply_generated', 'sequence': seq,
                                    'session_id': sid, 'reply_id': active_reply, 'offset_ms': inp.offset_ms, 'text': text}})
                await ws.send_json({'type': 'reply', 'text': text, 'reply_id': active_reply})
    except WebSocketDisconnect:
        return
