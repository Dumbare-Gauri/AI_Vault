from vault_shared.ai_gateway.interfaces import CompletionResult, Message

_NO_CONTEXT_MESSAGE = (
    "I don't have any relevant documents to answer this from yet. Try "
    "rephrasing your question, or make sure the relevant files have been "
    "scanned and enriched first."
)


class ExtractiveCompletionProvider:
    """The AI Gateway's default completion adapter when no real LLM
    provider is configured — never invents an answer. It assembles a
    response directly from the retrieved context `ContextBuilderService`
    already ranked and deduplicated, clearly labeled (`provider`, in every
    persisted `ConversationMessage`) so it's never mistaken for actual LLM
    reasoning.

    This *is* the Handbook's "prefer deterministic retrieval, only invoke
    an LLM when reasoning is required" principle taken to its logical
    conclusion: reasoning genuinely requires a real provider, and none is
    configured yet (Phase 6 was built with the completion side stubbed —
    see ADR-018). Swapping this for a real provider (Claude, GPT, Gemini)
    is a new adapter class plus a factory change in `get_ai_gateway()`;
    nothing that calls `AIGateway.complete()` changes."""

    name = "extractive_fallback"
    model_name = "none"

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        if not context:
            text = _NO_CONTEXT_MESSAGE
        else:
            question = messages[-1].content if messages else ""
            text = (
                f"No AI model is configured yet, so here is the most relevant "
                f'retrieved content for "{question}":\n\n{context}'
            )

        # A rough stand-in for a token budget — this provider never calls a
        # real tokenizer, so it just bounds output length in characters.
        max_chars = max_tokens * 4
        return CompletionResult(
            text=text[:max_chars],
            provider=self.name,
            model_name="none",
            tokens_used=None,
        )
