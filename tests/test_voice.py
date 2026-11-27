from fastapi.testclient import TestClient
import pytest
from cielink.api import create_app


def test_socket_training_protocol_and_correction(monkeypatch):
    monkeypatch.setenv('CIELINK_STT', 'text')
    with TestClient(create_app()).websocket_connect('/ws/voice') as ws:
        ready = ws.receive_json()
        assert ready['config']['stt'] == 'text' and ready['session_id']
        ws.send_json({'type': 'unknown'})
        assert ws.receive_json()['type'] == 'error'
        seq = 0
        for attempt in range(3):
            for kind, text in [('partial', 'корпус'), ('final', 'корпус первый')]:
                ws.send_json({'type': kind, 'text': text, 'offset_ms': attempt * 100})
                e = ws.receive_json()['event']; seq += 1
                assert (e['type'], e['sequence']) == (kind, seq)
            assert ws.receive_json()['event']['type'] == 'reply_generated'; seq += 1
            reply = ws.receive_json()
            assert 'один' in reply['text']
            for kind in ('playback_started', 'playback_interrupted'):
                ws.send_json({'type': kind, 'reply_id': reply['reply_id'], 'offset_ms': attempt * 100 + 1})
                assert ws.receive_json()['event']['type'] == kind; seq += 1
            ws.send_json({'type': 'playback_ended', 'reply_id': reply['reply_id'], 'offset_ms': attempt * 100 + 2})
            ws.send_json({'type': 'final', 'text': 'стоп, корпус второй', 'offset_ms': attempt * 100 + 3})
            e = ws.receive_json()['event']; seq += 1
            assert (e['type'], e['sequence']) == ('final', seq)
            assert ws.receive_json()['event']['type'] == 'reply_generated'; seq += 1
            assert 'корпус два' in ws.receive_json()['text']


def test_browser_from_other_site_cannot_open_local_session():
    from starlette.websockets import WebSocketDisconnect
    with TestClient(create_app()) as client:
        with pytest.raises(WebSocketDisconnect) as err:
            with client.websocket_connect('/ws/voice', headers={'origin': 'https://elsewhere.invalid'}):
                pytest.fail('foreign origin accepted')
        assert err.value.code == 1008


@pytest.mark.parametrize('path', ['/voice', '/static/voice.js'])
def test_browser_stand_is_served(path):
    with TestClient(create_app()) as client:
        assert client.get(path).status_code == 200
