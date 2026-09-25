"""Configuration and path resolution.

Two jobs:
  1. load config/experiment.yaml with dotted-path access
  2. guarantee acceptance criterion §16.15 — nothing outside this repo is
     ever read or written by our own code

`null` in the YAML is a real value meaning "undetermined". `require()` turns
reading one into a hard error, so an unresolved convention stops the run
instead of silently defaulting.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "experiment.yaml"

_MISSING = object()


class ConfigError(RuntimeError):
    """Raised when configuration is absent, unresolved, or inconsistent."""


class Config:
    """Dotted-path read-only view over experiment.yaml."""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        self._data = data
        self.source = source

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        p = Path(path) if path is not None else CONFIG_PATH
        if not p.exists():
            raise ConfigError(f"config not found: {p}")
        with p.open() as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data, source=p)

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                if default is _MISSING:
                    raise ConfigError(f"missing config key: {dotted}")
                return default
            cur = cur[part]
        return cur

    def require(self, dotted: str) -> Any:
        """Like get(), but a null value is an error rather than a value."""
        value = self.get(dotted)
        if value is None:
            raise ConfigError(
                f"config key '{dotted}' is null (undetermined). "
                "Resolve it before running this step — see docs/METHODOLOGY.md."
            )
        return value

    def override(self, dotted: str, value: Any) -> "Config":
        """Return a copy with one key replaced. Used by the probe, never by capture."""
        data = self.as_dict()
        cur = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        return Config(data, source=self.source)

    def as_dict(self) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self._data)

    # --- credentials ---------------------------------------------------------

    def api_key(self) -> str:
        """Fetch the API key at the moment it is needed.

        Never cached on an object, never logged, never written anywhere. The
        return value goes straight into an Authorization header and nowhere
        else — in particular it must not reach `.meta.json`, which is why the
        request params recorded there carry no credential at all.
        """
        source = str(self.get("provider.credential.source", "env")).lower()
        if source == "keychain":
            return self._api_key_from_keychain()
        if source == "env":
            return self._api_key_from_env()
        raise ConfigError(
            f"unknown provider.credential.source {source!r}; expected keychain or env"
        )

    def _api_key_from_env(self) -> str:
        env_name = self.require("provider.credential.api_key_env")
        key = os.environ.get(env_name)
        if not key:
            raise ConfigError(
                f"environment variable {env_name} is not set. "
                f"Export it in your shell; do not put it in this repo."
            )
        return key

    def _api_key_from_keychain(self) -> str:
        if sys.platform != "darwin":
            raise ConfigError(
                "provider.credential.source is 'keychain', but the macOS Keychain "
                f"is not available on {sys.platform}. Switch to 'env'."
            )
        service = self.require("provider.credential.keychain_service")
        account = self.require("provider.credential.keychain_account")
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"could not run `security`: {exc}") from exc

        if result.returncode != 0:
            # stderr from `security` names the item, never its value.
            raise ConfigError(
                f"no Keychain item for service={service!r} account={account!r} "
                f"(security exit {result.returncode}: {result.stderr.strip() or 'no detail'}).\n"
                "If this is running under cron: the login keychain must be "
                "unlocked, which it is not when the machine is locked or freshly "
                "rebooted. Test the scheduled path, not just an interactive shell."
            )
        key = result.stdout.strip()
        if not key:
            raise ConfigError(
                f"Keychain item service={service!r} account={account!r} is empty"
            )
        return key


# --- paths -------------------------------------------------------------------


def repo_path(*parts: str | Path) -> Path:
    """Resolve a repo-relative path, refusing anything that escapes the repo."""
    candidate = REPO_ROOT.joinpath(*[str(p) for p in parts]).resolve()
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(
            f"refusing to touch a path outside the repository: {candidate}"
        ) from exc
    return candidate


def raw_chain_dir(date_str: str) -> Path:
    return repo_path("data", "raw_chain", date_str)


def manual_input_dir() -> Path:
    return repo_path("data", "manual_input")
