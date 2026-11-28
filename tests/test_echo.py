import numpy as np
import pytest
from types import SimpleNamespace

from cielink_asr import echo
from cielink_asr.echo import _InterruptiblePlayback, _resolve_voice_url, _spoken_numbers, _turn_pause_ms


def test_tts_receives_russian_words_for_amounts_and_speeds():
    assert _spoken_numbers('До 500 мегабит. 1700 рублей. Номер 13.') == (
        'До пятисот мегабит. одна тысяча семьсот рублей. Номер тринадцать.'
    )
    assert _spoken_numbers('Нужно внести 1700 рублей.') == 'Нужно внести одну тысячу семьсот рублей.'


def test_pause_depends_on_transcript_and_speaker_pace():
    assert _turn_pause_ms('Да, оформляйте.', 1200, 0) == 800
    assert _turn_pause_ms('Мой бюджет не больше…', 1200, 0) == 1800
    assert _turn_pause_ms('Дом 18, корпус', 1200, 0) == 1800
    assert _turn_pause_ms('Хочу подключить интернет.', 1200, 1050) == 1350
    assert _turn_pause_ms('Да, оформляйте.', 1200, 1050) == 1350


def test_live_voice_backend_defaults_to_local_training_api():
    assert _resolve_voice_url(None, standalone=False, env={}) == (
        "ws://127.0.0.1:8000/ws/voice"
    )


def test_voice_backend_can_be_overridden_or_disabled():
    assert _resolve_voice_url(
        None, standalone=False, env={"CIELINK_VOICE_URL": "ws://stand.test/voice"}
    ) == "ws://stand.test/voice"
    assert _resolve_voice_url(
        "ws://explicit.test/voice", standalone=False, env={}
    ) == "ws://explicit.test/voice"
    assert _resolve_voice_url(None, standalone=True, env={}) is None


def test_playback_interrupt_discards_remaining_speech():
    playback = _InterruptiblePlayback(np)
    playback.play(np.ones(12, dtype="float32"), frame_samples=4)
    output = np.empty((4, 1), dtype="float32")

    playback.render(output, 4)
    assert output[:, 0].tolist() == [1.0] * 4
    assert playback.events.get_nowait() == ("started", None)

    playback.interrupt()
    playback.render(output, 4)

    assert output[:, 0].tolist() == [0.0] * 4
    assert not playback.speaking.is_set()
    assert playback.events.get_nowait() == ("interrupted", None)


def test_playback_reports_natural_completion():
    playback = _InterruptiblePlayback(np)
    playback.play(np.ones(4, dtype="float32"), frame_samples=4)
    output = np.empty((4, 1), dtype="float32")

    playback.render(output, 4)
    playback.render(output, 4)

    assert playback.events.get_nowait() == ("started", None)
    assert playback.events.get_nowait() == ("ended", None)
    assert not playback.speaking.is_set()


def test_playback_waits_for_the_second_fragment_before_reporting_complete():
    playback = _InterruptiblePlayback(np)
    frame = np.ones(4, dtype='float32')
    output = np.empty((4, 1), dtype='float32')
    playback.play(frame, 4, more=True)
    playback.render(output, 4)
    playback.render(output, 4)
    assert playback.events.get_nowait() == ('started', None)
    assert playback.events.empty() and playback.speaking.is_set()
    playback.append_last(frame, 4)
    playback.render(output, 4)
    playback.render(output, 4)
    assert playback.events.get_nowait() == ('ended', None)
    assert playback.events.empty()


def _mock_audio_input(monkeypatch, frames):
    import sys

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(echo, 'time', SimpleNamespace(monotonic=lambda: clock.now))

    class Stream:
        def __init__(self, *, callback, **_):
            self.callback = callback

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    stream = None

    def open_stream(**kwargs):
        nonlocal stream
        stream = Stream(**kwargs)
        return stream

    class FrameQueue(echo.queue.Queue):
        def get(self, *args, **kwargs):
            if self.maxsize and self.empty():
                value = next(frames)
                clock.now += 0.020
                frame = np.full((320, 1), value, dtype='float32')
                stream.callback(frame, np.empty_like(frame), 320, None, None)
            return super().get(*args, **kwargs)

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(Stream=open_stream))
    monkeypatch.setattr(echo.queue, 'Queue', FrameQueue)
    return clock


