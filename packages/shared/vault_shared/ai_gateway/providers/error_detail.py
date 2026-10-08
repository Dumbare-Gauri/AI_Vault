from typing import Any

_MAX_DETAIL_CHARS = 200


def provider_error_detail(response: Any) -> str | None:
    """The provider's own explanation of a refused request — OpenAI-style,
    OpenRouter and Anthropic APIs all answer `{"error": {"message": ...}}` —
    so the user sees "not a valid model ID" rather than a bare status code."""
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str) or not message.strip():
        return None
    message = " ".join(message.split())
    if len(message) > _MAX_DETAIL_CHARS:
        message = message[: _MAX_DETAIL_CHARS - 1] + "…"
    return message


def rejected_message(status: int, response: Any) -> str:
    detail = provider_error_detail(response)
    base = f"The AI provider rejected the request ({status})"
    return f"{base}: {detail}" if detail else f"{base}."
