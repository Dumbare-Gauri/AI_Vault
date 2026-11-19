"""Retrieved `context` is untrusted by definition (it is text from users'
files). The provider must deliver it inside an `<untrusted_data>` wrapper in
the final user turn — never as an additional system message, where a file
saying "ignore previous instructions" would carry system-level weight."""

from unittest.mock import MagicMock, patch

from vault_shared.ai_gateway.interfaces import Message
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)


def _response(json_body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = json_body
    return response


def _provider() -> OpenAICompatibleCompletionProvider:
    return OpenAICompatibleCompletionProvider(
        base_url="https://example.test/v1",
        api_key="test-key",
        model_name="test-model",
        timeout_seconds=60,
    )


def _sent_messages(messages: list[Message], context: str | None) -> list[dict]:
    body = {"choices": [{"message": {"content": "ok"}}]}
    with patch("requests.post", return_value=_response(body)) as mock_post:
        _provider().complete(messages=messages, context=context, max_tokens=100)
    return mock_post.call_args.kwargs["json"]["messages"]


def test_context_never_becomes_a_system_message() -> None:
    sent = _sent_messages(
        [Message(role="system", content="PERSONA"), Message(role="user", content="question")],
        context="FILE TEXT",
    )

    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"] == "PERSONA"
    assert "FILE TEXT" not in sent[0]["content"]


def test_context_is_appended_to_the_last_user_turn_inside_the_untrusted_wrapper() -> None:
    sent = _sent_messages(
        [Message(role="system", content="PERSONA"), Message(role="user", content="question")],
        context="FILE TEXT",
    )

    user_content = sent[-1]["content"]
    assert user_content.startswith("question")
    assert '<untrusted_data ref="context">' in user_content
    assert "FILE TEXT" in user_content
    assert user_content.rstrip().endswith("</untrusted_data>")


def test_context_is_attached_to_the_latest_user_turn_not_earlier_history() -> None:
    sent = _sent_messages(
        [
            Message(role="user", content="first question"),
            Message(role="assistant", content="first answer"),
            Message(role="user", content="second question"),
        ],
        context="FILE TEXT",
    )

    assert sent[0]["content"] == "first question"
    assert "FILE TEXT" in sent[2]["content"]


def test_context_with_no_user_message_becomes_a_user_message() -> None:
    sent = _sent_messages([Message(role="system", content="PERSONA")], context="FILE TEXT")

    assert [m["role"] for m in sent] == ["system", "user"]
    assert "FILE TEXT" in sent[1]["content"]


def test_no_context_leaves_messages_untouched() -> None:
    sent = _sent_messages(
        [Message(role="system", content="PERSONA"), Message(role="user", content="question")],
        context=None,
    )

    assert sent == [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "question"},
    ]
