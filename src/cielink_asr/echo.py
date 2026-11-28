"""Microphone test: GigaAM recognition followed by local speech synthesis."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import time
from math import ceil
from threading import Event, Lock
from typing import Any

from .llm import LLMError


class _InterruptiblePlayback:
    def __init__(self, np: Any) -> None:
        self._np = np
        self._frames: queue.Queue[Any] = queue.Queue()
        self._pending = np.empty(0, dtype="float32")
        self._lock = Lock()
        self.speaking = Event()
        self.events: queue.Queue[str] = queue.Queue()
        self._started_notified = False

    def play(self, audio: Any, frame_samples: int) -> None:
        with self._lock:
            for start in range(0, len(audio), frame_samples):
                frame = audio[start : start + frame_samples]
                if len(frame) < frame_samples:
                    frame = self._np.pad(frame, (0, frame_samples - len(frame)))
                self._frames.put(frame)
            self._started_notified = False
            self.speaking.set()

    def interrupt(self) -> None:
        with self._lock:
            was_speaking = self.speaking.is_set()
            with self._frames.mutex:
                self._frames.queue.clear()
            self._pending = self._np.empty(0, dtype="float32")
            self.speaking.clear()
            if was_speaking:
                self.events.put_nowait("interrupted")

    def render(self, outdata: Any, frames: int) -> None:
        with self._lock:
            written = 0
            while written < frames:
                if not len(self._pending):
                    try:
                        self._pending = self._frames.get_nowait()
                    except queue.Empty:
                        break
                count = min(frames - written, len(self._pending))
                outdata[written : written + count, 0] = self._pending[:count]
                self._pending = self._pending[count:]
                written += count
            if written and not self._started_notified:
                self._started_notified = True
                self.events.put_nowait("started")
            if written < frames:
                outdata[written:, 0] = 0
                if self._frames.empty():
                    if self.speaking.is_set():
                        self.speaking.clear()
                        if self._started_notified:
                            self.events.put_nowait("ended")


class _VoiceBackend:
    """Synchronous client for the training stand's consultation WebSocket."""

    def __init__(self, url: str) -> None:
        from websockets.sync.client import connect

        self._socket = connect(url, open_timeout=10)
        self._started_at = time.monotonic()
        self._socket.recv()  # ready

    def offset_ms(self) -> int:
        return round((time.monotonic() - self._started_at) * 1000)

    def send(self, event_type: str, **fields: Any) -> None:
        message = {"type": event_type, "offset_ms": self.offset_ms(), **fields}
        self._socket.send(json.dumps(message, ensure_ascii=False))

    def final(self, text: str) -> tuple[str, str]:
        self.send("final", text=text)
        while True:
            message = json.loads(self._socket.recv())
            if message.get("type") == "reply":
                return message["reply_id"], message["text"]
            if message.get("type") == "error":
                raise RuntimeError(message.get("message", "voice backend error"))

    def close(self) -> None:
        self._socket.close()


def _record_utterance(sample_rate: int) -> Any:
    import numpy as np
    import sounddevice as sd

    blocks = []
    callback_errors = []

    def capture(indata, frames, time_info, status):
        if status:
            callback_errors.append(str(status))
        blocks.append(indata[:, 0].copy())

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=5120,
        callback=capture,
    ):
        input("Recording. Press Enter to stop. ")

    if callback_errors:
        print("Microphone warning: " + "; ".join(callback_errors), file=sys.stderr)
    if not blocks:
        raise RuntimeError("No audio was captured from the microphone")
    return np.concatenate(blocks)


def _resample(audio: Any, source_rate: int, target_rate: int) -> Any:
    if source_rate == target_rate:
        return audio
    from math import gcd

    from scipy.signal import resample_poly

    common = gcd(source_rate, target_rate)
    return resample_poly(audio, target_rate // common, source_rate // common).astype(
        "float32"
    )


def _resolve_voice_url(
    voice_url: str | None, *, standalone: bool, env: dict[str, str] | None = None
) -> str | None:
    if voice_url:
        return voice_url
    if standalone:
        return None
    import os

    settings = os.environ if env is None else env
    return settings.get("CIELINK_VOICE_URL", "ws://127.0.0.1:8000/ws/voice")


