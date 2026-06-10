"""LLM adapter factory — provider-agnostic model selection.

Supports: openai, openrouter, ollama, llamacpp.
All providers are resolved through a single factory so the rest of the
codebase never imports provider-specific libraries directly.
"""

from enum import StrEnum
from typing import Any

from .config import Settings


class LLMProvider(StrEnum):
    """Supported LLM providers."""

    OPENAI = "openai"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"


def _resolve_base_url(config: Settings) -> str:
    """Resolve the effective base URL for LLM providers.

    Falls back from LLM_BASE_URL to OLLAMA_BASE_URL for ollama.
    """
    return config.llm_base_url or config.ollama_base_url


def build_llm(config: Settings, model_override: str = "") -> Any:
    """Build a LangChain chat model based on provider configuration.

    Args:
        config: Service settings carrying provider, model, and credentials.
        model_override: Optional model name to override the configured default.

    Returns:
        A LangChain chat model instance (ChatOpenAI, ChatOllama, …).

    Raises:
        ValueError: If the provider is unsupported or mis-configured.
    """
    provider = config.llm_provider
    model = model_override or config.llm_model or config.openai_model
    base_url = _resolve_base_url(config)

    if provider == LLMProvider.OLLAMA:
        from langchain_ollama import ChatOllama

        if not base_url:
            raise ValueError(
                "LLM_BASE_URL or OLLAMA_BASE_URL is required for ollama provider"
            )
        return ChatOllama(
            model=model,
            base_url=base_url,
            temperature=config.llm_temperature,
        )

    if provider in {LLMProvider.OPENAI, LLMProvider.OPENROUTER, LLMProvider.LLAMACPP}:
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": config.llm_temperature,
        }

        api_key = config.openai_api_key.get_secret_value()
        if api_key:
            kwargs["api_key"] = api_key
        elif provider == LLMProvider.LLAMACPP:
            # llama.cpp server does not require a real API key
            kwargs["api_key"] = "sk-no-key-required"

        if base_url:
            # Ensure OpenAI-compatible path prefix
            if not base_url.rstrip("/").endswith("/v1"):
                base_url = base_url.rstrip("/") + "/v1"
            kwargs["base_url"] = base_url

        return ChatOpenAI(**kwargs)

    raise ValueError(
        f"Unsupported LLM provider: {provider!r}. "
        f"Supported: {', '.join(p.value for p in LLMProvider)}"
    )


def validate_llm_config(config: Settings) -> None:
    """Validate that the LLM configuration is usable.

    Raises:
        ValueError: With a human-readable message if something is missing.
    """
    provider = config.llm_provider
    model = config.llm_model or config.openai_model
    base_url = _resolve_base_url(config)

    if not model:
        raise ValueError("LLM_MODEL (or OPENAI_MODEL) is required")

    if provider == LLMProvider.OLLAMA:
        if not base_url:
            raise ValueError(
                "LLM_BASE_URL or OLLAMA_BASE_URL is required for ollama provider"
            )
        return

    if provider == LLMProvider.LLAMACPP:
        if not base_url:
            raise ValueError("LLM_BASE_URL is required for llamacpp provider")
        return

    # openai, openrouter — API key required
    if not config.openai_api_key.get_secret_value():
        raise ValueError("OPENAI_API_KEY is required for openai/openrouter provider")
