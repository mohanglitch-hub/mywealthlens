"""
Wealth — Timezone Utilities (Phase I)
========================================
Single centralized helper for IST (Asia/Kolkata) date determination
(Section 11 of the Phase I spec — "do not scatter timezone strings
throughout the code... introduce a single shared configuration/
helper"). Every place that needs "today's date for snapshot purposes"
must call through here, not compute it independently.

Uses Python's stdlib `zoneinfo` (3.9+) rather than pytz — no extra
dependency needed, and this project's confirmed interpreter (3.12,
matching requirements.txt's already-installed stack) has it built in.

India Standard Time has a fixed +5:30 offset and does not observe
daylight saving time (Section 76) — zoneinfo's "Asia/Kolkata" handles
this correctly without any DST-specific logic on our part.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """Current wall-clock datetime in IST, timezone-aware."""
    return datetime.now(IST)


def today_ist():
    """
    Current CALENDAR DATE in IST, as a plain `date` (Section 10:
    snapshot dates are dates, not timestamps). This is the ONLY
    function that should ever be used to determine "what date is it
    for snapshot purposes" — never `date.today()` (implicit server/
    Windows local time) and never a UTC date converted after the
    fact (Section 10/43/75: both can silently produce the wrong
    calendar day around midnight IST, which is 18:30 UTC).
    """
    return now_ist().date()
