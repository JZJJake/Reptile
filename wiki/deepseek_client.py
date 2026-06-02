"""DeepSeek API client (OpenAI-compatible) with streaming support."""

import os
import json
from typing import AsyncGenerator, Optional
import httpx

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
TIMEOUT = 120.0


async def chat_completion(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    stream: bool = False,
    api_key: Optional[str] = None,
    temperature: float = 0.7,
) -> str | AsyncGenerator[str, None]:
    """
    Call DeepSeek chat completions endpoint.
    stream=True returns an async generator yielding text deltas.
    stream=False returns the full response string.
    API key from DEEPSEEK_API_KEY env var if not passed.
    """
    key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise ValueError("DEEPSEEK_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }

    if stream:
        return _stream_response(headers, payload)
    else:
        return await _full_response(headers, payload)


async def _full_response(headers: dict, payload: dict) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json={**payload, "stream": False},
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


async def _stream_response(headers: dict, payload: dict) -> AsyncGenerator[str, None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json={**payload, "stream": True},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    delta = data["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
