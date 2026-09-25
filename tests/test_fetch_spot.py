import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_spot.py"
SPEC = importlib.util.spec_from_file_location("fetch_spot", SCRIPT)
fetch_spot = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fetch_spot)


def test_write_rows_uses_lf_not_crlf(tmp_path):
    path = tmp_path / "transmission_spot.csv"
    fetch_spot.write_rows(
        path,
        [{
            "date": "2026-08-05",
            "time_label": "1600",
            "spot": "7723.54",
            "source_timestamp": "2026-08-05T16:00:00-04:00",
            "source": "yahoo:^GSPC:1m",
            "note": "",
        }],
    )
    payload = path.read_bytes()
    assert b"\r\n" not in payload
    assert payload.count(b"\n") == 2
