"""Settle konvence US seance — hranice obchodního dne na JEDNOM místě (#498).

Sdílí ji SetupEngine (settle expirace setupů) a T6 (řez denních closů).
Pozn.: issue #511 později nahradí fixní UTC hodinu odvozením z burzovní
timezone (DST posouvá 16:00 ET mezi 20:00 a 21:00 UTC) — proto konvence nesmí
existovat na více místech; kdo ji potřebuje, importuje odsud.
"""

import datetime as dt

# Settle ≈ 20:00 UTC (shodné s frontend instrument/expiry.ts)
SETTLE_HOUR_UTC = 20


def settle_ts(day: dt.date) -> dt.datetime:
    """Okamžik settle US seance daného kalendářního dne (UTC)."""
    return dt.datetime.combine(day, dt.time(SETTLE_HOUR_UTC, 0), tzinfo=dt.UTC)
