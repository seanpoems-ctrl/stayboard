"""Turn bookings + blocks into one row per unit per night."""
from __future__ import annotations

import numpy as np
import pandas as pd

PLATFORM = ("airbnb", "agoda", "bookingcom")
STATE_ORDER = ["stay", "maintenance", "personal", "unknown", "open"]


def drop_shadow_direct(bookings: pd.DataFrame, min_overlap: float = 0.5) -> pd.DataFrame:
    """Drop 'direct' rows whose nights are mostly covered by a platform stay
    (notes that merely annotate an Airbnb/Agoda guest)."""
    conf = bookings[(bookings.status == "confirmed")]
    plat = conf[conf.channel.isin(PLATFORM)]
    taken = set()
    for r in plat.itertuples():
        for d in pd.date_range(r.checkin, r.checkout - pd.Timedelta(days=1)):
            taken.add((r.unit_id, d))
    keep = []
    for r in bookings.itertuples():
        if r.channel != "direct":
            keep.append(True)
            continue
        days = pd.date_range(r.checkin, r.checkout - pd.Timedelta(days=1))
        cov = sum((r.unit_id, d) in taken for d in days) / max(len(days), 1)
        keep.append(cov < min_overlap)
    return bookings[np.array(keep)]


def nightly(bookings: pd.DataFrame, blocks: pd.DataFrame, units: list[dict],
            as_of: pd.Timestamp, forward_days: int) -> pd.DataFrame:
    """Columns: unit_id, date, state, channel, booking_id, gross, net, room, rev_known, past.
    room = gross minus cleaning fee (the nightly rate the guest paid)."""
    conf = bookings[bookings.status == "confirmed"].copy()
    conf["prio"] = np.where(conf.channel.isin(PLATFORM), 0, 1)
    rows = []
    for r in conf.sort_values(["prio", "checkin"]).itertuples():
        n = max(int(r.nights), 1)
        g = (r.gross_revenue / n) if pd.notna(r.gross_revenue) else np.nan
        nt = (r.net_revenue / n) if pd.notna(r.net_revenue) else np.nan
        rm = ((r.gross_revenue - r.cleaning_fee) / n) if pd.notna(r.gross_revenue) and pd.notna(r.cleaning_fee) else g
        for d in pd.date_range(r.checkin, r.checkout - pd.Timedelta(days=1)):
            rows.append((r.unit_id, d, "stay", r.channel, r.booking_id, g, nt, rm, bool(r.revenue_known)))
    stays = pd.DataFrame(rows, columns=["unit_id", "date", "state", "channel", "booking_id", "gross", "net", "room", "rev_known"])
    dup = stays.duplicated(["unit_id", "date"])
    stays = stays[~dup]          # first one wins (platform before direct)

    end = as_of + pd.Timedelta(days=forward_days)
    frames = []
    for u in units:
        if not u.get("active"):
            continue
        ub = conf[conf.unit_id == u["id"]]
        if ub.empty:
            continue
        start = pd.Timestamp(u.get("live_from") or ub.checkin.min())
        cal = pd.DataFrame({"unit_id": u["id"], "date": pd.date_range(start, end - pd.Timedelta(days=1))})
        frames.append(cal)
    cal = pd.concat(frames, ignore_index=True)
    out = cal.merge(stays, on=["unit_id", "date"], how="left")
    out = out.merge(blocks.rename(columns={"category": "block"}), on=["unit_id", "date"], how="left")
    out["state"] = out["state"].fillna(out["block"]).fillna("open")
    out = out.drop(columns="block")
    out["past"] = out["date"] < as_of
    out["rev_known"] = out["rev_known"].fillna(False).astype(bool)
    return out, int(dup.sum())
