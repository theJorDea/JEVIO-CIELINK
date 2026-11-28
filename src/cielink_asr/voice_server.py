"""Local WebSocket consultation server used by ``cielink-echo --live``."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from .addresses import AddressCatalog, AddressCatalogError
from .consultation import Consultation
from .llm import CONNECTION_ASSISTANT_SYSTEM_PROMPT, ChatClient, LLMError


class VoiceSession:
    def __init__(self, catalog: AddressCatalog, llm: Any = None) -> None:
        self.consultation = Consultation.start(catalog)
        self.llm = llm
        self.history: list[dict[str, str]] = []

    def reply(self, transcript: str) -> str:
        turn = self.consultation.process(transcript)
        if turn.reply:
            answer = turn.reply
        elif self.llm:
            try:
                answer = self.llm.respond(
                    transcript,
                    system_prompt=(
                        CONNECTION_ASSISTANT_SYSTEM_PROMPT
                        + " Текущее состояние: "
                        + self.consultation.context_text()
                        + " Не повторяй уже найденный адрес без необходимости. "
                        + "Не выдумывай цены и покрытие."
                    ),
                    history=self.history,
                )
            except LLMError:
                answer = "Не удалось получить ответ сейчас. Повторите, пожалуйста, ещё раз."
        else:
            answer = transcript
        self.history.extend(
            [{"role": "user", "content": transcript}, {"role": "assistant", "content": answer}]
        )
        del self.history[:-12]
        return answer


async def _handle(websocket: Any, catalog: AddressCatalog, llm: Any) -> None:
    session = VoiceSession(catalog, llm)
    await websocket.send(
        json.dumps(
            {
                "type": "ready",
                "session_id": uuid.uuid4().hex,
                "config": {"catalog_url": catalog.source_url},
            },
            ensure_ascii=False,
        )
    )
    async for raw in websocket:
        try:
            event = json.loads(raw)
            if event.get("type") != "final":
                continue
            transcript = str(event.get("text", "")).strip()
            if not transcript:
                continue
            answer = await asyncio.to_thread(session.reply, transcript)
            await websocket.send(
                json.dumps(
                    {
                        "type": "reply",
                        "reply_id": uuid.uuid4().hex,
                        "text": answer,
                    },
                    ensure_ascii=False,
                )
            )
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            await websocket.send(
                json.dumps({"type": "error", "message": f"invalid voice event: {exc}"})
            )


async def _serve(host: str, port: int, catalog: AddressCatalog, llm: Any) -> None:
    try:
        from websockets.asyncio.server import serve
    except ImportError:
        from websockets.server import serve

    async with serve(
        lambda websocket: _handle(websocket, catalog, llm), host, port
    ):
        print(f"CIELINK voice server: ws://{host}:{port}/ws/voice")
        await asyncio.Future()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local CIELINK voice consultation server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        catalog = AddressCatalog.load()
    except AddressCatalogError as exc:
        print(f"Cannot load SkyNet address catalog: {exc}")
        return 1
    try:
        llm = ChatClient.from_env()
    except ValueError as exc:
        print(f"LLM configuration error: {exc}")
        return 2
    try:
        asyncio.run(_serve(args.host, args.port, catalog, llm))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
