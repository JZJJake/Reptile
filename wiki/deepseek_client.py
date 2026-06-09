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
    timeout: Optional[float] = None,
) -> str | AsyncGenerator[str, None]:
    """
    Call DeepSeek chat completions endpoint.
    stream=True returns an async generator yielding text deltas.
    stream=False returns the full response string.
    API key from DEEPSEEK_API_KEY env var if not passed.
    `timeout` overrides the default per call (large assembly calls need longer).
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
    eff_timeout = timeout or TIMEOUT

    if stream:
        return _stream_response(headers, payload, eff_timeout)
    else:
        return await _full_response(headers, payload, eff_timeout)


async def _full_response(headers: dict, payload: dict, timeout: float = TIMEOUT) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json={**payload, "stream": False},
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


async def _stream_response(headers: dict, payload: dict,
                           timeout: float = TIMEOUT) -> AsyncGenerator[str, None]:
    async with httpx.AsyncClient(timeout=timeout) as client:
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
                except json.JSONDecodeError:
                    continue
                if "error" in data:
                    # DeepSeek sends error-shaped SSE events mid-stream for
                    # rate limiting / content filtering instead of raising at
                    # connect time — surface it instead of dropping it silently.
                    err = data["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"DeepSeek 流式响应错误: {msg}")
                try:
                    delta = data["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (KeyError, IndexError):
                    continue
