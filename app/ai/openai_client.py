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
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    try:
        async with httpx.AsyncClient(timeout=settings.openai_timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.openai_api_key}"},
            )
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
