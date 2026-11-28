import numpy as np

from cielink_asr.echo import _InterruptiblePlayback, _resolve_voice_url


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
    assert playback.events.get_nowait() == "started"

    playback.interrupt()
    playback.render(output, 4)

    assert output[:, 0].tolist() == [0.0] * 4
    assert not playback.speaking.is_set()
    assert playback.events.get_nowait() == "interrupted"


def test_playback_reports_natural_completion():
    playback = _InterruptiblePlayback(np)
    playback.play(np.ones(4, dtype="float32"), frame_samples=4)
    output = np.empty((4, 1), dtype="float32")

    playback.render(output, 4)
    playback.render(output, 4)

    assert playback.events.get_nowait() == "started"
    assert playback.events.get_nowait() == "ended"
    assert not playback.speaking.is_set()
