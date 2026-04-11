from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def get_kst_date_str() -> str:
    """Return current date string in KST (YYYY-MM-DD)."""
    if ZoneInfo is not None:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    else:
        now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    return now.date().isoformat()
