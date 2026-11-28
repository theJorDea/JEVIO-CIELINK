import json
from io import BytesIO

import pytest

from cielink_asr.llm import ChatClient, LLMError


class FakeResponse:
    def __init__(self, value):
        self.value = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.value


def test_ollama_request_uses_local_chat_api():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"message": {"content": "Здравствуйте! Чем помочь?"}})

    client = ChatClient(provider="ollama", model="qwen3:4b", opener=opener)
    reply = client.respond("Привет", system_prompt="Коротко отвечай")

    request, timeout = requests[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert reply == "Здравствуйте! Чем помочь?"
    assert request.full_url == "http://localhost:11434/api/chat"
    assert payload["model"] == "qwen3:4b"
    assert payload["messages"][1]["content"] == "Привет"
    assert payload["think"] is False
    assert payload["keep_alive"] == "10m"
    assert payload["options"] == {
        "temperature": 0.3,
        "num_predict": 120,
        "num_ctx": 4096,
    }
    assert timeout == 45.0


def test_ollama_does_not_inherit_openai_credentials(monkeypatch):
    monkeypatch.setenv('CIELINK_LLM_PROVIDER', 'ollama')
    monkeypatch.setenv('OPENAI_API_KEY', 'not-for-ollama')
    monkeypatch.delenv('CIELINK_LLM_API_KEY', raising=False)
    assert ChatClient.from_env().api_key is None


def test_custom_endpoint_requires_explicit_credentials(monkeypatch):
    monkeypatch.setenv('CIELINK_LLM_PROVIDER', 'openai-compatible')
    monkeypatch.setenv('CIELINK_LLM_BASE_URL', 'https://api.example.test/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'not-for-custom-endpoint')
    monkeypatch.delenv('CIELINK_LLM_API_KEY', raising=False)
    with pytest.raises(ValueError):
        ChatClient.from_env()


def test_chat_request_preserves_recent_conversation_history():
    requests = []

    def opener(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse({"message": {"content": "Запомнил."}})

    client = ChatClient(provider="ollama", model="qwen3:4b", opener=opener)
    client.respond(
        "Это квартира",
        system_prompt="Веди разговор",
        history=[
            {"role": "user", "content": "Хочу интернет"},
            {"role": "assistant", "content": "Квартира или дом?"},
        ],
    )

    assert requests[0]["messages"][1:] == [
        {"role": "user", "content": "Хочу интернет"},
        {"role": "assistant", "content": "Квартира или дом?"},
        {"role": "user", "content": "Это квартира"},
    ]


def test_openai_compatible_request_sends_api_key():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse({"choices": [{"message": {"content": "Добрый день."}}]})

    client = ChatClient(
        provider="openai-compatible",
        model="test-model",
        base_url="https://api.example.test/v1",
        api_key="test-secret",
        opener=opener,
    )

    assert client.respond("Привет", system_prompt="Отвечай") == "Добрый день."
    request = requests[0]
    assert request.full_url == "https://api.example.test/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer test-secret"


def test_cloud_backend_requires_key():
    with pytest.raises(ValueError, match="CIELINK_LLM_API_KEY"):
        ChatClient(provider="openai-compatible", model="test-model")


def test_invalid_response_is_reported():
    client = ChatClient(
        provider="ollama",
        model="qwen3:4b",
        opener=lambda *args, **kwargs: FakeResponse({"done": True}),
    )
    with pytest.raises(LLMError, match="does not contain assistant text"):
        client.respond("Привет", system_prompt="Отвечай")


def test_from_env_reads_timeout(monkeypatch):
    monkeypatch.setenv("CIELINK_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("CIELINK_LLM_MODEL", "test-model")
    monkeypatch.setenv("CIELINK_LLM_TIMEOUT", "120")
    client = ChatClient.from_env()
    assert client is not None
    assert client.timeout == 120.0
