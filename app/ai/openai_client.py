"""Port of app/routes/chat.jsx's callOpenAIOnce -- a raw HTTP call to the OpenAI chat completions
API (no SDK, matching the JS original), with a hard timeout so a hung request never leaves the
customer's chat bubble waiting indefinitely.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def call_openai_once(messages: list[dict], tools: list[dict] | None, tool_choice: dict | str | None = None) -> dict | None:
    payload = {
        "model": settings.openai_model,
        "messages": messages,
        "temperature": settings.openai_temperature,
        # Phase 2 (F6): every completion has a hard output ceiling.
        "max_tokens": settings.openai_max_output_tokens,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.openai_api_key}"}

    try:
        async with httpx.AsyncClient(timeout=settings.openai_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            # Some models (reasoning-tier ones in particular) reject parameters this codebase
            # always used to be able to send unconditionally -- correct the payload from the
            # error and retry rather than hardcoding a model-name allowlist, so this keeps working
            # correctly as OPENAI_MODEL changes. Bounded to a couple of corrections; a genuinely
            # unrelated 400 still surfaces immediately below.
            for _ in range(3):
                if response.status_code != 400:
                    break
                error_text = response.text
                if "temperature" in payload and "temperature" in error_text and "does not support" in error_text:
                    payload.pop("temperature", None)
                elif "max_tokens" in payload and "max_tokens" in error_text and "max_completion_tokens" in error_text:
                    # Newer models take the same ceiling under a different name.
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                elif "reasoning_effort" in error_text and payload.get("reasoning_effort") != "none":
                    payload["reasoning_effort"] = "none"
                else:
                    break
                response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            logger.error("OpenAI API error: %s %s", response.status_code, response.text)
            return None
        return response.json()
    except httpx.TimeoutException:
        logger.error("OpenAI request failed: timed out after %ss", settings.openai_timeout_seconds)
        return None
    except Exception as err:
        logger.error("OpenAI request failed: %s", err)
        return None
