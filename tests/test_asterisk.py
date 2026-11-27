import asyncio
from io import StringIO
import sqlite3
import struct
import wave

import pytest

from cielink import asterisk as a


PLAY = '200 result=0 endpos=8000'
REC = '200 result=8453 (timeout) endpos=16000'  # ast_waitfor may return remaining ms
UP = '200 result=6'
ADDRESS = 'учебная улица дом восемнадцать корпус один'


class Speech:
    def __init__(self, texts):
        self.texts = iter(texts)

    def synthesize(self, text, base):
        assert text and base.name == 'prompt'

    def recognize(self, base):
        assert base.name == 'caller'
        return next(self.texts)


@pytest.mark.parametrize('texts,replies,error,route,outcome,count', [
    ([ADDRESS, 'да подключайте'], [PLAY, REC, UP, PLAY, REC, UP, PLAY], None, 'DONE', 'order', 1),
    (['хочу оператора', 'да'], [PLAY, REC, UP, PLAY, REC, UP, PLAY], None, 'OPERATOR', 'handoff', 1),
    ([ADDRESS, 'нет'], [PLAY, REC, UP, PLAY, REC, UP, PLAY], None, 'DONE', 'no_action', 0),
    ([ADDRESS], [PLAY, REC, UP, '200 result=-1 endpos=13', '511 Command Not Permitted on a dead channel'], a.Hangup, None, 'disconnected', 0),
    ([ADDRESS], [PLAY, REC, UP, '200 result=-1 endpos=0', UP], RuntimeError, None, 'engine_failure', 0),
    ([ADDRESS, 'да'], [PLAY, REC, UP, PLAY, REC, 'HANGUP'], a.Hangup, None, 'disconnected', 0),
    ([], [PLAY, '200 result=-1 (hangup) endpos=10'], a.Hangup, None, 'disconnected', 0),
    ([''], [PLAY, REC, UP], RuntimeError, None, 'engine_failure', 0),
])
def test_agi_call(tmp_path, texts, replies, error, route, outcome, count):
    commands = StringIO()
    agi = a.AGI(StringIO('agi_channel: test\n\n' + '\n'.join(replies) + '\n'), commands)
    review = a.ReviewStore(tmp_path / 'review.db')
    store = a.OperationStore(f'sqlite:///{tmp_path}/crm.db')
    call = a.run_call(agi, Speech(texts), store, review, tmp_path)
    if error:
        with pytest.raises(error):
            asyncio.run(call)
    else:
        assert asyncio.run(call) == route
    assert len(commands.getvalue().splitlines()) == len(replies)
    session, = review.list_sessions()
    detail = review.detail(session['id'])
    assert session['outcome'] == outcome
    assert detail['tracks'] == []
    events = detail['events']
    completed = [e['text'] for e in events if e['type'] == 'playback_ended']
    assert len(completed) == replies.count(PLAY)
    if any('endpos=13' in r or 'endpos=0' in r for r in replies):
        assert events[-1]['type'] == 'playback_interrupted'
    with sqlite3.connect(tmp_path / 'crm.db') as conn:
        assert conn.execute('SELECT count(*) FROM crm_operations').fetchone() == (count,)
    if count:
        assert detail['receipt']['confirmed_terms']['prompt'] == completed[-2]
        assert detail['receipt']['receipt_id'] in completed[-1]


@pytest.mark.parametrize('enhanced,expected', [('1.0', [(3, False)]), ('0.0', [])])
def test_agi_detects_enhanced_audio(monkeypatch, enhanced, expected):
    calls = []
    monkeypatch.setattr(a.os, 'set_blocking', lambda fd, flag: calls.append((fd, flag)))
    a.AGI(StringIO(f'agi_channel: test\nagi_enhanced: {enhanced}\n\n'), StringIO())
    assert calls == expected


def test_read_audio_returns_all_available_bytes(monkeypatch):
    monkeypatch.setattr(a.os, 'set_blocking', lambda *_: None)
    agi = a.AGI(StringIO('agi_enhanced: 1.0\n\n'), StringIO())
    blocks = iter([b'one', b'two'])

    def read(fd, size):
        assert fd == 3
        if block := next(blocks, None):
            return block
        raise BlockingIOError

    monkeypatch.setattr(a.os, 'read', read)
    data = agi.read_audio()
    assert isinstance(data, bytes) and data == b'onetwo'


@pytest.mark.parametrize('options,last_level,completed', [
    ({}, 499, True), ({}, 500, False), ({'detect': False}, 500, True),
])
def test_play_prompt_checks_audio_between_wav_chunks(tmp_path, monkeypatch, options, last_level, completed):
    monkeypatch.setattr(a.os, 'set_blocking', lambda *_: None)
    agi = a.AGI(StringIO('agi_enhanced: 1.0\n\n'), StringIO())
    base = tmp_path / 'prompt'
    pcm = struct.pack('<3350h', *range(3350))
    with wave.open(str(base.with_suffix('.wav')), 'wb') as wav:
        wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm)

    def frame(level):  # zero mean, RMS == level; 20 ms at 8 kHz
        return struct.pack('<hh', level, -level) * 80

    # Ignore stale speech. Two loud frames, a quiet reset, then two loud
    # frames still aren't speech until the next read completes the streak.
    reads = iter([frame(900) * 3, frame(500) * 2 + frame(499) + frame(500) * 2,
                  frame(last_level), b''])
    events = []

    def read_audio():
        events.append('drain')
        return next(reads)

    def play(chunk):
        with wave.open(str(chunk.with_suffix('.wav')), 'rb') as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 8000)
            events.append(wav.readframes(wav.getnframes()))

    monkeypatch.setattr(agi, 'read_audio', read_audio)
    monkeypatch.setattr(agi, 'play', play)
    assert agi.play_prompt(base, **options) is completed
    expected = ['drain', pcm[:3200], 'drain', pcm[3200:6400], 'drain']
    assert events == expected + ([pcm[6400:], 'drain'] if completed else [])


