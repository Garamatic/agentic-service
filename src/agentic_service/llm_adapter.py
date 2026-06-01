"""LLM adapter factory — provider-agnostic model selection.

Supports: openai, openrouter, ollama, llamacpp.
All providers are resolved through a single factory so the rest of the
codebase never imports provider-specific libraries directly.
"""

from enum import Enum
from typing import Any

from .config import Settings


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"


def build_llm(config: Settings) -> Any:
    """Build a LangChain chat model based on provider configuration.

    Args:
        config: Service settings carrying provider, model, and credentials.

    Returns:
        A LangChain chat model instance (ChatOpenAI, ChatOllama, …).

    Raises:
        ValueError: If the provider is unsupported or mis-configured.
    """
    provider = config.llm_provider
    model = config.llm_model or config.openai_model

    if provider == LLMProvider.OLLAMA:
        from langchain_ollama import ChatOllama

        if not config.llm_base_url:
            raise ValueError(
                "LLM_BASE_URL (or OLLAMA_BASE_URL) is required for ollama provider"
            )
        return ChatOllama(
            model=model,
            base_url=config.llm_base_url,
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

        if config.llm_base_url:
            kwargs["base_url"] = config.llm_base_url

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

    if not model:
        raise ValueError("LLM_MODEL (or OPENAI_MODEL) is required")

    if provider == LLMProvider.OLLAMA:
        if not config.llm_base_url:
            raise ValueError(
                "LLM_BASE_URL (or OLLAMA_BASE_URL) is required for ollama provider"
            )
        return

    if provider == LLMProvider.LLAMACPP:
        if not config.llm_base_url:
            raise ValueError("LLM_BASE_URL is required for llamacpp provider")
        return

    # openai, openrouter — API key required
    if not config.openai_api_key.get_secret_value():
        raise ValueError("OPENAI_API_KEY is required for openai/openrouter provider")
