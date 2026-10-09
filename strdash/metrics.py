"""Aggregate the nightly table and bookings into the compact cubes the dashboard reads.

The dashboard does the final roll-ups in the browser (any date window, unit
or channel filter), so here we only pre-aggregate to unit x month x channel.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CH_CODE = {"airbnb": "A", "agoda": "G", "direct": "D", "bookingcom": "B"}
STATE_CODE = {"maintenance": "M", "personal": "P", "unknown": "U", "open": "O"}


def _r(x, nd=2):
    return None if pd.isna(x) else round(float(x), nd)


def monthly_stays(night: pd.DataFrame) -> list:
    s = night[night.state == "stay"].copy()
    s["m"] = s.date.dt.strftime("%Y-%m")
    s["gk"] = s.gross.where(s.rev_known)
    s["nk"] = s.net.where(s.rev_known)
    s["rk"] = s.room.where(s.rev_known)
    g = s.groupby(["unit_id", "m", "channel"]).agg(
        n=("date", "size"), nk=("rev_known", "sum"), g=("gk", "sum"), nt=("nk", "sum"), rm=("rk", "sum")).reset_index()
    return [[r.unit_id, r.m, r.channel, int(r.n), int(r.nk), _r(r.g), _r(r.nt), _r(r.rm)] for r in g.itertuples()]


def monthly_supply(night: pd.DataFrame) -> list:
    n = night.copy()
    n["m"] = n.date.dt.strftime("%Y-%m")
    t = pd.crosstab([n.unit_id, n.m], n.state)
    for c in ("maintenance", "personal", "unknown", "open", "stay"):
        if c not in t:
            t[c] = 0
    t["cal"] = t.sum(axis=1)
    t = t.reset_index()
    return [[r.unit_id, r.m, int(r.cal), int(r.maintenance), int(r.personal), int(r.unknown), int(r.open)]
            for r in t.itertuples()]


def booking_events(bookings: pd.DataFrame) -> list:
    """One row per booking: unit, channel, checkin month, status, nights, lead days, net."""
    b = bookings[bookings.channel != "direct"].copy()
    lead = (b.checkin - b.booked_date).dt.days
    out = []
    for r, ld in zip(b.itertuples(), lead):
        out.append([r.unit_id, r.channel, r.checkin.strftime("%Y-%m"),
                    1 if r.status == "cancelled" else 0, int(r.nights),
                    None if pd.isna(ld) else int(max(ld, 0)),
                    _r(r.net_revenue if r.status == "cancelled" else None)])
    return out


def gaps(night: pd.DataFrame, as_of: pd.Timestamp, max_len: int = 14) -> list:
    """Unbooked runs of open nights with a stay on both sides.
    Returns [unit, start, nights, upcoming(0/1), weekend_nights]."""
    out = []
    for u, d in night.sort_values("date").groupby("unit_id"):
        st = d.state.to_numpy()
        dates = d.date.to_numpy()
        i, n = 0, len(st)
        while i < n:
            if st[i] != "open":
                i += 1
                continue
            j = i
            while j < n and st[j] == "open":
                j += 1
            before = i > 0 and st[i - 1] == "stay"
            after = j < n and st[j] == "stay"
            length = j - i
            if before and after and length <= max_len:
                start = pd.Timestamp(dates[i])
                wk = sum(pd.Timestamp(x).dayofweek in (4, 5) for x in dates[i:j])  # Fri/Sat nights
                out.append([u, start.strftime("%Y-%m-%d"), int(length), int(start >= as_of), int(wk)])
            i = j
    return out


def forward_strip(night: pd.DataFrame, as_of: pd.Timestamp, days: int = 120) -> dict:
    f = night[(night.date >= as_of) & (night.date < as_of + pd.Timedelta(days=days))]
    out = {}
    for u, d in f.sort_values("date").groupby("unit_id"):
        codes = [CH_CODE.get(c, "S") if s == "stay" else STATE_CODE[s] for s, c in zip(d.state, d.channel)]
        out[u] = "".join(codes)
    return out


def fee_history(bookings: pd.DataFrame) -> list:
    """Platform fee as % of gross, by channel and quarter (confirmed, reported revenue only)."""
    b = bookings[(bookings.status == "confirmed") & bookings.channel.isin(["airbnb", "agoda", "bookingcom"])
                 & bookings.gross_revenue.gt(0)].copy()
    b["q"] = b.checkin.dt.to_period("Q").astype(str)
    b["room"] = b.gross_revenue - b.cleaning_fee.fillna(0)
    g = b.groupby(["channel", "q"]).agg(gross=("gross_revenue", "sum"), fee=("platform_fee", "sum"),
                                        est=("revenue_estimated", "mean"), n=("booking_id", "size")).reset_index()
    return [[r.channel, r.q, _r(r.fee / r.gross * 100, 2), int(r.n), _r(r.est, 2)] for r in g.itertuples() if r.n >= 3]


def adjustments(adj: pd.DataFrame) -> list:
    if adj is None or adj.empty:
        return []
    a = adj.dropna(subset=["unit_id", "date"]).copy()
    a["m"] = pd.to_datetime(a.date).dt.strftime("%Y-%m")
    g = a.groupby(["unit_id", "m", "channel"]).amount.sum().reset_index()
    return [[r.unit_id, r.m, r.channel, _r(r.amount)] for r in g.itertuples() if abs(r.amount) > 0.005]
