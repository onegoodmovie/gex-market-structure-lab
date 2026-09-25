"""Provider registry (spec §4). One file per provider; base.py is the contract."""

from __future__ import annotations

from .base import ChainProvider, FetchMeta, FetchResult, ProviderError, flatten_records

_REGISTRY = {"massive", "synthetic"}


def get_provider(config, name: str | None = None):
    """Build the configured provider. Synthetic needs no key; real ones do."""
    provider_name = (name or config.require("provider.name")).lower()
    if provider_name not in _REGISTRY:
        raise ValueError(
            f"unknown provider '{provider_name}'. Known: {sorted(_REGISTRY)}"
        )
    if provider_name == "synthetic":
        from .synthetic import SyntheticProvider

        return SyntheticProvider(config)
    from .massive import MassiveProvider

    # Pass the accessor, not the value — see MassiveProvider.__init__.
    return MassiveProvider(config, api_key=config.api_key)


__all__ = [
    "ChainProvider",
    "FetchMeta",
    "FetchResult",
    "ProviderError",
    "flatten_records",
    "get_provider",
]
