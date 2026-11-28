"""LiveKit Cloud voice entry point for the CIELINK training dialogue."""

import asyncio
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, inference, llm

from .consent import ConfirmedDialogue
from .consultation import Consultation
from .crm import OperationStore
from .domain import FinalInput, SessionContext, SessionState, handle_input
from .quotes import FixtureQuoteAdapter
from .review import ReviewStore


class CielinkAgent(Agent):
    def __init__(self, review: ReviewStore, store: OperationStore, session_id: str):
        super().__init__(instructions="Голосовой вход CIELINK.")
        self.review = review
        self.state = SessionState(context=SessionContext(session_id=session_id, channel='web'))
        self.rules = Consultation(FixtureQuoteAdapter())
        self.dialogue = ConfirmedDialogue(self.rules, store)
        self.started_at = monotonic()
        self.delivered_at = None
        self.pending_reply: asyncio.Task[str] | None = None

    def offset_ms(self):
        return int((monotonic() - self.started_at) * 1000)

    async def record(self, kind, offset, text=None):
        await asyncio.to_thread(self.review.append_event, self.state.context.session_id, kind, offset, text)

    async def on_enter(self):
        self.session.say('Здравствуйте. Назовите адрес, по которому хотите подключить интернет.')

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        text = (new_message.text_content or '').strip()
        if not text:
            return
        offset = self.offset_ms()
        self.state = handle_input(self.state, FinalInput(text=text, offset_ms=offset)).state
        await self.record('final', offset, text)
        started = new_message.metrics.get('started_speaking_at')
        if (self.dialogue.proposal and self.delivered_at is not None and started is not None
                and started < self.delivered_at):
            self.dialogue.proposal.delivered = False
        # A caller can disconnect while a confirmed CRM write is committing.
        self.pending_reply = asyncio.create_task(self.dialogue.respond(self.state, text))
        reply = await asyncio.shield(self.pending_reply)
        self.pending_reply = None
        await self.record('reply_generated', self.offset_ms(), reply)
        speech = self.session.say(reply)
        await speech.wait_for_playout()
        end = self.offset_ms()
        delivered_at = next((
            item.metrics['stopped_speaking_at'] for item in speech.chat_items
            if isinstance(item, llm.ChatMessage) and 'stopped_speaking_at' in item.metrics
        ), None)
        if speech.interrupted or speech.exception() is not None or delivered_at is None:
            await self.record('playback_interrupted', end)
            return
        self.delivered_at = delivered_at
        self.dialogue.delivered()
        offer = self.state.active_offer
        verified = offer.model_dump(mode='json') if offer else None
        await self.record('offer_verified', end, json.dumps(verified, ensure_ascii=False))
        await self.record('playback_ended', end, reply)

    async def finish(self):
        if self.pending_reply is not None:
            await asyncio.gather(self.pending_reply, return_exceptions=True)
        await self.rules.reads.close()
        receipt = self.dialogue.receipt
        offer = self.state.active_offer
        outcome = receipt.operation_type if receipt else ('no_action' if self.dialogue.finished else 'disconnected')
        await asyncio.to_thread(
            self.review.finish_session, self.state.context.session_id, outcome,
            receipt=asdict(receipt) if receipt else None,
            offer=offer.model_dump(mode='json') if offer else None,
            coverage_status=self.rules.coverage_status,
            address=self.rules.describe(self.state.address) if self.state.address else None,
        )


async def entrypoint(ctx: JobContext):
    stt_model = os.getenv('CIELINK_LIVEKIT_STT', 'deepgram/nova-3')
    tts_model = os.getenv('CIELINK_LIVEKIT_TTS', 'cartesia/sonic-3')
    voice = os.environ['CIELINK_LIVEKIT_VOICE']
    session = AgentSession(
        stt=inference.STT(model=stt_model, language='ru'),
        tts=inference.TTS(model=tts_model, voice=voice, language='ru'),
    )
    Path('local-data').mkdir(exist_ok=True)
    dsn = os.getenv('CIELINK_CRM_URL', 'sqlite:///local-data/crm.sqlite3')
    store = await asyncio.to_thread(OperationStore, dsn)
    review = await asyncio.to_thread(ReviewStore)
    session_id = await asyncio.to_thread(review.start_session)
    agent = CielinkAgent(review, store, session_id)

    async def finish():
        try:
            await session.aclose()
        finally:
            await agent.finish()

    ctx.add_shutdown_callback(finish)
    logging.getLogger(__name__).info('CIELINK session_id=%s', session_id)
    await agent.record('session_started', 0, json.dumps({
        'transport': 'livekit', 'stt': stt_model, 'tts': tts_model, 'voice': voice, 'dialogue': 'rules',
    }))
    await session.start(agent=agent, room=ctx.room)
    await ctx.connect()


def main():
    load_dotenv('.env.local')
    if os.getenv('CIELINK_MODE', 'training') != 'training':
        raise SystemExit('LiveKit adapter supports CIELINK_MODE=training')
    server = AgentServer()
    server.rtc_session(agent_name=os.getenv('CIELINK_LIVEKIT_AGENT_NAME', 'cielink'))(entrypoint)
    cli.run_app(server)


if __name__ == '__main__':
    main()
