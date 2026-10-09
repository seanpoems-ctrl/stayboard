"""Build clean tables and the dashboard data file for one host.

    python -m strdash.build config/host.json

Each source in config['sources'] is optional; a host with only Airbnb works.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import metrics, model, public
from .adapters import agoda, airbnb, bookingcom, calendar_notes
from .schema import ADJ_COLS, BLOCK_COLS, BOOKING_COLS


def _fill_airbnb_cleaning(b: pd.DataFrame) -> pd.DataFrame:
    """Airbnb data before the transaction export has no cleaning fee. The host
    charges the same cleaning fee on every channel, so borrow the unit's Agoda
    cleaning fee from the same half-year (falling back to the nearest one)."""
    ag = b[(b.channel == "agoda") & (b.status == "confirmed") & b.cleaning_fee.gt(0)].copy()
    if ag.empty:
        return b
    ag["half"] = ag.checkin.dt.to_period("Q").dt.qyear.astype(str) + "H" + np.where(ag.checkin.dt.month <= 6, "1", "2")
    ref = ag.groupby(["unit_id", "half"]).cleaning_fee.median().round(0)
    need = (b.channel == "airbnb") & (b.status == "confirmed") & b.cleaning_fee.isna()
    half = b.checkin.dt.year.astype(str) + "H" + np.where(b.checkin.dt.month <= 6, "1", "2")
    vals = []
    for u, h in zip(b.loc[need, "unit_id"], half[need]):
        if (u, h) in ref.index:
            vals.append(ref[(u, h)])
            continue
        hs = ref[u] if u in ref.index.get_level_values(0) else None
        if hs is None or hs.empty:
            vals.append(np.nan)
            continue
        # nearest half-year
        order = sorted(hs.index, key=lambda x: abs(int(x[:4]) * 2 + int(x[-1]) - (int(h[:4]) * 2 + int(h[-1]))))
        vals.append(hs[order[0]])
    b.loc[need, "cleaning_fee"] = vals
    # cleaning can't exceed the stay's gross
    b["cleaning_fee"] = np.minimum(b.cleaning_fee, b.gross_revenue)
    return b


def run(cfg_path: str):
    cfg_path = Path(cfg_path)
    root = cfg_path.parent.parent
    cfg = json.loads(cfg_path.read_text())
    src = {k: root / v for k, v in cfg["sources"].items()}
    units = cfg["units"]
    as_of = pd.Timestamp(cfg["as_of"])
    log = []

    ag_map = {u["agoda_id"]: u["id"] for u in units if u.get("agoda_id")}
    ab_map = {u["airbnb_id"]: u["id"] for u in units if u.get("airbnb_id")}
    delisted = {u["id"]: u["airbnb_delisted_after"] for u in units if u.get("airbnb_delisted_after")}

    parts, adjs = [], []
    if "agoda_transactions" in src:
        b, a = agoda.load(src["agoda_transactions"], ag_map)
        parts.append(b); adjs.append(a)
        log.append(f"agoda: {len(b)} bookings, {len(a)} adjustments")
    if "airbnb_reservations" in src:
        b, a = airbnb.load(src["airbnb_reservations"], ab_map, tx_path=src.get("airbnb_transactions"),
                           fee_rate=cfg.get("airbnb_estimated_fee_rate", 0.0324))
        parts.append(b); adjs.append(a)
        log.append(f"airbnb: {len(b)} bookings ({int((~b.revenue_estimated & (b.status=='confirmed')).sum())} with exact fee split)")
    if "bookingcom" in src:
        b, dropped = bookingcom.load(src["bookingcom"], ag_map)
        parts.append(b)
        log.append(f"booking.com: {len(b)} bookings ({dropped} rows without dates skipped)")
    blocks = pd.DataFrame(columns=BLOCK_COLS)
    if "airbnb_calendar" in src:
        d, blocks = calendar_notes.load(src["airbnb_calendar"], ab_map, delisted, cfg.get("direct_notes_start", "2023-01-01"))
        parts.append(d)
        log.append(f"calendar: {len(d)} direct stays from notes ({int(d.revenue_known.sum())} priced), {len(blocks)} blocked nights")

    bookings = pd.concat([p for p in parts if len(p)], ignore_index=True)
    bookings = bookings[bookings.nights > 0]
    before = (bookings.channel == "direct").sum()
    bookings = model.drop_shadow_direct(bookings)
    log.append(f"direct notes dropped as annotations of platform stays: {before - (bookings.channel == 'direct').sum()}")
    bookings = _fill_airbnb_cleaning(bookings)
    adjustments = pd.concat([a for a in adjs if len(a)], ignore_index=True) if adjs else pd.DataFrame(columns=ADJ_COLS)

    night, dups = model.nightly(bookings, blocks, units, as_of, cfg.get("forward_days", 180))
    log.append(f"nightly rows: {len(night)}; overlapping stay-nights resolved: {dups}")

    out_clean = root / "data" / "clean"
    out_clean.mkdir(parents=True, exist_ok=True)
    bookings.sort_values(["unit_id", "checkin"]).to_csv(out_clean / "bookings.csv", index=False)
    blocks.to_csv(out_clean / "blocks.csv", index=False)
    adjustments.to_csv(out_clean / "adjustments.csv", index=False)
    night.to_csv(out_clean / "nightly.csv.gz", index=False, compression="gzip")

    active = [u for u in units if u.get("active")]
    is_public = bool(cfg.get("public_mode"))
    names = public.labels(active, is_public)
    live_from = night.groupby("unit_id").date.min().dt.strftime("%Y-%m-%d").to_dict()
    data = {
        "meta": {
            "host": cfg.get("public_host_name", "Demo Host") if is_public else cfg.get("host_name", ""), "currency": cfg.get("currency", "MYR"),
            "as_of": cfg["as_of"], "built": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "direct_from": cfg.get("direct_notes_start"),
            "units": [{**public.unit_meta(u, names[u["id"]], is_public), "live_from": live_from.get(u["id"])} for u in active],
            "channels": sorted(bookings.channel.unique().tolist()),
        },
        "stays": metrics.monthly_stays(night),
        "supply": metrics.monthly_supply(night),
        "events": metrics.booking_events(bookings[bookings.unit_id.isin(names)]),
        "gaps": metrics.gaps(night, as_of),
        "strip": metrics.forward_strip(night, as_of),
        "fees": metrics.fee_history(bookings[bookings.unit_id.isin(names)]),
        "adjust": metrics.adjustments(adjustments[adjustments.unit_id.isin(names)]),
    }
    if is_public:
        data = public.trim_history(data, cfg["as_of"], cfg.get("public_months", 24))
    text = json.dumps(data, separators=(",", ":"))
    if is_public:
        leaks = public.check(text, units)
        if leaks:
            raise SystemExit(f"Public build stopped, private names found: {', '.join(leaks)}")
    site = root / (cfg.get("public_output_dir", "docs") if is_public else cfg.get("output_dir", "private"))
    site.mkdir(exist_ok=True)
    shutil.copyfile(root / "strdash" / "index.html", site / "index.html")
    (site / "data.json").write_text(text)
    log.append(f"data.json: {(site / 'data.json').stat().st_size/1024:.0f} KB")
    print("\n".join(log))
    return bookings, blocks, night, adjustments


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "config/host.json")
