"""A web call can be observed live and retains its actual result after hangup."""
from tempfile import NamedTemporaryFile
from pathlib import Path
from fastapi.testclient import TestClient
from cielink.api import create_app
from cielink.review import ReviewStore


def test_socket_to_live_card_and_receipt(monkeypatch):
    with NamedTemporaryFile(delete_on_close=False) as review_file, NamedTemporaryFile(delete_on_close=False) as crm_file:
        review_db = Path(review_file.name)
        monkeypatch.setenv('CIELINK_REVIEW_DB', str(review_db))
        monkeypatch.setenv('CIELINK_CRM_URL', f'sqlite:///{crm_file.name}')
        monkeypatch.setenv('CIELINK_TTS', 'text')
        with TestClient(create_app(), base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
            with client.websocket_connect('/ws/voice') as ws:
                sid = ws.receive_json()['session_id']
                path = '/api/sessions/' + sid
                assert client.get(path).json()['status'] == 'active'
                ws.send_json({'type':'final','text':'Учебная 18 корпус 2','offset_ms':20})
                ws.receive_json(); ws.receive_json(); reply = ws.receive_json()
                ws.send_json({'type':'text_displayed','reply_id':reply['reply_id'],'offset_ms':21}); ws.receive_json()
                ws.send_json({'type':'final','text':'да проверьте адрес','offset_ms':30})
                ws.receive_json(); ws.receive_json(); receipt_text = ws.receive_json()['text']
                assert any(e['text'] == 'да проверьте адрес' for e in client.get(path).json()['events'])
            # TestClient can cancel a socket scope on context exit; the real transport receives disconnect.
            import time
            for _ in range(50):
                result = client.get(path).json()
                if result['status'] == 'finished': break
                time.sleep(.01)
            assert result['outcome'] == 'coverage_request' and receipt_text.endswith(f"Номер {result['receipt']['spoken_reference']}.")
            assert result['coverage_status'] == 'unknown' and not result['tracks']
            assert ReviewStore(review_db).analyze_next() == 'complete'
