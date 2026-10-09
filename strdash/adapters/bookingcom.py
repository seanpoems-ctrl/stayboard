"""Booking.com reservations.

Accepts Booking.com's extranet reservations export (Book number, Arrival,
Departure, Status, Price, Commission amount, Booked on) or a host-kept sheet
with Check-in / Check-out / Gross Revenue / Commission / Tax / Cleaning Fee.
A 'unit' column (Booking.com property id or unit id) maps rows to units;
otherwise pass default_unit.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..schema import BOOKING_COLS


def _col(df, *names):
    for n in names:
        for c in df.columns:
            if c.strip().lower() == n.lower():
                return df[c]
    return pd.Series(np.nan, index=df.index)


def _num(s):
    return pd.to_numeric(s.astype(str).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")


def load(path, unit_lookup: dict[str, str] | None = None, default_unit: str | None = None):
    df = pd.read_csv(path, dtype=str)
    unit_raw = _col(df, "unit", "property id", "property")
    unit = unit_raw.map(unit_lookup or {}) if unit_lookup else unit_raw
    if default_unit:
        unit = unit.fillna(default_unit)
    checkin = pd.to_datetime(_col(df, "check-in", "arrival"), errors="coerce", format="mixed")
    checkout = pd.to_datetime(_col(df, "check-out", "departure"), errors="coerce", format="mixed")
    status_raw = _col(df, "result", "status").astype(str).str.lower()
    status = np.where(status_raw.str.contains("cancel|no.show"), "cancelled", "confirmed")
    gross = _num(_col(df, "gross revenue (myr)", "gross revenue", "price"))
    comm = _num(_col(df, "booking.com commission (myr)", "commission amount", "commission"))
    tax = _num(_col(df, "tax on commission")).fillna(0)
    clean = _num(_col(df, "cleaning fee"))
    fee = comm.fillna(0) + tax
    out = pd.DataFrame({
        "booking_id": "BK-" + _col(df, "book number", "book number ").astype(str).str.strip(),
        "channel": "bookingcom", "unit_id": unit,
        "booked_date": pd.to_datetime(_col(df, "booked on"), errors="coerce", format="mixed"),
        "checkin": checkin, "checkout": checkout, "nights": (checkout - checkin).dt.days,
        "status": status, "gross_revenue": gross, "cleaning_fee": clean,
        "platform_fee": fee.round(2), "net_revenue": (gross - fee).round(2),
        "revenue_estimated": False, "revenue_known": gross.notna(),
    })
    dropped = int(out["checkin"].isna().sum() + out["unit_id"].isna().sum())
    out = out[out["checkin"].notna() & out["unit_id"].notna()]
    return out[BOOKING_COLS], dropped
