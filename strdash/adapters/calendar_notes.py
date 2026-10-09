"""Airbnb host calendar: blocked dates and the host's notes on them.

Input columns: listing_id, kind (blocker | block_note | private_note), start,
end (inclusive), type (HOST_BUSY | SYNCED_CALENDAR | BOOKING_WINDOW | ...), note.

Output:
  * direct bookings - notes that describe a paying or named guest
  * blocks          - closed nights that are not stays (maintenance, personal,
                      unknown)

Note text often contains guest names and phone numbers. Only the category,
dates and parsed amount leave this module; the text itself is never written
to the clean tables.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from ..schema import BLOCK_COLS, BOOKING_COLS

MAINT = r"rentokil|pest|bug|treatment|\bac\b|aircond|service|repair|maint|renovat|clean|leak|inspect|mattress|floor|water|plumb|light|smell|tnb|wifi|unifi"
PERSONAL = r"relative|family|coming over|on leave|\boff\b|owner|friend|cousin|visiting|my |own use"
DIRECT = r"rm\s?\d|\+60|\b0\d{8,10}\b|direct|additional night|extend|extension|\d+\s*nights?"


def classify(note: str) -> str:
    t = str(note).lower()
    if re.search(MAINT, t):
        return "maintenance"
    if re.search(PERSONAL, t):
        return "personal"
    if "agoda" in t or "airbnb" in t:
        return "platform"            # note about a platform stay - not a separate booking
    if re.search(DIRECT, t):
        return "direct"
    return "direct_unpriced"         # name only: treated as a direct guest, revenue unknown


def parse_amount(note: str, nights: int) -> float | None:
    """Best-effort RM amount for a direct stay. Returns None when there is no price."""
    t = str(note).replace(",", "")
    if not re.search(r"(?i)rm\s?\d", t):
        return None
    if re.search(r"(?i)/\s*month|per month|monthly", t):
        m = re.search(r"(?i)rm\s?(\d+(?:\.\d+)?)", t)
        return round(float(m.group(1)) * nights / 30, 2)
    after_eq = re.findall(r"=\s*(?:rm)?\s?(\d+(?:\.\d+)?)", t, flags=re.I)
    if after_eq:
        return float(after_eq[-1])
    cands = [float(x) for x in re.findall(r"(?i)rm\s?(\d+(?:\.\d+)?)", t)]
    # "RM115 x 2 + RM60" style expressions
    for m in re.finditer(r"(?i)rm\s?(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+)((?:\s*\+\s*(?:rm)?\s?\d+(?:\.\d+)?)*)", t):
        v = float(m.group(1)) * int(m.group(2))
        v += sum(float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", m.group(3) or ""))
        cands.append(v)
    # bare "115x2 + 60= 290" without RM is caught by after_eq; take the largest
    # figure as the stay total (totals are written alongside per-night rates)
    return max(cands) if cands else None


def text_range(note: str, anchor: pd.Timestamp):
    """Dates written in the note itself, e.g. '18-29/8' or '19-23/7' -> (checkin, checkout).
    The host writes check-in day - check-out day / month. Year comes from where the
    note sits on the calendar. Returns None when the note has no range."""
    m = re.search(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*/\s*(\d{1,2})\b", str(note))
    if not m:
        return None
    d1, d2, mo = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d1 <= 31 and 1 <= d2 <= 31):
        return None
    best = None
    for yr in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            if d2 > d1:
                ci, co = pd.Timestamp(yr, mo, d1), pd.Timestamp(yr, mo, d2)
            else:  # "30-2/1": the month belongs to check-out; check-in is the month before
                co = pd.Timestamp(yr, mo, d2)
                prev = co.replace(day=1) - pd.Timedelta(days=1)
                ci = pd.Timestamp(prev.year, prev.month, d1)
        except ValueError:
            continue
        dist = abs((ci - anchor).days)
        if best is None or dist < best[0]:
            best = (dist, ci, co)
    if best is None or best[0] > 45 or not (1 <= (best[2] - best[1]).days <= 60):
        return None
    return best[1], best[2]


def _merge_runs(n: pd.DataFrame) -> pd.DataFrame:
    """Join pieces of the same note that were split at year boundaries."""
    n = n.sort_values(["unit_id", "note", "start"])
    out = []
    for (u, note), d in n.groupby(["unit_id", "note"], sort=False):
        cur = None
        for _, r in d.iterrows():
            if cur is not None and r["start"] <= cur["end"] + pd.Timedelta(days=1):
                cur["end"] = max(cur["end"], r["end"])
            else:
                if cur is not None:
                    out.append(cur)
                cur = r.to_dict()
        out.append(cur)
    return pd.DataFrame(out)


def load(path, unit_lookup: dict[str, str], delisted_after: dict[str, str],
         notes_start: str = "2023-01-01"):
    b = pd.read_csv(path, encoding="utf-8-sig", dtype={"listing_id": str}).drop_duplicates()
    b["unit_id"] = b["listing_id"].map(unit_lookup)
    b = b[b["unit_id"].notna()].copy()
    b["start"] = pd.to_datetime(b["start"])
    b["end"] = pd.to_datetime(b["end"])

    # ---- notes -> direct stays / categorised blocks ----
    notes = b[b["kind"].isin(["block_note", "private_note"]) & b["note"].notna()].copy()
    notes = notes[notes["start"] >= notes_start]
    notes = _merge_runs(notes[["unit_id", "start", "end", "note"]])
    notes["category"] = notes["note"].map(classify)
    notes["nights"] = (notes["end"] - notes["start"]).dt.days + 1

    d = notes[notes["category"].isin(["direct", "direct_unpriced"])].copy()
    # stay dates: prefer the range written in the note ("18-29/8"), else the calendar span
    d["checkin"] = d["start"]
    d["checkout"] = d["end"] + pd.Timedelta(days=1)
    for i, r in d.iterrows():
        tr = text_range(r["note"], r["start"])
        if tr:
            d.at[i, "checkin"], d.at[i, "checkout"] = tr
    d["nights"] = (d["checkout"] - d["checkin"]).dt.days
    d["amount"] = [parse_amount(t, n) for t, n in zip(d["note"], d["nights"])]
    # a single bare price far below a night's rate on a multi-night stay is a nightly rate
    single = d["note"].str.count(r"(?i)rm\s?\d") == 1
    plain = ~d["note"].str.contains(r"(?i)[x×=]|month", regex=True)
    nightly_rate = single & plain & (d["nights"] > 1) & (d["amount"] / d["nights"] < 70)
    d.loc[nightly_rate, "amount"] = d.loc[nightly_rate, "amount"] * d.loc[nightly_rate, "nights"]
    d = d.sort_values("checkin").drop_duplicates(["unit_id", "checkin"])
    direct = pd.DataFrame({
        "booking_id": ["DR-%s-%s" % (u, s.strftime("%Y%m%d")) for u, s in zip(d["unit_id"], d["checkin"])],
        "channel": "direct", "unit_id": d["unit_id"], "booked_date": pd.NaT,
        "checkin": d["checkin"], "checkout": d["checkout"], "nights": d["nights"],
        "status": "confirmed", "gross_revenue": d["amount"], "cleaning_fee": np.nan,
        "platform_fee": 0.0, "net_revenue": d["amount"],
        "revenue_estimated": d["amount"].notna(),   # parsed from free text
        "revenue_known": d["amount"].notna(),
    }).drop_duplicates("booking_id")[BOOKING_COLS]

    # ---- blocked nights ----
    blk = b[(b["kind"] == "blocker") & b["type"].isin(["HOST_BUSY", "SYNCED_CALENDAR"])]
    rows = []
    for _, r in blk.iterrows():
        for day in pd.date_range(r["start"], r["end"]):
            rows.append((r["unit_id"], day))
    nights = pd.DataFrame(rows, columns=["unit_id", "date"]).drop_duplicates()
    # ignore Airbnb blocks after a unit was deliberately delisted from Airbnb
    cut = nights["unit_id"].map(delisted_after)
    nights = nights[cut.isna() | (nights["date"] <= pd.to_datetime(cut))]

    cat_rows = []
    for _, r in notes[notes["category"].isin(["maintenance", "personal"])].iterrows():
        for day in pd.date_range(r["start"], r["end"]):
            cat_rows.append((r["unit_id"], day, r["category"]))
    cats = pd.DataFrame(cat_rows, columns=["unit_id", "date", "category"]).drop_duplicates(["unit_id", "date"])
    blocks = (pd.concat([nights.assign(category="unknown"), cats])
              .sort_values("category")             # 'maintenance' < 'personal' < 'unknown'
              .drop_duplicates(["unit_id", "date"]))
    return direct, blocks[BLOCK_COLS].reset_index(drop=True)
