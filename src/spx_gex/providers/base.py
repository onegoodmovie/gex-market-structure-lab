"""Provider contract (spec §4).

A provider does exactly one thing: hand back the provider's payload flattened
into a DataFrame with the *provider's own column names*. No renaming, no
filtering, no unit conversion, no derived columns. Everything else happens in
normalize.py, so that switching providers later does not invalidate stored
history.

`fetch_chain` is the contract from the spec. `fetch_chain_with_meta` is the
same call plus the provenance the capture scripts must persist (§16.2);
implementations write one and get the other for free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd


@dataclass
class FetchMeta:
    """Provenance for one capture. Serialized verbatim into `.meta.json`."""

    provider: str
    endpoint: str
    request_started_utc: str
    request_finished_utc: str
    pages: int
    row_count: int
    request_params: dict[str, Any] = field(default_factory=dict)
    provider_timestamp_field: str | None = None
    provider_timestamp_min_utc: str | None = None
    provider_timestamp_max_utc: str | None = None
    quote_timeframes: dict[str, int] = field(default_factory=dict)
    underlying_asset: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    frame: pd.DataFrame
    meta: FetchMeta


@runtime_checkable
class ChainProvider(Protocol):
    def fetch_chain(self, underlying: str, asof: datetime) -> pd.DataFrame:
        """Return the RAW provider payload flattened to a DataFrame.

        Provider column names are preserved. No renaming, no filtering.
        """
        ...

    def provider_name(self) -> str:
        ...


class ProviderError(RuntimeError):
    """Provider transport or payload failure. Never swallowed."""


# --- shared helpers ----------------------------------------------------------


def flatten_record(obj: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten one nested provider record to dotted provider column names.

    `{"details": {"strike_price": 7400}}` -> `{"details.strike_price": 7400}`.
    Lists are JSON-encoded rather than exploded: the raw layer must stay
    lossless and one-row-per-contract.
    """
    out: dict[str, Any] = {}
    for key, value in obj.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten_record(value, prefix=f"{name}."))
        elif isinstance(value, (list, tuple)):
            out[name] = json.dumps(value)
        else:
            out[name] = value
    return out


def flatten_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Flatten a page list into a DataFrame, preserving provider column names.

    Column order follows first-seen order across the payload, so a re-fetch of
    the same data produces an identically ordered frame.
    """
    flat = [flatten_record(r) for r in records]
    columns: list[str] = []
    seen: set[str] = set()
    for row in flat:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return pd.DataFrame(flat, columns=columns)
