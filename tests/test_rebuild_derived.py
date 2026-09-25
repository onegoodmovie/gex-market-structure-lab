"""Safety and discovery tests for the offline rebuild entry point."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_derived.py"
SPEC = importlib.util.spec_from_file_location("rebuild_derived", SCRIPT)
assert SPEC and SPEC.loader
rebuild = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rebuild)


def test_discovers_only_dated_directories_with_parquet(tmp_path: Path):
    valid = tmp_path / "2026-07-31"
    valid.mkdir()
    (valid / "0945.parquet").write_bytes(b"fixture")
    empty = tmp_path / "2026-08-01"
    empty.mkdir()
    (tmp_path / "notes").mkdir()

    assert rebuild.discover_dates(tmp_path) == ["2026-07-31"]


def test_output_must_be_empty(tmp_path: Path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("do not overwrite")

    with pytest.raises(ValueError, match="not empty"):
        rebuild.require_empty_output(occupied)


def test_output_may_not_be_inside_canonical_data_tree():
    with pytest.raises(ValueError, match="may not be data"):
        rebuild.require_empty_output(rebuild.REPO_ROOT / "data" / "rebuild")


def test_comparison_distinguishes_exact_changed_and_new(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    output = tmp_path / "output"
    for root in (repo / "data" / "normalized", output / "normalized"):
        root.mkdir(parents=True)
    (repo / "data" / "derived").mkdir(parents=True)
    (output / "derived").mkdir(parents=True)
    (repo / "data" / "normalized" / "exact").write_text("same")
    (output / "normalized" / "exact").write_text("same")
    (repo / "data" / "normalized" / "changed").write_text("before")
    (output / "normalized" / "changed").write_text("after")
    (output / "normalized" / "new").write_text("new")
    monkeypatch.setattr(rebuild, "REPO_ROOT", repo)

    result = rebuild.compare_to_current(output)

    assert result["exact_count"] == 1
    assert result["changed"] == ["normalized/changed"]
    assert result["new"] == ["normalized/new"]
