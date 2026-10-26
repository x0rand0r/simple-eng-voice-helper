"""OpenAI calls: speech-to-text and streamed chat completions."""
from __future__ import annotations

import re
from typing import AsyncIterator

import openai

from .config import Settings

TIMEOUT_SEC = 30.0


def _model_params(model: str) -> dict:
    """Latency-friendly defaults: no hidden reasoning where the model allows turning it off."""
    m = model.lower()
    if re.match(r"gpt-5(-|$)", m):
        return {"reasoning_effort": "minimal"}
    if re.match(r"gpt-(5\.\d|6)", m) and "chat" not in m:
        return {"reasoning_effort": "none"}
    if re.match(r"o\d", m):
        return {"reasoning_effort": "low"}
    return {"temperature": 0.3}


class MissingKeyError(RuntimeError):
    pass


def friendly_error(e: Exception) -> str:
    if isinstance(e, MissingKeyError):
        return "Укажите API ключ OpenAI в настройках"
    if isinstance(e, openai.AuthenticationError):
        return "Неверный API ключ OpenAI"
    if isinstance(e, openai.PermissionDeniedError):
        return "Доступ к OpenAI запрещён (регион не поддерживается? нужен VPN или прокси в Base URL)"
    if isinstance(e, openai.RateLimitError):
        return "Лимит запросов OpenAI или закончились средства на балансе"
    if isinstance(e, openai.NotFoundError):
        return f"Модель не найдена или недоступна: {getattr(e, 'message', e)}"
    if isinstance(e, openai.APITimeoutError):
        return "OpenAI не ответил вовремя"
    if isinstance(e, openai.APIConnectionError):
        return "Нет соединения с OpenAI (интернет, VPN, прокси?)"
    if isinstance(e, openai.APIStatusError):
        return f"Ошибка OpenAI {e.status_code}: {getattr(e, 'message', e)}"
    return f"{type(e).__name__}: {e}"


class AI:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: openai.AsyncOpenAI | None = None
        self._client_key: tuple[str, str] | None = None
        self._unsupported: dict[str, set[str]] = {}   # model -> params the API rejected

    def client(self) -> openai.AsyncOpenAI:
        key = (self.settings.api_key(), self.settings["base_url"])
        if self._client is None or key != self._client_key:
            if not key[0]:
                raise MissingKeyError("API key is not set")
            self._client = openai.AsyncOpenAI(api_key=key[0], base_url=key[1] or None,
                                              timeout=TIMEOUT_SEC, max_retries=1)
            self._client_key = key
        return self._client

    async def check(self) -> None:
        await self.client().models.list()

    async def transcribe(self, wav: bytes, prompt: str) -> str:
        model = self.settings["stt_model"]
        result = await self.client().audio.transcriptions.create(
            model=model, file=("speech.wav", wav, "audio/wav"), prompt=prompt)
        return result if isinstance(result, str) else (result.text or "")

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        model = self.settings["llm_model"]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        while True:
            params = {k: v for k, v in _model_params(model).items()
                      if k not in self._unsupported.get(model, set())}
            try:
                response = await self.client().chat.completions.create(
                    model=model, messages=messages, stream=True, **params)
                break
            except openai.BadRequestError as e:
                # Different model families accept different knobs; drop the rejected one and retry.
                rejected = next((p for p in params if p in str(e)), None)
                if rejected is None:
                    raise
                self._unsupported.setdefault(model, set()).add(rejected)
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
