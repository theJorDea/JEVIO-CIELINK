"""Local Russian speech synthesis adapters."""

from __future__ import annotations

import importlib.util
import filecmp
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable


class SileroTTS:
    def __init__(
        self,
        *,
        speaker: str = "baya",
        sample_rate: int = 8_000,
        model_loader: Callable[..., Any] | None = None,
    ) -> None:
        if model_loader is None:
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError(
                    "PyTorch is required for Silero TTS. Install CIELINK ASR dependencies."
                ) from exc
            model_loader = lambda: torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker="v5_ru",
            )[0]

        self._model = model_loader()
        self.speaker = speaker
        self.sample_rate = sample_rate

    def synthesize(self, text: str) -> Any:
        if not text.strip():
            raise ValueError("Text to synthesize must not be empty")
        return self._model.apply_tts(
            text=text,
            speaker=self.speaker,
            sample_rate=self.sample_rate,
        )


class KokoroTTS:
    """Kokoro-RU 82M with Russian stress-aware grapheme-to-phoneme conversion."""

    repo_id = "zaakirio/kokoro-ru"

    def __init__(self, *, speaker: str = "sveta", device: str = "cpu") -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download, snapshot_download
        except ImportError as exc:
            raise RuntimeError(
                "Kokoro TTS dependencies are missing. Install with "
                "`pip install -e '.[asr,voice-test]'`."
            ) from exc

        if speaker not in {"sveta", "masha", "dima"}:
            raise ValueError("Kokoro-RU speaker must be sveta, masha, or dima")

        model_dir = snapshot_download(
            repo_id=self.repo_id,
            allow_patterns=[
                "kokoro-config.json",
                "ru_g2p.py",
                "espeak-data/ru_dict",
                f"onnx/model{'_dima' if speaker == 'dima' else ''}_quantized.onnx",
                f"voices/{speaker}.bin",
            ],
        )
        try:
            import espeakng_loader
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
        except ImportError as exc:
            raise RuntimeError(
                "Kokoro's eSpeak runtime is missing. Reinstall with "
                "`pip install -e '.[asr,voice-test]'`."
            ) from exc

        # Keep the DLL search handle alive for the lifetime of the TTS adapter.
        if hasattr(os, "add_dll_directory"):
            self._espeak_dll_directory = os.add_dll_directory(
                str(Path(espeakng_loader.get_library_path()).parent)
            )
        EspeakWrapper.set_library(espeakng_loader.get_library_path())

        # The model's custom ru_dict is paired with its own espeak build. The
        # portable loader ships a compatible binary data set, so merge only
        # that dictionary into a cached copy of the loader's data.
        loader_data = Path(espeakng_loader.get_data_path())
        merged_data = Path.home() / ".cache" / "cielink" / "espeak-data"
        custom_ru_dict = Path(model_dir) / "espeak-data" / "ru_dict"
        merged_ru_dict = merged_data / "ru_dict"
        if not merged_ru_dict.exists() or not filecmp.cmp(
            custom_ru_dict, merged_ru_dict, shallow=False
        ):
            shutil.copytree(loader_data, merged_data, dirs_exist_ok=True)
            shutil.copy2(custom_ru_dict, merged_ru_dict)
        EspeakWrapper.set_data_path(str(merged_data))

        module_path = f"{model_dir}/ru_g2p.py"
        config_path = Path(model_dir) / "kokoro-config.json"
        g2p_config_path = Path.home() / ".cache" / "cielink" / "kokoro-config.json"
        g2p_config_path.parent.mkdir(parents=True, exist_ok=True)
        g2p_config_path.write_text(
            json.dumps(json.loads(config_path.read_text(encoding="utf-8")), ensure_ascii=True),
            encoding="ascii",
        )
        spec = importlib.util.spec_from_file_location("cielink_kokoro_ru_g2p", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load Kokoro-RU phonemizer")
        g2p_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = g2p_module
        spec.loader.exec_module(g2p_module)
        # HF stores snapshot files through symlinks. Pass the snapshot asset
        # paths explicitly because ru_g2p.py resolves its own __file__ to the
        # blob cache, where the companion espeak-data directory does not live.
        self._g2p = g2p_module.RuG2P(
            espeak_data=str(merged_data),
            vocab_path=str(g2p_config_path),
        )
        espeak_g2p = self._g2p._espeak

        def phonemize_with_metadata(text: str) -> tuple[str, None]:
            return espeak_g2p(text), None

        self._g2p._espeak = phonemize_with_metadata

        model_name = f"model{'_dima' if speaker == 'dima' else ''}_quantized.onnx"
        model_path = hf_hub_download(self.repo_id, f"onnx/{model_name}")
        voice_path = hf_hub_download(self.repo_id, f"voices/{speaker}.bin")
        config_path = hf_hub_download(self.repo_id, "kokoro-config.json")
        self._session = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )
        self._voice = np.fromfile(voice_path, dtype=np.float32).reshape(510, 256)
        with open(config_path, encoding="utf-8") as config_file:
            self._vocab = json.load(config_file)["vocab"]
        self._np = np
        self.speaker = speaker
        self.sample_rate = 24_000

    def synthesize(self, text: str) -> Any:
        if not text.strip():
            raise ValueError("Text to synthesize must not be empty")
        phonemes, unknown = self._g2p(text)
        if unknown:
            raise ValueError(f"Kokoro-RU cannot pronounce symbols: {''.join(sorted(unknown))}")
        input_ids = self._np.asarray(
            [[0, *(self._vocab[symbol] for symbol in phonemes), 0]], dtype=self._np.int64
        )
        output = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "style": self._voice[len(phonemes) - 1][None, :],
                "speed": self._np.ones(1, dtype=self._np.float32),
            },
        )[0]
        return self._np.asarray(output, dtype="float32").reshape(-1)


