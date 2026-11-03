"""
agent-memory-hub — ISO-8601 parsing that works the same on every Python the hooks run on.

Postgres drops trailing zeros from the fraction of a second (06:32:58.29789+00:00) and the
Python 3.9 of /usr/bin/python3 cannot read that with datetime.fromisoformat (it wants 3 or 6
digits). parse_ts reads any fraction from 1 to 9 digits, a Z, an offset with or without the
colon, and a bare date.
"""
import re
from datetime import datetime, timedelta, timezone

_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
                    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?$")


def parse_ts(value):
    """datetime aware em UTC. Sem fuso = UTC (o que o hub grava). ValueError se não for ISO."""
    if isinstance(value, datetime):
        dt = value
    else:
        m = _TS_RE.match(str(value or "").strip())
        if not m:
            raise ValueError(f"timestamp inválido: {value!r}")
        y, mo, d, h, mi, s, frac, tz = m.groups()
        micro = int((frac or "0")[:6].ljust(6, "0"))
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), micro)
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            off = int(digits[:2]) * 60 + int(digits[2:4] or 0)
            dt = dt.replace(tzinfo=timezone(sign * timedelta(minutes=off)))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