def test_live_ignores_noise_and_waits_for_an_unfinished_address(monkeypatch):
    frames = iter([1, 0, 1, 1, 1, 0] + [1] * 16 + [0] * 10 + [1] * 16 + [0] * 31
                  + [1] * 5 + [0] + [1] * 6 + [0])
    _mock_audio_input(monkeypatch, frames)
    transcripts = iter(['.', 'Дом 18, корпус первый.', 'Стоп, корпус второй.'])
    sent = []

    class Backend:
        greeting = None

        def final(self, text):
            sent.append(('final', text))
            return text, 'Ответ'

        def send(self, event, **_):
            sent.append((event, None))

        def close(self):
            pass

    monkeypatch.setattr(echo, '_VoiceBackend', lambda _: Backend())
    def new_session():
        text = next(transcripts)
        voiced_frames = 0

        def feed(audio):
            nonlocal voiced_frames
            voiced_frames += int((audio > 0).sum()) // 320
            partial = 'Дом 18, корпус' if text.startswith('Дом 18') and voiced_frames < 24 else text
            return SimpleNamespace(e2e_delta='', e2e_text=partial)

        def finish():
            if text.startswith('Дом 18'):
                assert voiced_frames == 32
            return SimpleNamespace(e2e_delta='', e2e_text=text)

        return SimpleNamespace(feed=feed, finish=finish)

    engine = SimpleNamespace(new_session=new_session)
    synth = SimpleNamespace(sample_rate=16000, synthesize=lambda _: np.zeros(16000, dtype='float32'))
    with pytest.raises(StopIteration):
        echo._run_live_conversation(sample_rate=16000, engine=engine, synthesizer=synth,
                                    llm=None, system_prompt='', silence_ms=20, threshold=0.5,
                                    voice_url='ws://test')

    assert sent == [('final', 'Дом 18, корпус первый.'), ('playback_started', None),
                    ('playback_interrupted', None), ('final', 'Стоп, корпус второй.')]


@pytest.mark.parametrize(('partial', 'internal_pause', 'expected_ms'), [
    ('Добрый день!', 0, 800),
    ('Мой бюджет не больше', 0, 1800),
    ('Здравствуйте, я хочу…', 0, 1800),
    ('Здравствуйте.', 1000, 1300),
])
def test_live_endpoint_and_first_output_callback(monkeypatch, capsys, partial, internal_pause, expected_ms):
    speech = [1] * 16
    if internal_pause:
        speech += [0] * (internal_pause // 20) + [1] * 16
    clock = _mock_audio_input(monkeypatch, iter(speech + [0] * (expected_ms // 20 + 1)))
    finals = []
    def feed(_):
        # Before resumption the transcript does not yet indicate a complete turn.
        text = '' if internal_pause and clock.now < len(speech) * .02 - .01 else partial
        return SimpleNamespace(e2e_delta='', e2e_text=text)

    def final(text):
        finals.append((text, round(clock.now * 1000) - len(speech) * 20))
        clock.now += .250  # backend time, distinct from endpoint silence
        return 'reply-id', 'Ответ'

    backend = SimpleNamespace(greeting=None, final=final, send=lambda *a, **kw: None, close=lambda: None)
    monkeypatch.setattr(echo, '_VoiceBackend', lambda _: backend)
    session = SimpleNamespace(feed=feed, finish=lambda: SimpleNamespace(e2e_delta='', e2e_text=partial))
    with pytest.raises(StopIteration):
        echo._run_live_conversation(sample_rate=16000, engine=SimpleNamespace(new_session=lambda: session),
                                    synthesizer=SimpleNamespace(sample_rate=16000, synthesize=lambda _: np.ones(320)),
                                    llm=None, system_prompt='', silence_ms=1200, threshold=.5, voice_url='ws://test')
    assert finals == [(partial, expected_ms)]
    output = capsys.readouterr().out
    assert f'порог {expected_ms} мс, пауза {expected_ms} мс' in output
    assert f'первый выходной callback с PCM] {expected_ms + 250 + 20} мс' in output


@pytest.mark.parametrize('route_source', ['event', 'reply', None])
def test_backend_reports_only_supplied_route(capsys, route_source):
    event = {'type': 'reply_generated'}
    reply = {'type': 'reply', 'reply_id': 'id', 'text': 'Ответ'}
    if route_source:
        (event if route_source == 'event' else reply)['route'] = 'equipment'
    messages = iter([{'type': 'event', 'event': event}, reply])
    backend = echo._VoiceBackend.__new__(echo._VoiceBackend)
    backend._socket = SimpleNamespace(recv=lambda: echo.json.dumps(next(messages)), send=lambda _: None)
    backend._started_at = echo.time.monotonic()
    assert backend.final('Вопрос') == ('id', 'Ответ')
    assert [s for s in capsys.readouterr().out.splitlines() if '[route]' in s] == (
        ['[route] equipment'] if route_source else []
    )
