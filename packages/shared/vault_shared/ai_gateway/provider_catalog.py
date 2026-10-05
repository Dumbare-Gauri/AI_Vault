"""The AI providers an organization may choose. Each maps to a fixed endpoint
— an organization picks a provider and supplies a key, never a URL, so a
tenant can never point the server at an arbitrary host. Business logic never
names a provider; it only ever holds a `CompletionProvider`."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AIProviderSpec:
    id: str
    label: str
    protocol: str  # "openai_chat" | "anthropic_messages"
    base_url: str
    model_hint: str


AI_PROVIDERS: dict[str, AIProviderSpec] = {
    spec.id: spec
    for spec in (
        AIProviderSpec(
            id="openrouter",
            label="OpenRouter",
            protocol="openai_chat",
            base_url="https://openrouter.ai/api/v1",
            model_hint="z-ai/glm-4.6",
        ),
        AIProviderSpec(
            id="openai",
            label="OpenAI",
            protocol="openai_chat",
            base_url="https://api.openai.com/v1",
            model_hint="gpt-4.1-mini",
        ),
        AIProviderSpec(
            id="anthropic",
            label="Anthropic",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            model_hint="claude-sonnet-4-5",
        ),
        AIProviderSpec(
            id="gemini",
            label="Google Gemini",
            protocol="openai_chat",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model_hint="gemini-2.5-flash",
        ),
    )
}
DEFAULT_AI_PROVIDER = "openrouter"