def test_interrupted_offer_requires_fresh_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(a.os, 'set_blocking', lambda *_: None)
    agi = a.AGI(StringIO('agi_enhanced: 1.0\n\n'), StringIO())
    review = a.ReviewStore(tmp_path / 'review.db')
    store = a.OperationStore(f'sqlite:///{tmp_path}/crm.db')
    turns = []
    prompts = {}
    recorded = []
    answers = iter([ADDRESS, 'да подключайте', 'да подключайте'])

    class Caller:
        def synthesize(self, text, base):
            prompts[base] = text

        def recognize(self, base):
            assert recorded.pop() == base  # cannot recognize twice from one recording
            text = next(answers)
            turns.append(('answer', text))
            return text

    def play_prompt(base, *, detect):
        assert base == tmp_path / 'prompt'
        turns.append(('prompt' if detect else 'play', prompts[base]))
        return not detect or len(turns) != 3  # interrupt the first offer

    def command(line):
        assert not recorded
        base = tmp_path / 'caller'
        suffix = '0 BEEP s=2' if turns[-1][0] == 'play' else '0 s=2'
        assert line == f'RECORD FILE "{base}" wav "" 12000 {suffix}'
        with sqlite3.connect(tmp_path / 'crm.db') as db:
            assert db.execute('SELECT count(*) FROM crm_operations').fetchone() == (0,)
        recorded.append(base)
        return 0

    monkeypatch.setattr(agi, 'play_prompt', play_prompt)
    monkeypatch.setattr(agi, 'command', command)
    monkeypatch.setattr(agi, 'up', lambda: None)
    assert asyncio.run(a.run_call(agi, Caller(), store, review, tmp_path)) == 'DONE'
    greeting, address, interrupted, instruction, affirmative, replay, confirmation, receipt = turns
    assert address == ('answer', ADDRESS)
    assert instruction == ('play', 'Я вас слышу. После сигнала повторите фразу с самого начала.')
    assert affirmative == confirmation == ('answer', 'да подключайте')
    assert interrupted == replay == ('prompt', replay[1])
    session, = review.list_sessions()
    detail = review.detail(session['id'])
    assert [e['type'] for e in detail['events'] if e['text'] == replay[1]] == [
        'reply_generated', 'reply_generated', 'playback_ended',
    ]
    assert sum(e['type'] == 'playback_interrupted' for e in detail['events']) == 1
    assert detail['receipt']['confirmed_terms']['prompt'] == replay[1]
    assert receipt[0] == 'play'
    assert detail['receipt']['receipt_id'] in receipt[1]
    with sqlite3.connect(tmp_path / 'crm.db') as db:
        assert db.execute('SELECT count(*) FROM crm_operations').fetchone() == (1,)


@pytest.mark.parametrize('piper', [None, '/models/ru.onnx'])
def test_offline_audio_conversion_and_final_segments(monkeypatch, tmp_path, piper):
    from types import SimpleNamespace

    calls = []

    def command(args, data=None, *, capture=False):
        calls.append((args, data))
        return b'\0' * 10000 if capture else None

    class Recognizer:
        def __init__(self, model, rate):
            assert model == 'local' and rate == 16000

        def AcceptWaveform(self, chunk):
            return len(chunk) == 8000

        def Result(self):
            return '{"text":"не"}'

        def FinalResult(self):
            return '{"text":"надо"}'

    monkeypatch.setitem(a.sys.modules, 'vosk', SimpleNamespace(KaldiRecognizer=Recognizer))
    monkeypatch.setattr(a, 'audio_command', command)
    speech = a.LocalSpeech.__new__(a.LocalSpeech)
    speech.model, speech.piper = 'local', piper
    speech.synthesize('Добрый день.', tmp_path / 'prompt')
    assert calls[0][0][0] == ('piper' if piper else 'espeak-ng')
    assert calls[0][1].decode().strip() == 'Добрый день.'
    assert '8000' in calls[1][0] and calls[1][0][-1] == str(tmp_path / 'prompt.wav')
    assert speech.recognize(tmp_path / 'caller') == 'не надо'
    assert calls[2][0][1] == str(tmp_path / 'caller.wav') and '16000' in calls[2][0]


def test_worker_missing_model_leaves_operator_default(monkeypatch, tmp_path):
    monkeypatch.setenv('CIELINK_REVIEW_DB', str(tmp_path / 'review.db'))
    monkeypatch.setenv('CIELINK_CRM_URL', f'sqlite:///{tmp_path}/crm.db')
    monkeypatch.delenv('CIELINK_VOSK_MODEL', raising=False)
    monkeypatch.setattr(a.sys, 'stdin', StringIO('agi_request: cielink-agi\n\n'))
    monkeypatch.setattr(a.sys, 'stdout', StringIO())
    monkeypatch.setattr(a.os, 'umask', lambda _: None)
    assert a.main() == 1
    assert not a.sys.stdout.getvalue()
