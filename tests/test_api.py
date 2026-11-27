import pytest
from fastapi.testclient import TestClient
from cielink.api import create_app


def test_training_api_runs_without_keys():
    with TestClient(create_app()) as client:
        assert client.get('/health').json() == {'status': 'ok', 'mode': 'training'}


def test_unknown_mode_not_silently_treated_as_production(monkeypatch):
    monkeypatch.setenv('CIELINK_MODE', 'production')
    with pytest.raises(ValueError, match='training'):
        create_app()