def _run_live_conversation(
    *,
    sample_rate: int,
    engine: Any,
    synthesizer: Any,
    llm: Any,
    system_prompt: str,
    silence_ms: int,
    threshold: float,
    voice_url: str | None = None,
) -> None:
    import numpy as np
    import sounddevice as sd
    import torch

    frame_ms = 20
    frame_samples = round(sample_rate * frame_ms / 1000)
    samples_per_asr_chunk = 320 * 16
    silence_frames_required = ceil(silence_ms / frame_ms)
    input_frames: queue.Queue[Any] = queue.Queue(maxsize=256)
    dropped_input = Event()
    playback = _InterruptiblePlayback(np)
    backend = _VoiceBackend(voice_url) if voice_url else None
    active_reply_id: str | None = None
    history: list[dict[str, str]] = []
    consultation = None
    if not backend:
        from .addresses import AddressCatalog, AddressCatalogError
        from .consultation import Consultation

        try:
            consultation = Consultation.start(AddressCatalog.load())
            print("SkyNet address catalog: loaded")
        except AddressCatalogError as exc:
            print(f"SkyNet address catalog unavailable: {exc}", file=sys.stderr)

    def audio_callback(indata, outdata, frames, time_info, status):
        if status:
            print(f"Audio warning: {status}", file=sys.stderr)
        try:
            input_frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            dropped_input.set()

        playback.render(outdata, frames)

    def queue_speech(text: str) -> None:
        speech = synthesizer.synthesize(text)
        if isinstance(speech, torch.Tensor):
            speech = speech.detach().cpu().numpy()
        audio = np.asarray(speech, dtype="float32").reshape(-1)
        audio = _resample(audio, synthesizer.sample_rate, sample_rate)
        playback.play(audio, frame_samples)

    print("Live conversation. Speak to interrupt. Press Ctrl+C to quit.")
    try:
        with sd.Stream(
            samplerate=sample_rate,
            channels=(1, 1),
            dtype="float32",
            blocksize=frame_samples,
            callback=audio_callback,
        ):
            session = None
            pending_audio = np.empty(0, dtype="float32")
            active = False
            silence_frames = 0
            onset_frames: list[Any] = []

            while True:
                frame = input_frames.get()
                if dropped_input.is_set():
                    raise RuntimeError('Microphone frames lost during processing; incomplete audio must not be sent. Restart the live test.')
                while True:
                    try:
                        event = playback.events.get_nowait()
                    except queue.Empty:
                        break
                    if backend and active_reply_id:
                        backend.send(f"playback_{event}", reply_id=active_reply_id)
                        if event in ("ended", "interrupted"):
                            active_reply_id = None
                is_speech = float(np.sqrt(np.mean(np.square(frame)))) >= threshold

                if not active:
                    if playback.speaking.is_set() and is_speech:
                        playback.interrupt()
                        print("\nInterrupted.")
                        onset_frames = [frame]
                        active = True
                        session = engine.new_session()
                        silence_frames = 0
                    elif not playback.speaking.is_set() and is_speech:
                        onset_frames = [frame]
                        active = True
                        session = engine.new_session()
                        silence_frames = 0
                        print("You: ", end="", flush=True)
                    else:
                        continue

                if onset_frames:
                    onset_audio = np.concatenate(onset_frames)
                    pending_audio = np.concatenate(
                        (pending_audio, _resample(onset_audio, sample_rate, 16_000))
                    )
                    onset_frames.clear()
                else:
                    pending_audio = np.concatenate(
                        (pending_audio, _resample(frame, sample_rate, 16_000))
                    )

                while len(pending_audio) >= samples_per_asr_chunk:
                    chunk = pending_audio[:samples_per_asr_chunk]
                    pending_audio = pending_audio[samples_per_asr_chunk:]
                    update = session.feed(torch.from_numpy(chunk.astype("float32", copy=False)))
                    if update.e2e_delta:
                        print(update.e2e_delta, end="", flush=True)

                silence_frames = 0 if is_speech else silence_frames + 1
                if silence_frames < silence_frames_required:
                    continue

                if len(pending_audio):
                    session.feed(torch.from_numpy(pending_audio.astype("float32", copy=False)))
                    pending_audio = np.empty(0, dtype="float32")
                final = session.finish()
                if final.e2e_delta:
                    print(final.e2e_delta, end="", flush=True)
                transcript = final.e2e_text.strip()
                active = False
                session = None
                print()
                if not transcript:
                    continue

                if backend:
                    active_reply_id, reply = backend.final(transcript)
                else:
                    reply = transcript
                    llm_answered = False
                    if consultation:
                        turn = consultation.process(transcript)
                        reply = turn.reply or 'Могу уточнить адрес в справочнике. Цены и возможность подключения здесь не подтверждаются.'
                        llm_answered = bool(turn.reply)
                    if llm and not llm_answered and (
                        not consultation or not consultation.connection_requested
                    ):
                        try:
                            reply_system_prompt = system_prompt
                            if consultation and consultation.address:
                                reply_system_prompt += (
                                    " Текущее состояние CIELINK: "
                                    + consultation.context_text()
                                    + " Не повторяй уже найденный адрес без необходимости. "
                                    + "Не выдумывай условия или статус."
                                )
                            reply = llm.respond(
                                transcript,
                                system_prompt=reply_system_prompt,
                                history=history,
                            )
                        except LLMError:
                            reply = "Не удалось получить ответ сейчас. Повторите, пожалуйста, ещё раз."
                    history.extend(
                        [
                            {"role": "user", "content": transcript},
                            {"role": "assistant", "content": reply},
                        ]
                    )
                    del history[:-12]
                if llm or backend:
                    print(f"Assistant: {reply}")
                queue_speech(reply)
    finally:
        if backend:
            backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Speak into the microphone, then hear GigaAM + local TTS output"
    )
    parser.add_argument("--context-ms", type=int, choices=(160, 320, 640), default=320)
    parser.add_argument("--device", help="Torch device, such as cpu or cuda")
    parser.add_argument(
        "--tts-engine", choices=("omnivoice", "kokoro", "silero"), default="omnivoice",
        help="local speech synthesis engine",
    )
    parser.add_argument("--speaker", help="Kokoro-RU: sveta, masha, dima. Silero: baya, etc.")
    parser.add_argument(
        "--live", action="store_true", help="listen continuously and detect pauses"
    )
    parser.add_argument(
        "--silence-ms", type=int, default=800, help="pause that ends a live utterance"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.012,
        help="live voice activity RMS threshold",
    )
    parser.add_argument(
        "--voice-url",
        default=None,
        help="training stand WebSocket URL (default: CIELINK_VOICE_URL or localhost:8000)",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="answer with the CLI LLM instead of the consultation backend",
    )
    args = parser.parse_args()
    if args.silence_ms <= 0:
        parser.error("--silence-ms must be positive")
    if args.threshold <= 0:
        parser.error("--threshold must be positive")
    args.voice_url = _resolve_voice_url(
        args.voice_url, standalone=args.standalone or not args.live
    )

    try:
        import numpy as np
        import sounddevice as sd
        import torch

        from .streaming import GigaAMEngine
        from .tts import KokoroTTS, OmniVoiceTTS, SileroTTS
        from .llm import (
            CONNECTION_ASSISTANT_SYSTEM_PROMPT,
            ChatClient,
            LLMError,
            VOICE_TEST_SYSTEM_PROMPT,
        )
    except ImportError as exc:
        print(
            "Install dependencies with `pip install -e '.[asr,voice-test]'`.",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 2

    input_device = sd.query_devices(kind="input")
    source_rate = int(input_device["default_samplerate"])
    target_rate = 16_000
    engine = GigaAMEngine(future_context_ms=args.context_ms, device=args.device)
    if args.tts_engine == "omnivoice":
        synthesizer = OmniVoiceTTS(device=args.device or "cuda")
    elif args.tts_engine == "kokoro":
        synthesizer = KokoroTTS(speaker=args.speaker or "sveta")
    else:
        synthesizer = SileroTTS(speaker=args.speaker or "baya")
    try:
        llm = ChatClient.from_env()
    except ValueError as exc:
        print(f"LLM configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"Microphone rate: {source_rate} Hz. ASR rate: {target_rate} Hz.")
    print(f"TTS: {args.tts_engine} / {synthesizer.speaker}")
    if args.voice_url:
        print(f"Consultation backend: {args.voice_url}")
        print("LLM: consultation backend (configured by the API server)")
    else:
        print(f"LLM: {llm.provider} / {llm.model}" if llm else "LLM: off (repeat transcript)")
    if args.live:
        print(
            f"Live mode: pauses end phrases after {args.silence_ms} ms; "
            f"RMS threshold {args.threshold:g}."
        )
    else:
        print("Press Enter to record a phrase, then Enter again to stop. Type q to quit.")

    while True:
        if args.live:
            try:
                _run_live_conversation(
                    sample_rate=source_rate,
                    engine=engine,
                    synthesizer=synthesizer,
                    llm=llm,
                    system_prompt=(
                        CONNECTION_ASSISTANT_SYSTEM_PROMPT
                        if args.standalone
                        else VOICE_TEST_SYSTEM_PROMPT
                    ),
                    silence_ms=args.silence_ms,
                    threshold=args.threshold,
                    voice_url=args.voice_url,
                )
            except (KeyboardInterrupt, EOFError):
                print()
                return 0
            except Exception as exc:
                print(f"Test failed: {exc}", file=sys.stderr)
                return 1

        if not args.live:
            command = input("\nReady> ").strip().lower()
            if command == "q":
                return 0

        try:
            audio = _record_utterance(source_rate)
            audio = _resample(audio, source_rate, target_rate)
            session = engine.new_session()
            chunk_samples = 320 * 16
            for start in range(0, len(audio), chunk_samples):
                session.feed(torch.from_numpy(audio[start : start + chunk_samples]))
            transcript = session.finish().e2e_text.strip()
            if not transcript:
                print("No speech was recognized.")
                continue

            print(f"Recognized: {transcript}")
            reply = (
                llm.respond(transcript, system_prompt=VOICE_TEST_SYSTEM_PROMPT)
                if llm
                else transcript
            )
            if llm:
                print(f"Assistant: {reply}")
            speech = synthesizer.synthesize(reply)
            if isinstance(speech, torch.Tensor):
                speech = speech.detach().cpu().numpy()
            sd.play(np.asarray(speech, dtype="float32"), synthesizer.sample_rate)
            sd.wait()
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
        except Exception as exc:
            print(f"Test failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
