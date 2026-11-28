"""Small synchronous chat clients for local Ollama and OpenAI-compatible APIs."""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    """Raised when an LLM backend is unavailable or returns an invalid reply."""


class ChatClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 45.0,
        keep_alive: str = "10m",
        context_length: int = 4096,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if provider not in {"ollama", "openai-compatible"}:
            raise ValueError("provider must be ollama or openai-compatible")
        if not model.strip():
            raise ValueError("model must not be empty")
        if provider == "openai-compatible" and not api_key:
            raise ValueError("Set CIELINK_LLM_API_KEY or OPENAI_API_KEY")

        default_url = (
            "http://localhost:11434/api/chat"
            if provider == "ollama"
            else "https://api.openai.com/v1/chat/completions"
        )
        self.provider = provider
        self.model = model
        self.url = (base_url or default_url).rstrip("/")
        if provider == "openai-compatible" and not self.url.endswith("/chat/completions"):
            self.url += "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.context_length = context_length
        self._opener = opener or urlopen

    @classmethod
    def from_env(cls, *, opener: Callable[..., Any] | None = None) -> ChatClient | None:
        provider = os.getenv("CIELINK_LLM_PROVIDER", "none").strip().lower()
        if provider == "none":
            return None
        if provider not in {"ollama", "openai-compatible"}:
            raise ValueError(
                "CIELINK_LLM_PROVIDER must be none, ollama, or openai-compatible"
            )
        default_model = "qwen3:4b" if provider == "ollama" else "gpt-4o-mini"
        model = os.getenv("CIELINK_LLM_MODEL", default_model)
        timeout = float(os.getenv("CIELINK_LLM_TIMEOUT", "45"))
        return cls(
            provider=provider,
            model=model,
            base_url=os.getenv("CIELINK_LLM_BASE_URL") or None,
            api_key=os.getenv("CIELINK_LLM_API_KEY") or (
                os.getenv("OPENAI_API_KEY") if provider == "openai-compatible" and not os.getenv("CIELINK_LLM_BASE_URL") else None
            ),
            timeout=timeout,
            keep_alive=os.getenv("CIELINK_LLM_KEEP_ALIVE", "10m"),
            context_length=int(os.getenv("CIELINK_LLM_CONTEXT", "4096")),
            opener=opener,
        )

    def respond(
        self,
        text: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        if not text.strip():
            raise ValueError("User text must not be empty")
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": text})
        if self.provider == "ollama":
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "keep_alive": self.keep_alive,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 120,
                    "num_ctx": self.context_length,
                },
            }
        else:
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.4,
                "max_tokens": 120,
                "stream": False,
            }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise LLMError(f"LLM request failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Cannot reach {self.provider} at {self.url}: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LLMError("LLM returned invalid JSON") from exc

        try:
            if self.provider == "ollama":
                content = result["message"]["content"]
            else:
                content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response does not contain assistant text") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM returned an empty assistant response")
        return content.strip()


VOICE_TEST_SYSTEM_PROMPT = (
    "Ты голосовой помощник CIELINK, который ведёт обычный разговор на русском языке. "
    "Отвечай по смыслу последней реплики пользователя, коротко и естественно, "
    "обычно одним предложением. Не начинай каждый ответ с приветствия и не "
    "переводи тему на услуги провайдера, если пользователь об этом не спросил. "
    "На приветствие поздоровайся. На грубость спокойно ответь без нотаций. "
    "Если пользователь сообщает факт или мнение, отреагируй именно на него. "
    "Если спрашивают, кто ты, ответь, что ты голосовой помощник CIELINK. "
    "Не называй себя большой языковой моделью, моделью Google или OpenAI и "
    "не рассказывай о создателе или технической реализации. Не выдумывай "
    "сведения и не приписывай человеку неупомянутые намерения."
)


CONNECTION_ASSISTANT_SYSTEM_PROMPT = (
    "Ты голосовой помощник провайдера SkyNet. Веди живой, уважительный и короткий "
    "разговор о подключении интернета. Если клиент хочет подключение, выясни, "
    "что подключаем: квартиру, частный дом или бизнес/юридическое лицо. "
    "Если тип ещё не назван, задай этот вопрос. Не угадывай тип по адресу. "
    "При необходимости уточни улицу, "
    "номер дома и корпус. Не утверждай, что подключение доступно, по одному "
    "лишь совпадению адреса с публичным каталогом: статусы каталога требуют "
    "трактовки и могут быть устаревшими. Отсутствие адреса в каталоге означает "
    "только отсутствие сведений. Не создавай заявку и не обещай обратный звонок "
    "без явного согласия и подтверждённой бизнес-операции. Для бизнеса обозначь, "
    "что условия нужно уточнять по отдельному сценарию. Не выдумывай тарифы, "
    "покрытие, цены или результат проверки."
)
