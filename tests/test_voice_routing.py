from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cielink import gemini
from cielink.api import create_app


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model = AsyncMock(return_value='Пинг измеряется в миллисекундах.')
    monkeypatch.setattr(gemini, 'explain', model)
    with TestClient(create_app()).websocket_connect('/ws/voice') as ws:
        assert ws.receive_json()['type'] == 'ready'
        offset = 0

        def say(text, route):
            nonlocal offset
            offset += 1
            ws.send_json({'type': 'final', 'text': text, 'offset_ms': offset})
            assert ws.receive_json()['event']['type'] == 'final'
            event = ws.receive_json()['event']
            reply = ws.receive_json()
            assert event['type'] == 'reply_generated' and reply['type'] == 'reply'
            assert event['route'] == reply['route'] == route
            return reply['text']

        yield say, model


def test_social_mixed_and_sensitive_turns_stay_local(session):
    say, model = session
    assert 'Здравствуйте' in say('Привет!', 'local:greeting')
    assert 'свидания' in say('Спасибо, пока!', 'local:farewell')
    assert 'адрес' in say('До свидания, сколько стоит подключение?', 'local:payment')
    mixed = say('Что такое Wi-Fi? И проверьте мой платёж.', 'local:payment')
    assert 'радиосигналу' in mixed and 'счёта' in mixed
    assert 'специалисту' in say('Привет, нужен специалист!', 'local:crm')
    assert 'специалисту' in say('Да, передайте специалисту', 'local:crm')  # not delivered
    say('Что такое Wi-Fi? Адрес: Учебная, дом 18', 'local:consultation')
    assert '3900' in say('Здравствуйте, какие есть роутеры?', 'local:equipment')
    assert '3900' in say('Беру второй', 'local:equipment')
    model.assert_not_awaited()


def test_only_standalone_education_uses_gemini_and_invalidates_proposal(session):
    say, model = session
    say('Нужен специалист', 'local:crm')
    assert say('Привет! Что такое пинг? Спасибо, пока!', 'gemini:education') == model.return_value
    model.assert_awaited_once_with('что такое пинг')
    assert 'не создаю' in say('Да, передайте специалисту', 'local:crm')


@pytest.mark.parametrize('bad', [None, '', 'Подключение за 500 рублей!', 'Я уже записал вас к мастеру.',
                                 'Сделаем за полцены.'])
def test_model_cannot_quote_or_promise_and_failure_is_local(session, bad):
    say, model = session
    model.return_value = bad
    assert 'радиосигналу' in say('Как работает Wi-Fi?', 'local:education_fallback')
    model.assert_awaited_once()


def test_router_consultation_smoke_in_one_socket(session):
    say, model = session
    model.return_value = 'Роутер соединяет устройства в сеть.'
    assert say('Что такое роутер?', 'gemini:education') == model.return_value
    assert '3900' in say('Привет, сколько стоит роутер?', 'local:equipment')
    assert 'не оформляю' in say('Пока не оформляйте', 'local:crm')
    assert '2400' in say('Мне надо поставить роутер домой', 'local:equipment')
    assert '2400' in say('Давайте первый вариант', 'local:equipment')
    assert '3900' in say('Нет, второй', 'local:equipment')
    price = say('А это разово или в месяц?', 'local:equipment')
    assert '3900' in price and 'один раз' in price
    model.assert_awaited_once_with('что такое роутер')


def test_real_conversation_in_one_socket(session, monkeypatch):
    say, model = session
    classifier = AsyncMock(return_value='unknown')
    monkeypatch.setattr(gemini, 'classify_equipment_intent', classifier)
    say('Какие есть роутеры?', 'local:equipment')
    for text, price in [
        ('Давайте первый вариант. А, нет, давайте лучше второй.', '3900'),
        ('Я понял, что у вас 2 варианта. Давайте мне первый.', '2400'),
        ('Хорошо, можно всё-таки второй?', '3900'),
    ]:
        assert price in say(text, 'local:equipment')
    assert 'адрес' not in say('Я понял', 'local:equipment')
    price = say('А это разово или в месяц?', 'local:equipment')
    assert '3900' in price and 'один раз' in price
    say('Хочу подключить интернет', 'local:consultation')
    assert 'слушаю' in say('А.', 'local:filler')
    assert 'Пожалуйста' in say('Хорошо, спасибо', 'local:thanks')
    mixed = say('Что такое Wi-Fi? И проверьте мой платёж.', 'local:payment')
    assert 'радиосигналу' in mixed and 'личном кабинете' in mixed
    model.assert_not_awaited()
    classifier.assert_not_awaited()


def test_model_suggestion_does_not_create_a_confirmable_proposal(session, monkeypatch):
    say, _ = session
    classifier = AsyncMock(return_value='second')
    monkeypatch.setattr(gemini, 'classify_equipment_intent', classifier)
    say('Какие есть роутеры?', 'local:equipment')
    assert 'беру второй' in say('Давайте скорее второй', 'local:equipment')
    classifier.assert_awaited_once()
    assert 'не создаю' in say('Да, передайте специалисту', 'local:crm')
