"""Agoda YCS Finance > Transactions.

Accepts either the API pull (columns: prop, txn_id, type, booking_id, created,
checkin, checkout, booking_value, payout, updated) or the portal's own
Excel/CSV export (one file per property; property id taken from the file name).

Status rules (verified against Agoda's booking statuses):
  one row, payout > 0          -> confirmed
  one row, payout == 0         -> cancelled, free
  several rows netting to ~0   -> cancelled after being charged
  several rows netting > 0     -> confirmed if the stay nights still stand
                                  (amendment), else cancelled with fee kept

Fees: Agoda pays  booking_value * (1 - commission) + cleaning_fee.
The cleaning fee is a flat amount per stay that Agoda passes through without
commission. We recover commission and cleaning fee per unit and half-year by
a least-squares fit on confirmed single-row bookings.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from ..schema import ADJ_COLS, BOOKING_COLS


def _read_api_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"prop": str, "booking_id": str, "txn_id": str})
    df["type"] = df["type"].replace({"R": "Reservation"})
    return df


def _read_portal_export(path: Path) -> pd.DataFrame:
    """Agoda's Excel export: title rows, header on row 5, two-row records."""
    raw = pd.read_excel(path, header=None, dtype=object)
    hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip().eq("Transaction type")][0]
    body = raw.iloc[hdr + 1:, :9].dropna(how="all")
    body = body[body.iloc[:, 0].notna() & ~body.iloc[:, 0].astype(str).str.startswith("or ")]
    body.columns = ["type", "booking_id", "created", "checkin", "checkout",
                    "method", "currency", "booking_value", "payout"]
    m = re.search(r"(\d{6,})", path.stem)
    body["prop"] = m.group(1) if m else path.stem
    body["txn_id"] = [f"{path.stem}-{i}" for i in range(len(body))]
    body["booking_id"] = body["booking_id"].astype(str).str.replace(r"\.0$", "", regex=True)
    return body


def load(paths, unit_lookup: dict[str, str]):
    """Return (bookings, adjustments). unit_lookup maps Agoda property id -> unit_id."""
    frames = []
    for p in ([paths] if isinstance(paths, (str, Path)) else paths):
        p = Path(p)
        frames.append(_read_portal_export(p) if p.suffix.lower() in (".xlsx", ".xls") else _read_api_csv(p))
    tx = pd.concat(frames, ignore_index=True).drop_duplicates("txn_id")
    for c in ("created", "checkin", "checkout"):
        tx[c] = pd.to_datetime(tx[c]).dt.normalize()
    for c in ("booking_value", "payout"):
        tx[c] = pd.to_numeric(tx[c], errors="coerce").fillna(0.0)
    tx["unit_id"] = tx["prop"].astype(str).map(unit_lookup)
    tx = tx[tx["unit_id"].notna()]

    # ---- adjustments (refunds/penalties raised outside a reservation) ----
    adj = tx[tx["type"] != "Reservation"]
    adjustments = pd.DataFrame({
        "channel": "agoda", "unit_id": adj["unit_id"], "date": adj["created"],
        "amount": adj["payout"], "ref": adj["booking_id"],
    })[ADJ_COLS]

    # ---- reservations ----
    r = tx[tx["type"] == "Reservation"]
    g = r.groupby(["unit_id", "booking_id"], as_index=False).agg(
        booked_date=("created", "min"), checkin=("checkin", "first"), checkout=("checkout", "first"),
        bv_first=("booking_value", "first"), bv_sum=("booking_value", "sum"),
        payout=("payout", "sum"), rows=("txn_id", "size"), max_payout=("payout", "max"),
    )
    g["nights"] = (g["checkout"] - g["checkin"]).dt.days
    single_paid = (g["rows"] == 1) & (g["payout"] > 0)
    reversed_zero = (g["rows"] > 1) & (g["payout"].abs() < 0.01)
    free_cancel = (g["rows"] == 1) & (g["payout"] == 0)
    multi_net = (g["rows"] > 1) & ~reversed_zero
    # a multi-row booking with positive net: amendment if net is close to the
    # original payout (stay kept), otherwise a cancellation that kept a fee.
    amended = multi_net & (g["payout"] >= 0.6 * g["max_payout"])
    g["status"] = np.where(single_paid | amended, "confirmed", "cancelled")

    # ---- fee model fit ----
    g["half"] = g["booked_date"].dt.year.astype(str) + "H" + np.where(g["booked_date"].dt.month <= 6, "1", "2")
    g["year"] = g["booked_date"].dt.year
    fit_src = g[single_paid & (g["bv_first"] > 0)]

    def _fit(d):
        if len(d) < 5 or d["bv_first"].nunique() < 2:
            return None
        A = np.vstack([d["bv_first"], np.ones(len(d))]).T
        (a, f), *_ = np.linalg.lstsq(A, d["payout"], rcond=None)
        if not (0.6 < a <= 1.0) or not (0 <= f <= 150):
            return None
        return 1 - a, f

    fits = {}
    for level, keys in (("half", ["unit_id", "half"]), ("year", ["unit_id", "year"]), ("unit", ["unit_id"])):
        for k, d in fit_src.groupby(keys):
            res = _fit(d)
            if res:
                fits[(level,) + (k if isinstance(k, tuple) else (k,))] = res

    def _lookup(row):
        for level, key in (("half", (row.unit_id, row.half)), ("year", (row.unit_id, row.year)), ("unit", (row.unit_id,))):
            if (level,) + key in fits:
                return fits[(level,) + key]
        return (0.18, 0.0)

    cf = g.apply(_lookup, axis=1, result_type="expand")
    g["commission"], g["clean_fit"] = cf[0], cf[1]

    room = g["bv_sum"].where(g["status"] == "confirmed", 0.0)
    # cleaning fee for this booking: what's left of the payout after the commissioned room part
    clean = (g["payout"] - room * (1 - g["commission"])).clip(lower=0)
    clean = clean.where((clean - g["clean_fit"]).abs() <= 15, g["clean_fit"])  # guard odd rows
    confirmed = g["status"] == "confirmed"
    g["cleaning_fee"] = np.where(confirmed, clean.round(2), 0.0)
    g["gross_revenue"] = np.where(confirmed, room + g["cleaning_fee"], g["payout"].clip(lower=0))
    g["net_revenue"] = np.where(confirmed, g["payout"], g["payout"].clip(lower=0))
    g["platform_fee"] = (g["gross_revenue"] - g["net_revenue"]).clip(lower=0).round(2)
    g["revenue_estimated"] = confirmed & ~((g["rows"] == 1))  # amended rows: split is approximate
    g["revenue_known"] = True
    g["channel"] = "agoda"
    g["booking_id"] = "AG-" + g["booking_id"].astype(str)
    return g[BOOKING_COLS], adjustments
