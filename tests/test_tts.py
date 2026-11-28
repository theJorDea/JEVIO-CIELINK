from types import SimpleNamespace

import pytest

from cielink_asr.tts import KokoroTTS, OmniVoiceTTS, SileroTTS


def test_silero_loads_russian_v5_and_synthesizes_at_telephone_rate():
    calls = []
    expected_audio = object()
    model = SimpleNamespace(
        apply_tts=lambda **kwargs: calls.append(kwargs) or expected_audio
    )
    tts = SileroTTS(model_loader=lambda: model)

    audio = tts.synthesize("Здравствуйте")

    assert audio is expected_audio
    assert calls == [
        {"text": "Здравствуйте", "speaker": "baya", "sample_rate": 8000}
    ]


def test_silero_rejects_empty_text():
    tts = SileroTTS(model_loader=lambda: object())
    with pytest.raises(ValueError, match="must not be empty"):
        tts.synthesize("  ")


def test_kokoro_uses_phonemes_and_voice_style_for_onnx_inference():
    import numpy as np

    calls = []
    tts = KokoroTTS.__new__(KokoroTTS)
    tts._g2p = lambda text: ("abc", set())
    tts._session = SimpleNamespace(
        run=lambda outputs, inputs: calls.append((outputs, inputs))
        or [np.array([[0.1, -0.2]], dtype=np.float32)]
    )
    tts._voice = np.zeros((510, 256), dtype=np.float32)
    tts._vocab = {"a": 1, "b": 2, "c": 3}
    tts._np = np

    audio = tts.synthesize("тест")

    assert audio.tolist() == pytest.approx([0.1, -0.2])
    _, inputs = calls[0]
    assert inputs["input_ids"].tolist() == [[0, 1, 2, 3, 0]]
    assert inputs["style"].shape == (1, 256)
    assert inputs["speed"].tolist() == [1.0]


def test_kokoro_rejects_unknown_phonemes():
    tts = KokoroTTS.__new__(KokoroTTS)
    tts._g2p = lambda text: ("x", {"x"})

    with pytest.raises(ValueError, match="cannot pronounce"):
        tts.synthesize("тест")


def test_omnivoice_generates_russian_with_repeatable_seed():
    import numpy as np
    import torch

    calls = []
    expected_audio = np.zeros(12, dtype=np.float32)
    tts = OmniVoiceTTS.__new__(OmniVoiceTTS)
    tts._torch = torch
    tts._model = SimpleNamespace(
        generate=lambda **kwargs: calls.append(kwargs) or [expected_audio]
    )
    tts.device = "cpu"
    tts.language = "Russian"
    tts._voice_prompt = "fixed-prompt"

    assert tts.synthesize("Здравствуйте") is expected_audio
    assert calls == [
        {
            "text": "Здравствуйте",
            "language": "Russian",
            "num_step": 32,
            "guidance_scale": 2.0,
            "speed": 1.0,
            "voice_clone_prompt": "fixed-prompt",
        }
    ]


def test_omnivoice_creates_prompt_from_a_repeatable_synthetic_reference():
    import numpy as np
    import torch

    calls = []
    expected_prompt = object()
    tts = OmniVoiceTTS.__new__(OmniVoiceTTS)
    tts._torch = torch
    tts._model = SimpleNamespace(
        generate=lambda **kwargs: calls.append(("generate", kwargs))
        or [np.zeros(24_000, dtype=np.float32)],
        create_voice_clone_prompt=lambda **kwargs: calls.append(("prompt", kwargs))
        or expected_prompt,
    )
    tts.device = "cpu"
    tts.language = "Russian"
    tts.sample_rate = 24_000

    prompt = tts._create_voice_prompt()

    assert prompt is expected_prompt
    assert calls[0][0] == "generate"
    assert calls[0][1]["text"] == (
        "Здравствуйте. Я голосовой помощник провайдера. "
        "Спокойно и подробно отвечу на ваши вопросы."
    )
    assert calls[0][1]["instruct"] == "male, middle-aged, moderate pitch"
    assert calls[1][0] == "prompt"
    assert calls[1][1]["ref_text"] == calls[0][1]["text"]


def test_omnivoice_rejects_empty_text():
    tts = OmniVoiceTTS.__new__(OmniVoiceTTS)
    with pytest.raises(ValueError, match="must not be empty"):
        tts.synthesize("  ")
