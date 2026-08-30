"""Fixed model metadata that FlowWeave must expose before the first request.

The values below are read from the LiteLLM catalog bundled in the pinned
OpenHands 1.44.0 Runtime image.  They are intentionally limited to the Codex
OAuth models that FlowWeave exposes through that image; other providers keep
an unknown window until OpenHands reports one formally.
"""

_CODEX_CONTEXT_WINDOWS: dict[str, int] = {
    "openai/gpt-5.4": 1_050_000,
    "openai/gpt-5.4-mini": 272_000,
    "openai/gpt-5.5": 1_050_000,
    "openai/gpt-5.6": 922_000,
    "openai/gpt-5.6-luna": 922_000,
    "openai/gpt-5.6-sol": 922_000,
    "openai/gpt-5.6-terra": 922_000,
}


def declared_context_window(model_name: str) -> int | None:
    """Return the pinned Runtime's declared input window for a catalog model."""

    normalized = model_name if "/" in model_name else f"openai/{model_name}"
    return _CODEX_CONTEXT_WINDOWS.get(normalized)


__all__ = ("declared_context_window",)
