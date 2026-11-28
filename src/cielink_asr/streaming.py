"""GigaAM-v4 RNN-T streaming adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ASRUpdate:
    raw_delta: str
    e2e_delta: str
    raw_text: str
    e2e_text: str
    is_final: bool


class ASRSession:
    """Owns one GigaAM stream and its transcript state for a single call."""

    def __init__(self, stream: Any, sample_rate: int = 16_000) -> None:
        self._stream = stream
        self.sample_rate = sample_rate
        self._raw_text = ""
        self._e2e_text = ""
        self._finished = False

    def feed(self, audio: Any) -> ASRUpdate:
        """Feed mono float32 PCM at 16 kHz and return newly decoded text."""
        if self._finished:
            raise RuntimeError("ASR session is already finished")
        if getattr(audio, "ndim", None) != 1:
            raise ValueError("Audio must be a one-dimensional mono waveform")
        if getattr(audio, "dtype", None) is None or str(audio.dtype) not in (
            "float32",
            "torch.float32",
        ):
            raise TypeError("Audio must use float32 samples")

        return self._update(self._stream.transcribe_partial(audio), is_final=False)

    def finish(self) -> ASRUpdate:
        """Flush the final partial packet and close this utterance."""
        if self._finished:
            raise RuntimeError("ASR session is already finished")
        self._finished = True
        return self._update(self._stream.flush(), is_final=True)

    def _update(self, result: Any, *, is_final: bool) -> ASRUpdate:
        raw_delta = result["raw"].text
        e2e_delta = result["e2e"].text
        self._raw_text += raw_delta
        self._e2e_text += e2e_delta
        return ASRUpdate(
            raw_delta=raw_delta,
            e2e_delta=e2e_delta,
            raw_text=self._raw_text,
            e2e_text=self._e2e_text,
            is_final=is_final,
        )


class GigaAMEngine:
    """Loads the selected model once and creates isolated streams per call."""

    def __init__(
        self,
        *,
        future_context_ms: int = 320,
        device: str | None = None,
        model_loader: Callable[..., Any] | None = None,
        stream_builder: Callable[[Any, int], Any] | None = None,
    ) -> None:
        if future_context_ms not in (160, 320, 640):
            raise ValueError("future_context_ms must be 160, 320, or 640")
        if model_loader is None:
            try:
                from gigaam import load_model
            except ImportError as exc:
                raise RuntimeError(
                    "GigaAM is not installed. Install the project with `.[asr]`."
                ) from exc
            model_loader = load_model

        options: dict[str, Any] = {}
        if device is not None:
            options["device"] = device
        self._model = model_loader("v4_rnnt", **options)
        self._stream_builder = stream_builder
        self.future_context_ms = future_context_ms

    def new_session(self) -> ASRSession:
        """Create independent decoder state for a new call."""
        stream_builder = self._stream_builder
        if stream_builder is None:
            try:
                from gigaam.streaming import GigaAMStreaming
            except ImportError as exc:
                raise RuntimeError(
                    "GigaAM is not installed. Install the project with `.[asr]`."
                ) from exc
            stream_builder = lambda model, context: GigaAMStreaming(
                model, future_context_ms=context, language="ru"
            )
        return ASRSession(stream_builder(self._model, self.future_context_ms))
