"""Local browser bridge. No external model or synthetic CRM side effects."""
import asyncio
from copy import copy
import json
from .consent import ConfirmedDialogue
from .crm import OperationStore
from .review import ReviewStore
from dataclasses import asdict
from anyio import CancelScope
import os
from .consultation import Consultation
from .quotes import FixtureQuoteAdapter
from .voice_routing import explanation, local_route, payment_answer, payment_request, split_social
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


class _VoiceConsultation(Consultation):
    # The consent layer can write CRM or consult, never both in one turn. Mark
    # the latter at the boundary rather than duplicating its phrase classifier.
    read_only = False
    obsolete = False

    async def _resolve(self, phrase):
        result = await super()._resolve(phrase)
        if self.obsolete:
            # Even a cancellation-ignoring address adapter may not select a
            # new request on the coordinator after a correction took ownership.
            raise asyncio.CancelledError
        return result

    async def respond(self, state, text):
        self.read_only = True
        self.proposed_action = None
        body, social = split_social(text)
        if social:
            self.route = social
            if social == 'local:thanks':
                return 'Пожалуйста!'
            if social == 'local:filler':
                return 'Да, слушаю вас.'
            if social == 'local:farewell':
                return 'До свидания!'
            return ('Да, я вас слышу. Чем могу помочь?' if text.casefold().startswith('ал')
                    else 'Здравствуйте! Чем могу помочь?')
        text = body
        self.route = local_route(text)
        if payment_request(text):
            return payment_answer(text)
        if educational := await explanation(text):
            answer, self.route = educational
            return answer
        answer = await super().respond(state, text)
        if 'equipment' in self.lookup_trace or (self.discussing_equipment and self.proposed_action == 'handoff'):
            self.route = 'local:equipment'
        return answer


async def _routed_turn(dialogue, rules, state, text):
    # Keep proposal invalidation, delivery and transaction semantics owned by
    # ConfirmedDialogue. A branch which never enters consultation is local CRM.
    rules.route = 'local:crm'
    body, social = split_social(text)
    answer = await dialogue.respond(state, text if social else body)
    return answer, rules.route


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
    await ws.send_json({'type': 'ready', 'session_id': sid, 'config': config.model_dump(),
                        'greeting': 'Здравствуйте! Я помогу проверить адрес и подобрать домашний интернет. Чем могу помочь?'})
    dsn = os.getenv('CIELINK_CRM_URL')
    if not dsn:
        Path('local-data').mkdir(exist_ok=True)
        dsn = 'sqlite:///local-data/crm.sqlite3'
    store = await asyncio.to_thread(OperationStore, dsn)
    rules = _VoiceConsultation(FixtureQuoteAdapter(), config.mode)
    consultation = ConfirmedDialogue(rules, store)
    active_reply = None
    started = False
    spoken_text = None
    spoken_offer = None
    async def record(kind, offset, text=None):
        await asyncio.to_thread(review.append_event, sid, kind, offset, text)
    seq = 0
    incoming = asyncio.create_task(ws.receive_text())
    pending = None
    partial_work = False
    retired = set()

    def forget(task):
        retired.discard(task)
        if not task.cancelled():
            task.exception()  # detached, superseded work must never publish

    async def publish():
        nonlocal pending, active_reply, started, spoken_offer, spoken_text, seq
        text, route = pending.result()
        pending = None
        active_reply, started = uuid4().hex, False
        spoken_text = text
        spoken_offer = state.active_offer.model_dump(mode='json') if state.active_offer else None
        # No disk I/O between the actor's freshness check and the first send.
        seq += 1
        await ws.send_json({'type': 'event', 'event': {'type': 'reply_generated', 'sequence': seq,
                            'session_id': sid, 'reply_id': active_reply, 'offset_ms': reply_offset,
                            'text': text, 'route': route, 'reads': list(rules.reads.trace), 'context': {
                                'house_id': state.address.house_id if state.address else None,
                                'monthly_budget': str(state.monthly_budget) if state.monthly_budget is not None else None,
                                'needs': list(state.needs)}, 'lookups': rules.lookup_trace,
                            'operation': ({'type': consultation.receipt.operation_type,
                                           'number': consultation.receipt.spoken_reference}
                                          if consultation.receipt else None)}})
        await ws.send_json({'type': 'reply', 'text': text, 'reply_id': active_reply,
                            'input_offset_ms': reply_offset, 'route': route})
        with CancelScope(shield=True):
            await record('reply_generated', reply_offset, text)

    try:
        while True:
            if pending is not None:
                await asyncio.wait((incoming, pending), return_when=asyncio.FIRST_COMPLETED)
                # Input wins ties against read-only work. A CRM transaction, on
                # the other hand, owns its whole turn through receipt publication.
                if not incoming.done() or not rules.read_only or (partial_work and pending.done()):
                    await asyncio.shield(pending)
                    if partial_work:
                        pending = None
                    else:
                        await publish()
                    continue
            raw = await incoming
            incoming = asyncio.create_task(ws.receive_text())
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
            active_reply, started = None, False  # reject callbacks immediately on barge-in
            if pending is not None:
                # Keep parsed address/base/budgets, not unpublished products of
                # the obsolete turn. Detach all mutable turn state before cancel.
                result.state = result.state.model_copy(deep=True)
                old_rules = rules
                rules = copy(old_rules)
                old_rules.obsolete = True
                rules.prepared = None
                rules.reads.invalidate()
                if partial_work:
                    consultation.consultation = rules  # partials never alter consent
                else:
                    result.state.active_offer = None
                    rules.coverage_status = rules.proposed_action = None
                    consultation = ConfirmedDialogue(rules, store)
                    spoken_offer = None
                retired.add(pending)
                pending.add_done_callback(forget)
                pending.cancel()
                pending = None
            await record(inp.type, inp.offset_ms, inp.text)
            state = result.state
            seq += 1
            await ws.send_json({'type': 'event', 'event': {**result.event.model_dump(), 'sequence': seq}})
            if isinstance(inp, PartialInput) and not consultation.finished and not consultation.receipt:
                partial_work = rules.read_only = True
                pending = asyncio.create_task(rules.partial(state, inp.text))
            if isinstance(inp, FinalInput):
                partial_work = False
                reply_offset = inp.offset_ms
                rules.read_only = False
                rules.lookup_trace = {}
                pending = asyncio.create_task(_routed_turn(consultation, rules, state, inp.text))
    except WebSocketDisconnect:
        return
    finally:
        # The ASGI host may cancel the socket scope immediately on disconnect.
        # Persist the terminal state before draining cancellable speculative reads.
        with CancelScope(shield=True):
            incoming.cancel()
            await asyncio.gather(incoming, return_exceptions=True)
            if pending is not None:
                if rules.read_only:
                    rules.obsolete = True
                    rules.reads.invalidate()
                    pending.cancel()
                    retired.add(pending)
                else:
                    await asyncio.gather(pending, return_exceptions=True)
            receipt = asdict(consultation.receipt) if consultation.receipt else None
            outcome = receipt['operation_type'] if receipt else ('no_action' if consultation.finished else 'disconnected')
            await asyncio.to_thread(review.finish_session, sid, outcome, receipt=receipt,
                                    offer=spoken_offer, coverage_status=rules.coverage_status,
                                    address=rules.describe(state.address) if state.address else None)
            await rules.reads.close()
            if retired:
                await asyncio.gather(*retired, return_exceptions=True)
