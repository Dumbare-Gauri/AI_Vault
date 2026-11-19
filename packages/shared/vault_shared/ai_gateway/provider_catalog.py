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
        AIProviderSpec(
            id="ollama",
            label="Ollama Cloud",
            protocol="openai_chat",
            base_url="https://ollama.com/v1",
            model_hint="gpt-oss:120b",
        ),
        AIProviderSpec(
            id="zai",
            label="Z.ai (GLM)",
            protocol="openai_chat",
            base_url="https://api.z.ai/api/paas/v4",
            model_hint="glm-4.6",
        ),
        AIProviderSpec(
            id="deepseek",
            label="DeepSeek",
            protocol="openai_chat",
            base_url="https://api.deepseek.com/v1",
            model_hint="deepseek-chat",
        ),
        AIProviderSpec(
            id="groq",
            label="Groq",
            protocol="openai_chat",
            base_url="https://api.groq.com/openai/v1",
            model_hint="llama-3.3-70b-versatile",
        ),
        AIProviderSpec(
            id="mistral",
            label="Mistral",
            protocol="openai_chat",
            base_url="https://api.mistral.ai/v1",
            model_hint="mistral-small-latest",
        ),
        AIProviderSpec(
            id="xai",
            label="xAI (Grok)",
            protocol="openai_chat",
            base_url="https://api.x.ai/v1",
            model_hint="grok-4",
        ),
    )
}
DEFAULT_AI_PROVIDER = "openrouter"
