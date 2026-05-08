"""OpenAI-compatible async chat client.

Endpoint-agnostic: works against vLLM, Ollama, llama.cpp server, OpenAI, or
any service exposing /v1/chat/completions with the standard schema.
"""
from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx

from .models import EndpointConfig


class ChatClient:
    def __init__(self, config: EndpointConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None
        # Aggregated token usage across all chat() calls in this session.
        # Surfaces in Report.metadata so the privacy claim is measurable.
        self.prompt_tokens_total = 0
        self.completion_tokens_total = 0
        self.calls = 0

    async def __aenter__(self) -> Self:
        headers: dict[str, str] = {}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url.rstrip("/"),
            timeout=self._config.timeout,
            headers=headers,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    async def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.6,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        if self._client is None:
            raise RuntimeError(
                "ChatClient must be used as an async context manager: "
                "`async with ChatClient(config) as client:`"
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage") or {}
                self.prompt_tokens_total += int(usage.get("prompt_tokens", 0) or 0)
                self.completion_tokens_total += int(
                    usage.get("completion_tokens", 0) or 0
                )
                self.calls += 1
                return data["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as e:
                # 4xx — caller error (auth, model not found). Don't retry.
                if 400 <= e.response.status_code < 500:
                    raise
                last_exc = e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e

            if attempt < self._config.max_retries:
                await asyncio.sleep(2**attempt)

        assert last_exc is not None
        raise last_exc
