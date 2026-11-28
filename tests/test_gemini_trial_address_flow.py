from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from cielink import gemini_trial as trial


def test_partial_address_is_not_a_coverage_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    generated = AsyncMock(return_value='Пожалуйста!')
    monkeypatch.setattr(trial, 'generate', generated)
    with TestClient(trial.app).websocket_connect('/ws/voice') as ws:
        ws.receive_json()
        def say(text):
            ws.send_json({'type': 'final', 'offset_ms': 1, 'text': text})
            return ws.receive_json()
        for fragment in ['А, ну пусть будет дом 3.', 'Том 3, корпус 4.', 'Ну хорошо, давайте другой адрес посмотрим.', 'А корпус 5.', 'Дом 4, корпус 7.']:
            assert say(fragment)['route'] == 'address:clarify'
        negative = say('Улица Колотушкина, дом 3.')
        assert negative['route'] == 'address:lookup' and 'не можем подключить' in negative['text']
        positive = say('Улица Пушкина, дом 1, корпус 5. Сколько стоит подключение?')
        assert positive['route'] == 'address:lookup' and '1700' in positive['text']
        say('Какие тарифы вообще существуют?')
        generated.assert_awaited_once()
