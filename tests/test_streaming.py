from types import SimpleNamespace

import pytest
import torch

from cielink_asr.streaming import ASRSession, GigaAMEngine


class FakeStream:
    def __init__(self):
        self.calls = 0

    def transcribe_partial(self, audio):
        self.calls += 1
        return {
            "raw": SimpleNamespace(text="дом "),
            "e2e": SimpleNamespace(text="Дом "),
        }

    def flush(self):
        return {
            "raw": SimpleNamespace(text="18 корпус 2"),
            "e2e": SimpleNamespace(text="18, корпус 2."),
        }


def test_session_accumulates_partial_and_final_deltas():
    session = ASRSession(FakeStream())

    partial = session.feed(torch.zeros(320, dtype=torch.float32))
    final = session.finish()

    assert partial.raw_delta == "дом "
    assert partial.e2e_text == "Дом "
    assert final.is_final
    assert final.raw_text == "дом 18 корпус 2"
    assert final.e2e_text == "Дом 18, корпус 2."


def test_session_rejects_wrong_audio_and_feed_after_finish():
    session = ASRSession(FakeStream())
    with pytest.raises(ValueError, match="mono waveform"):
        session.feed(torch.zeros((1, 320), dtype=torch.float32))
    with pytest.raises(TypeError, match="float32"):
        session.feed(torch.zeros(320, dtype=torch.int16))

    session.finish()
    with pytest.raises(RuntimeError, match="already finished"):
        session.feed(torch.zeros(320, dtype=torch.float32))


def test_engine_loads_v4_streaming_model_with_selected_context():
    calls = []
    model = object()

    def loader(name, **options):
        calls.append((name, options))
        return model

    engine = GigaAMEngine(
        future_context_ms=160,
        device="cpu",
        model_loader=loader,
        stream_builder=lambda loaded, context: FakeStream(),
    )
    first, second = engine.new_session(), engine.new_session()

    assert calls == [("v4_rnnt", {"device": "cpu"})]
    assert first._stream is not second._stream
    assert engine.future_context_ms == 160


def test_engine_rejects_unsupported_context():
    with pytest.raises(ValueError, match="160, 320, or 640"):
        GigaAMEngine(future_context_ms=240, model_loader=lambda *args, **kwargs: None)
