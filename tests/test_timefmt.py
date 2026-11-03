"""parse_ts lê os timestamps que o Postgres e o próprio hub gravam, em qualquer Python."""
from datetime import datetime, timezone

import pytest

from store.timefmt import parse_ts

UTC = timezone.utc


@pytest.mark.parametrize("raw,expected", [
    ("2026-11-02T06:32:58.29789+00:00", datetime(2026, 11, 2, 6, 32, 58, 297890, UTC)),
    ("2026-11-02T06:32:58.29+00:00", datetime(2026, 11, 2, 6, 32, 58, 290000, UTC)),
    ("2026-11-02T06:32:58.123456789Z", datetime(2026, 11, 2, 6, 32, 58, 123456, UTC)),
    ("2026-11-02 06:32:58-03", datetime(2026, 11, 2, 9, 32, 58, 0, UTC)),
    ("2026-11-02T06:32:58+0530", datetime(2026, 11, 2, 1, 2, 58, 0, UTC)),
    ("2026-11-02T06:32", datetime(2026, 11, 2, 6, 32, 0, 0, UTC)),
    ("2026-11-02", datetime(2026, 11, 2, 0, 0, 0, 0, UTC)),
])
def test_parse_ts_variants(raw, expected):
    assert parse_ts(raw) == expected
    assert parse_ts(raw).tzinfo == UTC


def test_parse_ts_rejects_garbage_and_none():
    for bad in ("", None, "ontem", "2026-13"):
        with pytest.raises(ValueError):
            parse_ts(bad)


def test_parse_ts_naive_datetime_is_utc():
    assert parse_ts(datetime(2026, 1, 1, 12)) == datetime(2026, 1, 1, 12, tzinfo=UTC)