class OmniVoiceTTS:
    """GPU OmniVoice adapter with a reproducible automatic Russian voice."""

    repo_id = "k2-fsa/OmniVoice"

    def __init__(self, *, device: str = "cuda", language: str = "Russian") -> None:
        try:
            import torch
            from omnivoice import OmniVoice
        except ImportError as exc:
            raise RuntimeError(
                "OmniVoice dependencies are missing. Install with "
                "`pip install -e '.[asr,voice-test]'`."
            ) from exc

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("OmniVoice requires a CUDA GPU for this local adapter")
        self._torch = torch
        self._model = OmniVoice.from_pretrained(
            self.repo_id,
            device_map=device,
            dtype=torch.float16 if device.startswith("cuda") else torch.float32,
        )
        self._model.eval()
        self.language = language
        self.device = device
        self.speaker = "fixed synthetic voice"
        self.sample_rate = int(self._model.sampling_rate)
        self._voice_prompt = self._create_voice_prompt()

    def _generate(self, text: str, **kwargs: Any) -> Any:
        devices = [self._torch.cuda.current_device()] if self.device.startswith("cuda") else []
        with self._torch.random.fork_rng(devices=devices):
            self._torch.manual_seed(42)
            if devices:
                self._torch.cuda.manual_seed_all(42)
            return self._model.generate(
                text=text,
                language=self.language,
                num_step=32,
                guidance_scale=2.0,
                speed=1.0,
                **kwargs,
            )[0]

    def _create_voice_prompt(self) -> Any:
        reference_text = "Здравствуйте. Я голосовой помощник провайдера. Спокойно и подробно отвечу на ваши вопросы."
        reference_audio = self._generate(
            reference_text,
            instruct="male, middle-aged, moderate pitch",
        )
        waveform = self._torch.from_numpy(reference_audio)
        return self._model.create_voice_clone_prompt(
            ref_audio=(waveform, self.sample_rate),
            ref_text=reference_text,
        )

    def synthesize(self, text: str) -> Any:
        if not text.strip():
            raise ValueError("Text to synthesize must not be empty")
        return self._generate(text, voice_clone_prompt=self._voice_prompt)
