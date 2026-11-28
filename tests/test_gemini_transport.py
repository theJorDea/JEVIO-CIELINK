import asyncio
from unittest.mock import Mock

import pytest

from cielink import gemini


def test_no_key_does_not_make_a_network_call(monkeypatch):
    monkeypatch.delenv('POLZA_API_KEY', raising=False)
    call = Mock()
    monkeypatch.setattr(gemini, '_request', call)
    assert asyncio.run(gemini.explain('Что такое роутер?')) is None
    call.assert_not_called()


def test_transport_error_allows_local_fallback(monkeypatch):
    monkeypatch.setenv('POLZA_API_KEY', 'fake')
    monkeypatch.setattr(gemini, '_request', Mock(side_effect=TimeoutError))
    assert asyncio.run(gemini.explain('Что такое роутер?')) is None


@pytest.mark.parametrize('text', ['первый, Иван', 'второй 1234', 'первый за сто рублей',
                                  'первый, мне на Ленина', 'первый\nвторой', 'первый@test.ru',
                                  'первый?', 'первый' * 21, 'первый ' * 17, 'пожалуйста'])
def test_classifier_rejects_whole_utterance_before_transport(monkeypatch, text):
    monkeypatch.setenv('POLZA_API_KEY', 'fake')
    post = Mock()
    monkeypatch.setattr(gemini, '_post', post)
    assert asyncio.run(gemini.classify_equipment_intent(text)) == 'unknown'
    post.assert_not_called()


def test_classifier_no_key(monkeypatch):
    monkeypatch.delenv('POLZA_API_KEY', raising=False)
    post = Mock()
    monkeypatch.setattr(gemini, '_post', post)
    assert asyncio.run(gemini.classify_equipment_intent('первый')) == 'unknown'
    post.assert_not_called()


def reply(content, finish='stop', refusal=None):
    return {'choices': [{'finish_reason': finish, 'message': {'content': content, 'refusal': refusal}}]}


@pytest.mark.parametrize('label', ['first', 'second', 'unknown'])
def test_classifier_sends_only_symbols_and_requests_schema(monkeypatch, label):
    monkeypatch.setenv('POLZA_API_KEY', 'fake')
    post = Mock(return_value=reply('{"intent":"' + label + '"}'))
    monkeypatch.setattr(gemini, '_post', post)
    assert asyncio.run(gemini.classify_equipment_intent('Первый, ой, лучше второй!')) == label
    key, payload = post.call_args.args
    assert key == 'fake'
    assert payload['messages'][1:] == [{'role': 'user', 'content': 'FIRST CORRECTION PREFER SECOND'}]
    assert payload['response_format']['type'] == 'json_schema'
    contract = payload['response_format']['json_schema']
    assert contract['strict'] is True
    assert contract['schema']['additionalProperties'] is False
    assert set(contract['schema']['properties']['intent']['enum']) == {'first', 'second', 'unknown'}


@pytest.mark.parametrize('response', [reply('first'), reply('{"intent":"buy"}'),
    reply('{"intent":"first","extra":false}'), reply('{"intent":"first","intent":"second"}'),
    reply('[["intent","first"]]'), reply('{"intent":"first"}', 'length'),
    reply('{"intent":"first"}', refusal='blocked'), {'choices': []}, TimeoutError()])
def test_classifier_fails_closed_on_transport_and_protocol_errors(monkeypatch, response):
    monkeypatch.setenv('POLZA_API_KEY', 'fake')
    post = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
    monkeypatch.setattr(gemini, '_post', post)
    assert asyncio.run(gemini.classify_equipment_intent('второй вариант')) == 'unknown'
