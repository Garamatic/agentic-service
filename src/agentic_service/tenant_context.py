"""Flexible, extensible tenant context management for Garamatic.

Core responsibilities:
- Load tenant configs from storage (files, env, database)
- Cache and serve TenantContext instances
- Support composition, chaining, and per-request overrides
- Enable multiple tenants in single request (cross-tenant workflows)
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

# Safe tenant identifier: alphanumeric, hyphens, underscores only
_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# ------ Storage Backend Protocol (extensibility point) ------


class TenantConfigBackend(ABC):
    """Pluggable storage for tenant configs. Support files, env vars, DB."""

    @abstractmethod
    def get_config(self, tenant_id: str) -> dict[str, Any]:
        """Load config for a single tenant. Raise KeyError if not found."""
        pass

    @abstractmethod
    def list_tenant_ids(self) -> list[str]:
        """List all available tenants."""
        pass


class FilesystemBackend(TenantConfigBackend):
    """Load tenant configs from masala-web directory structure."""

    def __init__(self, base_path: str | Path = "masala-web/tenants"):
        self.base_path = Path(base_path)

    def get_config(self, tenant_id: str) -> dict[str, Any]:
        """Load tenant config from JSON file.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Parsed config dict

        Raises:
            KeyError: If tenant not found
            ValueError: If tenant_id contains unsafe characters
        """
        if not _TENANT_ID_RE.match(tenant_id):
            raise ValueError(f"Invalid tenant_id: {tenant_id!r}")
        config_file = self.base_path / tenant_id / "config" / "masala_config.json"
        try:
            with open(config_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error("tenant_config.not_found", tenant_id=tenant_id, path=str(config_file))
            raise KeyError(f"Tenant {tenant_id} not found at {config_file}")
        except json.JSONDecodeError as e:
            logger.error("tenant_config.invalid_json", tenant_id=tenant_id, error=str(e))
            raise

    def list_tenant_ids(self) -> list[str]:
        """Scan filesystem for all tenant directories."""
        try:
            return [d.name for d in self.base_path.iterdir() if d.is_dir()]
        except FileNotFoundError:
            logger.error("tenant_config.base_path_not_found", path=str(self.base_path))
            return []


class EnvironmentBackend(TenantConfigBackend):
    """Load tenant configs from environment variables (for testing/overrides).

    Convention: TENANT_{UPPERCASE_ID}='{"key": "value"}'
    """

    def __init__(self, prefix: str = "TENANT_"):
        self.prefix = prefix

    def get_config(self, tenant_id: str) -> dict[str, Any]:
        """Get config from environment variable.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Parsed config dict

        Raises:
            KeyError: If env var not set or invalid JSON
        """
        import os

        key = f"{self.prefix}{tenant_id.upper()}"
        value = os.getenv(key)
        if not value:
            raise KeyError(f"Environment variable {key} not set")
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            logger.error("tenant_config.invalid_json", key=key, error=str(e))
            raise

    def list_tenant_ids(self) -> list[str]:
        """Scan environment for all TENANT_* variables."""
        import os

        ids = []
        for key in os.environ:
            if key.startswith(self.prefix):
                tenant_id = key[len(self.prefix) :].lower()
                ids.append(tenant_id)
        return ids


class CompositeBackend(TenantConfigBackend):
    """Chain multiple backends: try each until one succeeds.

    Useful for: environment overrides + file fallback + defaults.
    """

    def __init__(self, backends: list[TenantConfigBackend], default_config: dict[str, Any] | None = None):
        """Initialize composite backend.

        Args:
            backends: List of backends to try in order
            default_config: Merged as fallback if all backends fail
        """
        self.backends = backends
        self.default_config = default_config or {}

    def get_config(self, tenant_id: str) -> dict[str, Any]:
        """Try each backend in order until one succeeds.

        Returns:
            Merged config: {defaults} + {backend result}

        Raises:
            KeyError: If all backends fail and no defaults
        """
        for backend in self.backends:
            try:
                config = backend.get_config(tenant_id)
                # Merge: defaults get overridden by backend result
                return {**self.default_config, **config}
            except KeyError:
                continue

        # All backends failed. Use defaults if available.
        if self.default_config:
            logger.info("tenant_config.using_defaults", tenant_id=tenant_id)
            return self.default_config

        raise KeyError(f"Tenant {tenant_id} not found in any backend")

    def list_tenant_ids(self) -> list[str]:
        """Collect IDs from all backends."""
        seen = set()
        for backend in self.backends:
            try:
                seen.update(backend.list_tenant_ids())
            except Exception as e:
                logger.warning("tenant_config.list_failed", backend=type(backend).__name__, error=str(e))
        return list(seen)


# ------ Core Tenant Context ------


@dataclass(frozen=True)
class TenantContext:
    """Immutable tenant context for a single tenant.

    Acts as a view into the backing store with support for:
    - Caching (context doesn't know about backend storage)
    - Per-request overrides (functional composition)
    - Chaining/composition (parent fallback)
    """

    tenant_id: str
    config: dict[str, Any]
    overrides: dict[str, Any] = field(default_factory=dict)
    parent: Optional[TenantContext] = None

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value with override precedence.

        Precedence: overrides > config > parent > default

        Args:
            key: Simple key to look up
            default: Returned if not found anywhere

        Returns:
            Configuration value
        """
        if key in self.overrides:
            return self.overrides[key]
        if key in self.config:
            return self.config[key]
        if self.parent:
            return self.parent.get(key, default)
        return default

    def get_nested(self, path: str, default: Any = None) -> Any:
        """Get nested config value like 'GerdaAI.Ranking.Strategy'.

        Args:
            path: Dot-separated path
            default: Returned if not found

        Returns:
            Configuration value or default
        """
        parts = path.split(".")

        # Check overrides first (only top level)
        if parts[0] in self.overrides:
            value = self.overrides[parts[0]]
            for part in parts[1:]:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return default
            if value is not None:
                return value

        # Then check config
        value = self.config
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return default

        if value is not None:
            return value

        # Fall back to parent
        if self.parent:
            return self.parent.get_nested(path, default)
        return default

    def with_overrides(self, **overrides) -> TenantContext:
        """Return new context with additional overrides.

        Immutable and chainable. Does not modify original.

        Args:
            **overrides: Key-value pairs to override

        Returns:
            New TenantContext with merged overrides
        """
        merged_overrides = {**self.overrides, **overrides}
        return TenantContext(
            tenant_id=self.tenant_id,
            config=self.config,
            overrides=merged_overrides,
            parent=self.parent,
        )

    def with_parent(self, parent: TenantContext) -> TenantContext:
        """Create new context with parent for fallback/composition.

        Immutable. Original remains unchanged.

        Args:
            parent: Parent context to fall back to

        Returns:
            New TenantContext with parent set
        """
        return TenantContext(
            tenant_id=self.tenant_id,
            config=self.config,
            overrides=self.overrides,
            parent=parent,
        )

    def to_dict(self) -> dict[str, Any]:
        """Export all resolved values.

        Useful for: logging, passing to other services, serialization.

        Returns:
            Dict with tenant_id and merged config
        """
        return {
            "tenant_id": self.tenant_id,
            "config": {**self.config, **self.overrides},
        }


# ------ Manager (factory, cache, lifecycle) ------


class TenantContextManager:
    """Central factory and cache for tenant contexts.

    Responsibilities:
    - Load configs from storage backend (with caching)
    - Create TenantContext instances
    - Manage lifecycle (preload, invalidate cache)
    - Support composition and chaining
    """

    def __init__(self, backend: TenantConfigBackend):
        """Initialize manager with a storage backend.

        Args:
            backend: Implementation of TenantConfigBackend
        """
        self.backend = backend
        self._context_cache: dict[str, TenantContext] = {}

    def get_context(self, tenant_id: str, overrides: dict[str, Any] | None = None) -> TenantContext:
        """Get or create context for a tenant.

        First access loads from backend and caches. Subsequent accesses return
        cached version. Overrides are applied without modifying cache.

        Args:
            tenant_id: Tenant identifier
            overrides: Per-request config overrides

        Returns:
            Immutable TenantContext

        Raises:
            ValueError: If tenant not found
        """
        if tenant_id not in self._context_cache:
            try:
                config = self.backend.get_config(tenant_id)
                self._context_cache[tenant_id] = TenantContext(
                    tenant_id=tenant_id,
                    config=config,
                )
                logger.info("tenant_context.loaded", tenant_id=tenant_id)
            except KeyError:
                logger.error("tenant_context.get_failed", tenant_id=tenant_id)
                raise ValueError(f"Unknown tenant: {tenant_id}")

        context = self._context_cache[tenant_id]
        if overrides:
            context = context.with_overrides(**overrides)
        return context

    def get_contexts(self, tenant_ids: list[str], overrides: dict[str, Any] | None = None) -> list[TenantContext]:
        """Get multiple contexts (useful for cross-tenant workflows).

        Args:
            tenant_ids: List of tenant IDs
            overrides: Applied to all contexts

        Returns:
            List of TenantContext instances
        """
        return [self.get_context(tid, overrides) for tid in tenant_ids]

    def chain_contexts(
        self, primary_id: str, fallback_id: str, overrides: dict[str, Any] | None = None
    ) -> TenantContext:
        """Create chained context: primary with fallback parent.

        Use case: Process event from tenant A with defaults from tenant B.

        Args:
            primary_id: Primary tenant ID
            fallback_id: Fallback tenant ID
            overrides: Applied to primary

        Returns:
            TenantContext with fallback parent
        """
        primary = self.get_context(primary_id)
        fallback = self.get_context(fallback_id)
        chained = primary.with_parent(fallback)
        if overrides:
            chained = chained.with_overrides(**overrides)
        return chained

    def list_tenants(self) -> list[str]:
        """List all available tenants.

        Returns:
            List of tenant IDs
        """
        return self.backend.list_tenant_ids()

    def invalidate_cache(self, tenant_id: str | None = None) -> None:
        """Clear cache to force reload from backend.

        Args:
            tenant_id: Specific tenant to invalidate, or None for all
        """
        if tenant_id:
            self._context_cache.pop(tenant_id, None)
            logger.info("tenant_context.cache_invalidated", tenant_id=tenant_id)
        else:
            self._context_cache.clear()
            logger.info("tenant_context.cache_cleared")

    def preload(self, tenant_ids: list[str] | None = None) -> None:
        """Warm cache for all (or specified) tenants.

        Args:
            tenant_ids: Specific tenants to preload, or None for all
        """
        ids = tenant_ids or self.list_tenants()
        failed = []
        for tid in ids:
            try:
                self.get_context(tid)
            except ValueError:
                failed.append(tid)

        logger.info("tenant_context.preload_done", count=len(ids) - len(failed), failed=len(failed))
        if failed:
            logger.warning("tenant_context.preload_failed", tenants=failed)


# ------ Convenience factory (singleton pattern) ------

_global_manager: TenantContextManager | None = None


def initialize_tenant_context(backend: TenantConfigBackend | None = None) -> TenantContextManager:
    """Initialize global tenant context manager.

    Recommended usage in service startup:
    ```python
    backend = FilesystemBackend("masala-web/tenants")
    manager = initialize_tenant_context(backend)
    manager.preload()  # Warm cache on startup
    ```

    Args:
        backend: Storage backend, defaults to FilesystemBackend

    Returns:
        Initialized TenantContextManager
    """
    global _global_manager
    if backend is None:
        backend = FilesystemBackend()
    _global_manager = TenantContextManager(backend)
    logger.info("tenant_context.initialized", backend=type(backend).__name__)
    return _global_manager


def get_tenant_context_manager() -> TenantContextManager:
    """Get global manager. Initialize if needed.

    Returns:
        TenantContextManager instance
    """
    global _global_manager
    if _global_manager is None:
        _global_manager = initialize_tenant_context()
    return _global_manager


def get_tenant_context(tenant_id: str, overrides: dict[str, Any] | None = None) -> TenantContext:
    """Convenience function: get context from global manager.

    Args:
        tenant_id: Tenant identifier
        overrides: Per-request config overrides

    Returns:
        TenantContext instance
    """
    return get_tenant_context_manager().get_context(tenant_id, overrides)
