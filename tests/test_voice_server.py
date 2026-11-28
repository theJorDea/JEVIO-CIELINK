import asyncio
import json

from cielink_asr.addresses import AddressCatalog
from cielink_asr.llm import LLMError
from cielink_asr.voice_server import VoiceSession, _handle


def sample_catalog():
    return {
        "streets_max_length": 12,
        "streets": "Лесная".ljust(12) + "Набережная".ljust(12),
        "homes": [
            [
                {"id": 4, "t": "14", "s": 3},
                {"id": 6, "t": "10", "s": 3},
                {"id": 7, "t": "10 корпус 2", "s": 2},
                {"id": 8, "t": "2", "s": 3},
            ],
            [{"id": 5, "t": "1", "s": 0}],
        ],
    }


def test_voice_session_asks_slots_then_reads_catalog_status():
    session = VoiceSession(AddressCatalog.from_json(sample_catalog()))

    assert "квартиру" in session.reply("Хочу подключить интернет")
    assert "улицу" in session.reply("Для квартиры")
    answer = session.reply("Лесная, дом 14")

    assert "подключённый" in answer
    assert "справочный статус" in answer


def test_websocket_handler_sends_ready_and_reply_events():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.incoming = iter(
                [
                    json.dumps({"type": "final", "text": "Хочу интернет"}),
                    json.dumps({"type": "final", "text": "Для квартиры"}),
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.incoming)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, message):
            self.sent.append(json.loads(message))

    websocket = FakeWebSocket()
    asyncio.run(_handle(websocket, AddressCatalog.from_json(sample_catalog()), None))

    assert websocket.sent[0]["type"] == "ready"
    assert [event["type"] for event in websocket.sent[1:]] == ["reply", "reply"]
    assert "квартиру" in websocket.sent[1]["text"]
    assert "улицу" in websocket.sent[2]["text"]


def test_voice_session_keeps_support_outside_new_connection_flow():
    session = VoiceSession(AddressCatalog.from_json(sample_catalog()))

    answer = session.reply("Я уже абонент, интернет пропал")

    assert "не переключает" in answer
    assert "квартиру" not in answer


def test_voice_session_recovers_when_llm_is_unavailable():
    class BrokenLLM:
        def respond(self, *args, **kwargs):
            raise LLMError("offline")

    session = VoiceSession(AddressCatalog.from_json(sample_catalog()), BrokenLLM())
    answer = session.reply("Привет")

    assert "Не удалось получить ответ" in answer


def test_voice_session_discards_old_address_after_same_street_correction():
    session = VoiceSession(AddressCatalog.from_json(sample_catalog()))
    first = session.reply("Хочу подключить интернет в квартире, Лесная 14")
    corrected = session.reply("Нет, Лесная 999")

    assert "подключённый" in first
    assert "Не нашёл точный номер" in corrected
    assert session.consultation.address is None


def test_voice_session_asks_when_turn_contains_two_possible_house_numbers():
    payload = sample_catalog()
    payload["homes"][0].append({"id": 9, "t": "1000", "s": 3})
    session = VoiceSession(AddressCatalog.from_json(payload))

    answer = session.reply("Хочу подключить интернет в квартире, Лесная 14, бюджет 1000")

    assert "несколько вариантов" in answer
    assert session.consultation.address is None


def test_corpus_only_correction_uses_previous_street_and_house():
    session = VoiceSession(AddressCatalog.from_json(sample_catalog()))
    session.reply("Хочу интернет в квартире, Лесная 10")

    corrected = session.reply("Нет, корпус второй")

    assert "дом 10 корпус 2" in corrected
    assert session.consultation.address.house.id == 7


def test_found_address_is_not_repeated_for_unrelated_follow_up():
    class RecordingLLM:
        def __init__(self):
            self.prompts = []

        def respond(self, text, **kwargs):
            self.prompts.append(text)
            return "Да, расскажу подробнее."

    llm = RecordingLLM()
    session = VoiceSession(AddressCatalog.from_json(sample_catalog()), llm)
    found = session.reply("Хочу интернет в квартире, Лесная 14")
    follow_up = session.reply("А что означает этот статус?")

    assert "подключённый" in found
    assert follow_up == "Да, расскажу подробнее."
    assert llm.prompts == ["А что означает этот статус?"]
