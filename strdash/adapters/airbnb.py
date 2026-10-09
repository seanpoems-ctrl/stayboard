"""Airbnb.

reservations: either the API pull (code, listing_id, booked, start, end, nights,
              earnings, status, ...) or Airbnb's Reservations CSV export
              (Confirmation code, Status, Start date, End date, # of nights,
              Booked, Listing, Earnings).
transactions: Airbnb's Earnings > Transaction history CSV (optional). Gives the
              gross / service fee / cleaning fee split per reservation.

earnings in the reservations data is the host payout, i.e. net revenue.
Where the transaction file covers a booking we use its exact gross, cleaning
and service fee; otherwise gross is estimated from net with the configured fee
rate and the booking is flagged revenue_estimated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..schema import ADJ_COLS, BOOKING_COLS

_EXPORT_RENAME = {  # Airbnb's own Reservations CSV -> API names
    "Confirmation code": "code", "Status": "status", "Start date": "start", "End date": "end",
    "# of nights": "nights", "Booked": "booked", "Listing": "listing", "Earnings": "earnings",
}


def _money(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")


def load(res_path, unit_lookup: dict[str, str], name_lookup: dict[str, str] | None = None,
         tx_path=None, fee_rate: float = 0.0324):
    """unit_lookup: Airbnb listing id -> unit_id. name_lookup: listing name -> unit_id
    (used when the file has no listing id, e.g. Airbnb's own CSV export)."""
    r = pd.read_csv(res_path, encoding="utf-8-sig", dtype=str).rename(columns=_EXPORT_RENAME)
    if "listing_id" in r:
        r["unit_id"] = r["listing_id"].map(unit_lookup)
    else:
        r["unit_id"] = r["listing"].map(name_lookup or {})
    r = r[r["unit_id"].notna()].copy()
    for c in ("booked", "start", "end"):
        r[c] = pd.to_datetime(r[c], errors="coerce")
    r["net"] = _money(r["earnings"]).fillna(0.0)
    st = r["status"].str.lower()
    r["status_std"] = np.where(st.str.contains("cancel"), "cancelled", "confirmed")

    out = pd.DataFrame({
        "booking_id": "AB-" + r["code"], "channel": "airbnb", "unit_id": r["unit_id"],
        "booked_date": r["booked"], "checkin": r["start"], "checkout": r["end"],
        "nights": (r["end"] - r["start"]).dt.days, "status": r["status_std"],
        "net_revenue": r["net"].clip(lower=0),
    })

    adjustments = pd.DataFrame(columns=ADJ_COLS)
    detail = None
    if tx_path is not None:
        t = pd.read_csv(tx_path, encoding="utf-8-sig", dtype=str)
        t = t.drop(columns=[c for c in ("Guest", "Details", "Reference code") if c in t])
        for c in ("Amount", "Service fee", "Cleaning fee", "Gross earnings"):
            t[c] = _money(t[c])
        res = t[t["Type"] == "Reservation"]
        detail = res.groupby("Confirmation code").agg(
            gross=("Gross earnings", "sum"), fee=("Service fee", "sum"),
            cleaning=("Cleaning fee", "sum"), amount=("Amount", "sum"))
        detail.index = "AB-" + detail.index
        other = t[~t["Type"].isin(["Reservation", "Payout"])].copy()
        if len(other):
            code_unit = dict(zip("AB-" + r["code"], r["unit_id"]))
            adjustments = pd.DataFrame({
                "channel": "airbnb", "unit_id": ("AB-" + other["Confirmation code"]).map(code_unit),
                "date": pd.to_datetime(other["Date"], format="%m/%d/%Y", errors="coerce"),
                "amount": other["Amount"], "ref": other["Type"] + " " + other["Confirmation code"].fillna(""),
            })[ADJ_COLS]

    out = out.set_index("booking_id")
    if detail is not None:
        d = detail.reindex(out.index)
        have = d["gross"].notna() & (out["status"] == "confirmed")
    else:
        d, have = None, pd.Series(False, index=out.index)
    # Fee rate for bookings without a reported split: follow what Airbnb actually
    # charged on bookings made around the same time (Airbnb changes fee models,
    # e.g. split fee ~3% -> host-only fee ~16%). Rolling median of the last 5
    # reported bookings by booking date; fall back to the configured rate.
    rate = pd.Series(fee_rate, index=out.index, dtype=float)
    if d is not None and have.any():
        ref = pd.DataFrame({"booked": out.loc[have, "booked_date"],
                            "r": (d.loc[have, "fee"] / d.loc[have, "gross"]).astype(float)}).dropna().sort_values("booked")
        ref["r"] = ref["r"].rolling(5, min_periods=1).median()
        q = out.loc[~have, ["booked_date"]].reset_index().sort_values("booked_date")
        m = pd.merge_asof(q.dropna(subset=["booked_date"]), ref.rename(columns={"booked": "booked_date"}),
                          on="booked_date", direction="backward")
        rate.loc[m["booking_id"]] = m["r"].fillna(fee_rate).values
    est_gross = out["net_revenue"] / (1 - rate)
    conf = out["status"] == "confirmed"
    out["gross_revenue"] = np.where(have, d["gross"] if d is not None else np.nan,
                                    np.where(conf, est_gross, out["net_revenue"]))
    out["platform_fee"] = np.where(have, d["fee"] if d is not None else np.nan,
                                   np.where(conf, est_gross - out["net_revenue"], 0.0))
    out["cleaning_fee"] = np.where(have, d["cleaning"] if d is not None else np.nan, np.nan)
    out["revenue_estimated"] = conf & ~have
    out["revenue_known"] = True
    out = out.reset_index()
    for c in ("gross_revenue", "platform_fee"):
        out[c] = out[c].astype(float).round(2)
    return out[BOOKING_COLS], adjustments
