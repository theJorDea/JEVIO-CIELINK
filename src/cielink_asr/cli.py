"""Command-line smoke test for GigaAM-v4 on an audio file."""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe a WAV file with GigaAM-v4")
    parser.add_argument("audio", help="Input audio file supported by ffmpeg")
    parser.add_argument("--context-ms", type=int, choices=(160, 320, 640), default=320)
    parser.add_argument("--device", help="Torch device, such as cpu or cuda")
    args = parser.parse_args()

    try:
        import gigaam
        import torch

        from .streaming import GigaAMEngine
    except ImportError as exc:
        print(
            "Install CIELINK ASR dependencies with `pip install -e '.[asr]'`.",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 2

    waveform = gigaam.load_audio(args.audio)
    engine = GigaAMEngine(future_context_ms=args.context_ms, device=args.device)
    session = engine.new_session()
    chunk_samples = 320 * 16

    for start in range(0, waveform.numel(), chunk_samples):
        chunk = waveform[start : start + chunk_samples].to(dtype=torch.float32)
        update = session.feed(chunk)
        if update.e2e_delta:
            print(update.e2e_delta, end="", flush=True)

    final = session.finish()
    if final.e2e_delta:
        print(final.e2e_delta, end="", flush=True)
    print()
    print(f"\nRAW: {final.raw_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
