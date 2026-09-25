"""Massive (ex-Polygon) option-chain snapshot provider.

Endpoint: GET {base_url}/v3/snapshot/options/{underlying}

One call returns the whole chain page-by-page with pricing, greeks, IV,
quotes, day aggregates and open interest per contract — the OI and the provider
gamma the experiment needs arrive in the same response, which is what makes
§7 Path A checkable against the exact payload it was computed from.

Nothing here renames or filters. Records are flattened to dotted provider names
(`details.strike_price`, `greeks.gamma`, `last_quote.timeframe`) and handed on.

Two documented provider gaps, both surfaced rather than patched over:
  * `last_quote` / `last_trade` are only populated on plans that include
    quotes. Without them §7 Path B is impossible; Path A is unaffected.
  * greeks may be absent on deep-ITM contracts, which shapes the §7 sample.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .base import FetchMeta, FetchResult, ProviderError, flatten_records

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit("pip install requests")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _ns_to_iso(ns: Any) -> str | None:
    try:
        seconds = float(ns) / 1e9
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class MassiveProvider:
    def __init__(self, config, api_key):
        self._cfg = config
        # A callable, not a string: the credential is fetched at request time
        # and never lives on the instance, so it cannot end up in a repr, a
        # traceback frame, or a pickled object.
        self._api_key = api_key if callable(api_key) else (lambda: api_key)
        self._auth_scheme = str(config.get("provider.auth_scheme", "bearer")).lower()
        self._base_url = str(config.require("provider.base_url")).rstrip("/")
        self._page_limit = int(config.get("provider.page_limit", 250))
        self._max_pages = int(config.get("provider.max_pages", 200))
        self._timeout = float(config.get("provider.request_timeout_seconds", 30))
        self._max_retries = int(config.get("provider.max_retries", 5))
        self._backoff = float(config.get("provider.retry_backoff_seconds", 2.0))
        self._page_sleep = float(config.get("provider.sleep_between_pages_seconds", 0.0))
        self._extra_params = dict(config.get("provider.extra_params", {}) or {})
        self._allow_truncated = bool(
            config.get("provider.allow_truncated_pagination", False)
        )
        self._max_dte = int(config.require("capture.expiry_scope_max_dte"))

    def provider_name(self) -> str:
        return "massive"

    def endpoint(self, underlying: str) -> str:
        return f"{self._base_url}/v3/snapshot/options/{underlying}"

    # --- spec §4 contract ----------------------------------------------------

    def fetch_chain(self, underlying: str, asof: datetime) -> pd.DataFrame:
        return self.fetch_chain_with_meta(underlying, asof).frame

    def fetch_chain_with_meta(self, underlying: str, asof: datetime) -> FetchResult:
        started = datetime.now(timezone.utc)
        url = self.endpoint(underlying)
        asof_date = asof.date()
        params: dict[str, Any] = {
            "expiration_date.gte": asof_date.isoformat(),
            "expiration_date.lte": (asof_date + timedelta(days=self._max_dte)).isoformat(),
            "limit": self._page_limit,
            **self._extra_params,
        }
        # No credential in the query string. It travels in the Authorization
        # header, so it cannot leak into `next_url`, proxy logs, or the
        # `request_params` we persist in every .meta.json.
        request_params_for_log = dict(params)

        records: list[dict[str, Any]] = []
        underlying_asset: dict[str, Any] | None = None
        pages = 0
        notes: list[str] = []

        while url and pages < self._max_pages:
            payload = self._get(url, params)
            batch = payload.get("results") or []
            records.extend(batch)
            if underlying_asset is None and batch:
                candidate = batch[0].get("underlying_asset")
                if isinstance(candidate, dict):
                    underlying_asset = candidate
            pages += 1
            url = payload.get("next_url")
            # next_url already carries the full query, and the credential is in
            # the header, so nothing has to be re-attached.
            params = {}
            if url and self._page_sleep:
                time.sleep(self._page_sleep)

        if url and pages >= self._max_pages:
            if self._allow_truncated:
                notes.append(
                    f"TRUNCATED at {pages} pages with next_url still present — probe "
                    "mode only; a capture must never run this way."
                )
            else:
                raise ProviderError(
                    f"pagination stopped at max_pages={self._max_pages} with next_url "
                    "still present — the capture would be silently short. Raise "
                    "provider.max_pages."
                )
        if not records:
            raise ProviderError(
                f"zero contracts returned for {underlying}. Either the plan lacks index "
                "options or the query window is empty."
            )

        frame = flatten_records(records)
        finished = datetime.now(timezone.utc)

        ts_field = None
        for candidate in ("last_quote.last_updated", "day.last_updated", "last_trade.sip_timestamp"):
            if candidate in frame.columns and frame[candidate].notna().any():
                ts_field = candidate
                break
        ts_min = ts_max = None
        if ts_field is not None:
            values = pd.to_numeric(frame[ts_field], errors="coerce").dropna()
            if not values.empty:
                ts_min = _ns_to_iso(values.min())
                ts_max = _ns_to_iso(values.max())
        else:
            notes.append(
                "no per-contract provider timestamp field present — §5.3 requires T to be "
                "computed from the provider timestamp, so this plan cannot run the study."
            )

        timeframes: dict[str, int] = {}
        if "last_quote.timeframe" in frame.columns:
            timeframes = {
                str(k): int(v)
                for k, v in frame["last_quote.timeframe"].value_counts().items()
            }
        else:
            notes.append(
                "no last_quote block — plan excludes quotes; §7 Path B is impossible, "
                "Path A unaffected."
            )

        if "greeks.gamma" not in frame.columns:
            notes.append("no greeks.gamma column — §7 Path A cannot run on this plan.")

        meta = FetchMeta(
            provider=self.provider_name(),
            endpoint=self.endpoint(underlying),
            request_started_utc=started.isoformat(),
            request_finished_utc=finished.isoformat(),
            pages=pages,
            row_count=int(len(frame)),
            request_params=request_params_for_log,
            provider_timestamp_field=ts_field,
            provider_timestamp_min_utc=ts_min,
            provider_timestamp_max_utc=ts_max,
            quote_timeframes=timeframes,
            underlying_asset=underlying_asset,
            notes=notes,
        )
        return FetchResult(frame=frame, meta=meta)

    # --- transport -----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Built per request from the live credential; never stored on self."""
        if self._auth_scheme == "bearer":
            return {"Authorization": f"Bearer {self._api_key()}"}
        raise ProviderError(
            f"unsupported provider.auth_scheme {self._auth_scheme!r}; expected bearer"
        )

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error = ""
        headers = self._auth_headers()
        for attempt in range(self._max_retries):
            try:
                response = requests.get(
                    url, params=params, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code == 200:
                    return response.json()
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                if response.status_code not in _RETRYABLE_STATUS:
                    raise ProviderError(last_error)
            if attempt < self._max_retries - 1:
                time.sleep(self._backoff * (2**attempt))
        raise ProviderError(f"giving up after {self._max_retries} attempts. {last_error}")
