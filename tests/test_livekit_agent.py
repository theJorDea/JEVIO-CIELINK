"""Exercise the shared CRM and real SpeechHandle without a cloud account."""

import asyncio
import time

import pytest

pytest.importorskip('livekit.agents')

from livekit import rtc
from livekit.agents import AgentSession, TurnHandlingOptions, llm
from livekit.agents.voice import io

from cielink.crm import OperationStore
from cielink.livekit_agent import CielinkAgent
from cielink.review import ReviewStore


class TestAudioSink(io.AudioOutput):
    __test__ = False

    def __init__(self):
        super().__init__(label='test', capabilities=io.AudioOutputCapabilities(pause=False))

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        asyncio.get_running_loop().call_soon(lambda: self.on_playback_started(created_at=time.time()))
        await asyncio.sleep(0)

    def flush(self):
        super().flush()
        if self._pending_playback_count:
            self.on_playback_finished(playback_position=0.02, interrupted=False)

    def clear_buffer(self):
        self.flush()


class LocalSession(AgentSession):
    interrupt_next_reply = False

    def say(self, text):
        async def frames():
            yield rtc.AudioFrame.create(24000, 1, 480)

        speech = super().say(text, audio=frames())
        if self.interrupt_next_reply:
            speech.interrupt(force=True)
            self.interrupt_next_reply = False
        return speech


def test_livekit_coverage_request_requires_a_fresh_delivered_prompt(tmp_path, monkeypatch):
    async def no_greeting(_):
        pass

    monkeypatch.setattr(CielinkAgent, 'on_enter', no_greeting)

    async def run():
        store = OperationStore(f'sqlite:///{tmp_path / "crm.db"}')
        review = ReviewStore(tmp_path / 'review.db')
        session_id = review.start_session()
        agent = CielinkAgent(review, store, session_id)
        session = LocalSession(vad=None, turn_handling=TurnHandlingOptions(turn_detection=None))
        session.output.audio = TestAudioSink()
        await session.start(agent=agent)

        async def turn(text, started=None):
            message = llm.ChatMessage(role='user', content=[text])
            if started is not None:
                message.metrics['started_speaking_at'] = started
            await asyncio.wait_for(agent.on_user_turn_completed(llm.ChatContext(), message), timeout=5)

        try:
            await turn('Учебная 18 корпус 1, до 2000 в месяц')
            session.interrupt_next_reply = True
            await turn('Корпус второй')
            assert not agent.dialogue.proposal.delivered
            await turn('Да, проверьте адрес')
            assert agent.dialogue.receipt is None and agent.dialogue.proposal.delivered
            await turn('Да, проверьте адрес', started=agent.delivered_at - 0.1)
            assert agent.dialogue.receipt is None
            await turn('Да, проверьте адрес', started=agent.delivered_at + 0.1)
            assert agent.dialogue.receipt is not None
        finally:
            await session.aclose()
            await agent.finish()

        assert review.analyze_next() == 'complete'
        card = review.detail(session_id)
        assert card['outcome'] == 'coverage_request'
        terms = card['receipt']['confirmed_terms']
        assert terms['address']['house_id'] == 'uchebnaya-18-k2'
        assert terms['monthly_budget'] == '2000'
        assert store.lookup_operation(card['receipt']['operation_key']).confirmed_terms == terms
        assert sum(event['type'] == 'playback_interrupted' for event in card['events']) == 1

    asyncio.run(run())
