"""Optional Polza transport for explanations and clarification; never decides business actions."""
import asyncio
import json
import os
import re
from http.client import HTTPException
import urllib.error
import urllib.request

MODEL = 'google/gemini-2.5-flash-lite'
URL = 'https://polza.ai/api/v1/chat/completions'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _post(key: str, data: dict) -> dict:
    req = urllib.request.Request(URL, data=json.dumps(data).encode(), headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=2.5) as response:
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError('oversized response')
        return json.loads(body)


def _request(key: str, question: str) -> str | None:
    data = {'model': MODEL, 'temperature': 0.2, 'max_tokens': 90, 'stream': False,
            'messages': [{'role': 'system', 'content': 'Кратко и простыми словами по-русски объясни технический вопрос о домашнем интернете. Одно короткое предложение, без примеров и метафор. Примеры стиля: «Пинг показывает задержку сигнала в сети.» «Роутер соединяет домашние устройства с интернетом.» «Wi-Fi связывает устройства без проводов.» Не называй цены, адреса, условия оператора и не обещай никаких действий.'},
                         {'role': 'user', 'content': question}]}
    text = _post(key, data)['choices'][0]['message']['content']
    return text.strip() if isinstance(text, str) and 0 < len(text.strip()) <= 450 else None


_LABELS = ('first', 'second', 'unknown')
# The remote service gets only fixed symbols, never any caller-provided strings.
_WORDS = {word: symbol for symbol, words in {
    'FIRST': 'первый первую первого первая первым',
    'SECOND': 'второй вторую второго вторая вторым',
    'OPTION': 'роутер вариант модель номер',
    'CHOOSE': 'беру возьму выбираю выберу хочу давайте давай',
    'PREFER': 'лучше пожалуй наверное скорее',
    'ME': 'мне я', 'NOT': 'не нет', 'BUT': 'а но', 'CORRECTION': 'точнее вернее ой',
    'POLITE': 'пожалуйста', 'THEN': 'тогда',
}.items() for word in words.split()}


def _classify(key: str, symbols: list[str]) -> str:
    schema = {'type': 'object', 'properties': {'intent': {'type': 'string', 'enum': _LABELS}},
              'required': ['intent'], 'additionalProperties': False}
    data = {'model': MODEL, 'temperature': 0, 'max_tokens': 40, 'stream': False,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'equipment_intent', 'strict': True, 'schema': schema}},
            'messages': [{'role': 'system', 'content': 'Infer the intended option from these symbols '
                          'only to ask a clarifying question. If ambiguous return unknown.'},
                         {'role': 'user', 'content': ' '.join(symbols)}]}
    choice = _post(key, data)['choices'][0]
    if choice['finish_reason'] != 'stop' or choice['message'].get('refusal'):
        return 'unknown'
    content = choice['message']['content']
    if not isinstance(content, str) or len(content) > 128:
        return 'unknown'
    # Preserve pairs so duplicate keys cannot override a conflicting label.
    pairs = json.loads(content, object_pairs_hook=tuple)
    for label in _LABELS:
        if pairs == (('intent', label),):
            return label
    return 'unknown'


async def classify_equipment_intent(text: str) -> str:
    """Suggest a clarification label only. Never use as an equipment selection."""
    key = os.environ.get('POLZA_API_KEY')
    if not key or not isinstance(text, str) or not 0 < len(text) <= 120:
        return 'unknown'
    if not re.fullmatch(r'[А-Яа-яЁё ,.!]+', text):
        return 'unknown'
    words = re.findall(r'[а-яё]+', text.lower())
    if not 1 <= len(words) <= 16 or any(word not in _WORDS for word in words):
        return 'unknown'
    symbols = [_WORDS[word] for word in words]
    if 'FIRST' not in symbols and 'SECOND' not in symbols:
        return 'unknown'
    try:
        return await asyncio.wait_for(asyncio.to_thread(_classify, key, symbols), timeout=3)
    except (OSError, TimeoutError, HTTPException, ValueError, KeyError, IndexError, TypeError,
            AttributeError, RecursionError):
        return 'unknown'


async def explain(text: str) -> str | None:
    key = os.environ.get('POLZA_API_KEY')
    if not key:
        return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_request, key, text), timeout=3)
    except (OSError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
        return None
