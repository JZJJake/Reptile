"""DeepSeek API client (OpenAI-compatible) with streaming support."""

import os
import json
from typing import AsyncGenerator, Optional
import httpx

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# ── Hybrid model strategy (cost/quality tiering) ──────────────────────────────
# This assignment is deliberate and must NOT be reversed. The reasoning:
#
#   Knowledge-base construction is where errors COMPOUND. A wrong entity merge,
#   a dropped fact, or a fabricated relation written during distillation/assembly
#   becomes PERMANENT: it is read back as "existing network" by every later
#   assembly batch and surfaces in every future answer. Construction is also the
#   hardest reasoning in the system (entity resolution, relation extraction,
#   contradiction detection, cross-doc synthesis) and is paid ONCE per document.
#   → Construction (Stage 1 distil / Stage 2 assembly / Stage 3 shard) = v4-pro.
#
#   Interactive answers are EPHEMERAL: not stored back into the KB, grounded by
#   already-curated pages (the hard reasoning is baked in), and the workload is
#   high-volume + latency-sensitive (the user waits on a stream). The model mostly
#   reads curated pages and phrases them well — a lighter task.
#   → Q&A / page-selection = v4-flash (speed/cost first).
#
# A fast answer over a well-built KB beats a smart answer over a fragmented one:
# garbage in stays garbage. So build quality is non-negotiable; query is the place
# to economise. The ONE exception is genuine query-time synthesis ("connect these
# clues into a new logic chain") — for that, callers pass deep=True to escalate the
# *answer* step only to REASON_MODEL, while page-selection stays on the cheap tier.
# Change models by editing these constants only.
BUILD_MODEL = "deepseek-v4-pro"
QUERY_MODEL = "deepseek-v4-flash"
# Deep query-time reasoning (clue-connecting / new logic chains): same tier as
# construction. Opt-in per query via deep=True so routine Q&A stays cheap.
REASON_MODEL = BUILD_MODEL
# Backwards-compatible default = query tier (matches the iOS port's defaultModel).
DEFAULT_MODEL = QUERY_MODEL
TIMEOUT = 180.0


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
