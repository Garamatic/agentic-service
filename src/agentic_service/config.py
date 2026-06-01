"""Configuration management for agentic service."""

from functools import cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration from environment."""

    # API URLs
    ticket_api_url: str = "http://localhost:5000"
    odoo_bridge_url: str = "http://localhost:5001"

    # API Keys
    ticket_api_key: str = ""
    odoo_bridge_api_key: str = ""
    api_key: SecretStr = SecretStr("")

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 3001

    # CORS
    cors_origins: str = "*"  # comma-separated or "*"

    # RabbitMQ
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_pass: str = "guest"
    rabbitmq_vhost: str = "/"

    @property
    def rabbitmq_url(self) -> str:
        return f"amqp://{self.rabbitmq_user}:{self.rabbitmq_pass}@{self.rabbitmq_host}:{self.rabbitmq_port}{self.rabbitmq_vhost}"

    # Logging
    log_level: str = "INFO"

    # LLM Provider (openai, openrouter, ollama, llamacpp)
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.1

    # Backward compatibility — used when llm_model / llm_base_url are empty
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"

    # Approval threshold for invoices requiring human approval
    approval_threshold: float = 500.0

    # MCP server transport
    mcp_transport: str = "sse"

    # Agent persistence
    agent_db_path: str = ""

    # Mail defaults
    mail_from_email: str = "support@garamatic.com"
    mail_from_name: str = "Garamatic Support"
    mail_billing_from_email: str = "billing@garamatic.com"
    mail_billing_from_name: str = "Garamatic Billing"
    mail_system_from_email: str = "system@garamatic.com"
    mail_system_from_name: str = "Garamatic System"

    @property
    def cors_origin_list(self) -> list[str]:
        """Get the CORS origin list."""
        origins = self.cors_origins.strip()
        if origins == "*" or not origins:
            return ["*"]
        return [o.strip() for o in origins.split(",") if o.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


@cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
