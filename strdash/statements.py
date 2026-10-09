"""Monthly owner statements, one per owner (or owner group), in the layout of
the host's own revenue sheet.

    python -m strdash.statements config/host.json 2026-09

Reads the clean tables written by strdash.build (data/clean/) and writes one
printable page per statement to private/statements/<month>/ plus summary.csv
(management fee per unit). Statements stay in private/ and are never published.

Config:
  "statements_start": "2026-08",            # deficits carry forward from here
  "statements": { "<id>": {"owner": "Owner A", "title": "City units",
                           "monthly": {"Google Ad": 150}} }      # statement-level charges
  units[].owner_terms: {"statement": "<id>", "cleaning_cost": 45, "vacancy_wipe": 10,
                        "monthly": {"Season parking": 88, "Netflix": 29.63,
                                    "Broadband": {"amount": 108.80, "from": "2026-09"}}}
  owner_defaults: {"commission": 0.20, "vacancy_wipe": 10, "vacancy_after_nights": 3,
                   "month_basis": "checkin"}

Rules (as in the host's revenue sheets):
- A stay belongs to the month it checks in (month_basis "checkout" switches this).
- Gross + cleaning = what Airbnb/Agoda paid after their fee (direct: the price).
- Cleaning per stay = the unit's cleaning cost, plus the vacancy wipe when the unit
  sat empty for at least vacancy_after_nights before the stay.
- Gross = gross + cleaning - cleaning. Commission = rate x gross, per stay.
- Expenses: statement-level and per-unit monthly charges, commission, cleaning, and
  one-off items from data/raw/owner_expenses.csv (month,unit_id,category,item,amount;
  category defaults to Maintenance).
- A negative net payout is carried into the next month. The opening deficit for
  statements_start goes in data/raw/owner_balances.csv (month,statement,amount).
- Direct stays whose note had no price are flagged; add them in
  data/raw/direct_prices.csv (unit_id,checkin,amount) and rerun.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import pandas as pd

CH = {"airbnb": "Airbnb", "agoda": "Agoda", "direct": "Direct", "bookingcom": "Booking.com"}


def _read_csv(p: Path, **kw):
    return pd.read_csv(p, **kw) if p.exists() else None


def load(root: Path):
    clean = root / "data" / "clean"
    b = pd.read_csv(clean / "bookings.csv", parse_dates=["checkin", "checkout"])
    n = pd.read_csv(clean / "nightly.csv.gz", parse_dates=["date"])
    a = _read_csv(clean / "adjustments.csv", parse_dates=["date"])
    x = _read_csv(root / "data" / "raw" / "owner_expenses.csv")
    dp = _read_csv(root / "data" / "raw" / "direct_prices.csv", parse_dates=["checkin"])
    return b, n, a, x, dp


def empty_before(night: pd.DataFrame, unit: str) -> dict:
    """Consecutive open nights immediately before each date, per unit."""
    u = night[night.unit_id == unit].sort_values("date")
    out, run = {}, 0
    for d, st in zip(u.date, u.state):
        out[d] = run
        run = run + 1 if st == "open" else 0
    return out


def unit_month(unit: dict, month: str, b, night, adj, direct_prices, d: dict) -> dict:
    t = {**d, **unit.get("owner_terms", {})}
    uid = unit["id"]
    basis = "checkin" if t.get("month_basis", "checkin") == "checkin" else "checkout"
    s = b[(b.unit_id == uid) & (b[basis].dt.to_period("M") == pd.Period(month, "M"))].copy()
    s = s[(s.status == "confirmed") | (s.net_revenue.fillna(0) > 0)]
    s["revenue_known"] = s.revenue_known.astype(bool)
    if direct_prices is not None and len(s):
        dp = direct_prices[direct_prices.unit_id == uid].set_index("checkin").amount
        fill = (s.channel == "direct") & ~s.revenue_known & s.checkin.isin(dp.index)
        s.loc[fill, "net_revenue"] = s.loc[fill, "checkin"].map(dp)
        s.loc[fill, "revenue_known"] = True
    gap = empty_before(night, uid)
    cost, wipe, after = float(t.get("cleaning_cost", 0)), float(t["vacancy_wipe"]), int(t["vacancy_after_nights"])

    rows = []
    for r in s.sort_values("checkin").itertuples():
        confirmed = r.status == "confirmed"
        unpriced = r.channel == "direct" and not r.revenue_known
        payout = 0.0 if unpriced else float(r.net_revenue or 0)
        wiped = confirmed and gap.get(r.checkin, 0) >= after
        clean = (cost + (wipe if wiped else 0.0)) if confirmed else 0.0
        room = payout - clean
        comm = round(max(room, 0) * float(t["commission"]), 2)
        rows.append({"checkin": r.checkin, "checkout": r.checkout, "nights": int(r.nights),
                     "channel": CH.get(r.channel, r.channel), "cancelled": not confirmed, "unpriced": unpriced,
                     "payout": round(payout, 2), "room": round(room, 2), "cleaning": round(clean, 2), "wiped": wiped,
                     "commission": comm, "net": round(room - comm, 2)})
    if adj is not None and len(adj):
        aa = adj[(adj.unit_id == uid) & (adj.date.dt.to_period("M") == pd.Period(month, "M"))]
        for r in aa.itertuples():
            amt = float(r.amount)
            comm = round(amt * float(t["commission"]), 2)
            rows.append({"checkin": r.date, "checkout": r.date, "nights": 0, "channel": f"{CH.get(r.channel, r.channel)} adjustment",
                         "cancelled": False, "unpriced": False, "payout": amt, "room": amt, "cleaning": 0.0, "wiped": False,
                         "commission": comm, "net": round(amt - comm, 2)})
    wipes_n = sum(r["wiped"] for r in rows)
    nm = night[(night.unit_id == uid) & (night.date.dt.strftime("%Y-%m") == month)]
    return {
        "unit": unit, "name": unit.get("name", uid), "short": unit.get("short", uid), "rows": rows,
        "payout": round(sum(r["payout"] for r in rows), 2),
        "room": round(sum(r["room"] for r in rows), 2),
        "cleaning_fees": round(sum(r["cleaning"] for r in rows), 2),
        # as in the sheets: rate x the unit's total gross, rounded once
        "commission": round(max(sum(r["room"] for r in rows), 0) * float(t["commission"]) + 1e-9, 2),
        "nights": sum(r["nights"] for r in rows if not r["cancelled"]),
        "wipes_n": wipes_n,
        "monthly": t.get("monthly") or {},
        "unpriced": sum(r["unpriced"] for r in rows),
        "sold": int((nm.state == "stay").sum()), "avail": int(nm.state.isin(["stay", "open"]).sum()),
    }


def _charge(amt, month: str):
    """A monthly charge is a number, null (not set yet), or
    {"amount": x, "from": "YYYY-MM", "until": "YYYY-MM"}. Returns (applies, amount)."""
    if isinstance(amt, dict):
        if month < amt.get("from", "0000-00") or month > amt.get("until", "9999-99"):
            return False, 0.0
        amt = amt.get("amount")
    return True, (None if amt is None else float(amt))


def build_statement(sid: str, meta: dict, units: list[dict], month: str, data, d: dict, carried: float = 0.0) -> dict:
    """carried: last month's deficit (negative net payout), deducted this month."""
    b, night, adj, exp, dp = data
    us = [unit_month(u, month, b, night, adj, dp, d) for u in units]
    sections: list[tuple[str, list[tuple[str, float]]]] = []
    missing = []

    def add(cat, label, amt):
        for c, lines in sections:
            if c == cat:
                lines.append((label, amt)); return
        sections.append((cat, [(label, amt)]))

    for item, amt in (meta.get("monthly") or {}).items():
        ok, amt = _charge(amt, month)
        if not ok: continue
        if amt is None: missing.append(item)
        else: add(item, item, amt)
    for u in us:
        add("Commission", u["short"], u["commission"])
    for u in us:
        label = u["short"] + (f" (incl. {u['wipes_n']} vacancy wipe{'s' if u['wipes_n'] > 1 else ''})" if u["wipes_n"] else "")
        add("Cleaning", label, u["cleaning_fees"])
    items = []
    for u in us:
        for item, amt in u["monthly"].items():
            ok, amt = _charge(amt, month)
            if not ok: continue
            if amt is None: missing.append(f"{item} ({u['short']})")
            else: items.append((item, u["short"], amt))
    for item in dict.fromkeys(i for i, _, _ in items):
        if item == "Cleaning supplies":
            continue
        for _, s, a in (x for x in items if x[0] == item):
            add(item, s, a)
    if exp is not None and len(exp):
        ids = {u["unit"]["id"]: u["short"] for u in us}
        ee = exp[(exp.month == month) & (exp.unit_id.isin(list(ids) + [sid]))]
        for r in ee.itertuples():
            cat = getattr(r, "category", None)
            cat = cat if isinstance(cat, str) and cat else "Maintenance"
            who = ids.get(r.unit_id, "")
            add(cat, f"{who} {r.item}".strip(), float(r.amount))
    for _, s, a in (x for x in items if x[0] == "Cleaning supplies"):
        add("Cleaning supplies", s, a)

    revenue = round(sum(u["payout"] for u in us), 2)
    expenses = round(sum(a for _, lines in sections for _, a in lines), 2)
    return {"id": sid, "owner": meta.get("owner", ""), "title": meta.get("title", ""), "month": month,
            "units": us, "basis": d.get("month_basis", "checkin"), "revenue": revenue, "sections": sections, "expenses": expenses, "carried": round(carried, 2),
            "net_payout": round(revenue + carried - expenses, 2), "missing": missing,
            "unpriced": sum(u["unpriced"] for u in us)}


def _rm(v: float) -> str:
    return f"−RM{abs(v):,.2f}" if v < 0 else f"RM{v:,.2f}"


def render(st: dict, manager: str = "") -> str:
    e = html.escape
    ml = pd.Period(st["month"], "M").strftime("%b %Y")
    unit_html = ""
    for u in st["units"]:
        body = "".join(
            f"<tr{' class=\"muted\"' if r['cancelled'] else ''}><td>{r['checkin'].day}</td><td>{r['checkout'].day}</td><td class='n'>{r['nights'] or ''}</td>"
            f"<td class='n'>{'<b class=\"warn\">price needed</b>' if r['unpriced'] else f'{r['payout']:,.2f}'}</td>"
            f"<td class='n'>{r['room']:,.2f}</td><td class='n'>{r['cleaning']:,.2f}</td><td class='n'>{r['commission']:,.2f}</td>"
            f"<td class='n'>{r['net']:,.2f}</td><td>{e(r['channel'])}{' · cancelled, paid out' if r['cancelled'] else ''}</td></tr>"
            for r in u["rows"]) or "<tr><td colspan=9 class='muted'>No stays checked out this month.</td></tr>"
        occ = f"{u['sold'] / u['avail']:.0%}" if u["avail"] else "–"
        unit_html += f"""<section><h2>{e(u['name'])}</h2>
<table class="stays"><tr><th>Check in</th><th>Check out</th><th class='n'>Nights</th><th class='n'>Gross + cleaning</th><th class='n'>Gross</th><th class='n'>Cleaning</th><th class='n'>Commission</th><th class='n'>Net</th><th>OTA</th></tr>{body}
<tr class="tot"><td colspan=2></td><td class='n'>{u['nights']}</td><td class='n'>{u['payout']:,.2f}</td><td class='n'>{u['room']:,.2f}</td><td class='n'>{u['cleaning_fees']:,.2f}</td><td class='n'>{u['commission']:,.2f}</td><td class='n'>{u['room'] - u['commission']:,.2f}</td><td></td></tr></table>
<p class="sub">Occupancy {occ} · {u['sold']} of {u['avail']} nights sold in {ml}</p></section>"""

    rev = "".join(f"<tr><td>{e(u['short'])}</td><td class='n'>{_rm(u['payout'])}</td></tr>" for u in st["units"])
    exp = ""
    for cat, lines in st["sections"]:
        if len(lines) == 1 and lines[0][0] == cat:
            exp += f"<tr><td>{e(cat)}</td><td class='n'>{_rm(lines[0][1])}</td></tr>"
        else:
            exp += f"<tr class='cat'><td colspan=2>{e(cat)}</td></tr>" + "".join(
                f"<tr><td class='ind'>{e(l)}</td><td class='n'>{_rm(a)}</td></tr>" for l, a in lines)
    notes = []
    if st["unpriced"]:
        notes.append(f"{st['unpriced']} direct stay(s) have no price recorded and count as RM0 until added.")
    if st["missing"]:
        notes.append("Not yet deducted (amount not set): " + ", ".join(map(e, st["missing"])) + ".")
    notes_html = "".join(f"<p class='warn'>{n}</p>" for n in notes)
    head = " · ".join(x for x in [st["owner"], st["title"]] if x)
    prev = (pd.Period(st["month"], "M") - 1).strftime("%B")
    carry_html = f"<tr><td>{prev} deficit carried forward</td><td class='n'>{_rm(st['carried'])}</td></tr>" if st["carried"] else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(head)} · {ml}</title>
<style>
:root {{ --ink:#1c1c1a; --mute:#6b6a65; --line:#e3e1da; --bg:#fff; --warn:#a4400f; }}
* {{ box-sizing:border-box; }}
body {{ font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg); margin:0; padding:32px 16px; }}
main {{ max-width:860px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 2px; }}
h2 {{ font-size:15px; margin:28px 0 8px; }}
.sub {{ color:var(--mute); margin:4px 0 0; font-size:12.5px; }}
.wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
td, th {{ padding:6px 8px 6px 0; border-bottom:1px solid var(--line); text-align:left; }}
th {{ font-weight:500; color:var(--mute); font-size:12px; vertical-align:bottom; }}
.stays {{ min-width:640px; font-size:13px; }}
.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
tr.tot td {{ font-weight:600; border-top:2px solid var(--ink); }}
tr.cat td {{ font-weight:600; padding-top:12px; }} td.ind {{ padding-left:16px; }}
tr.total td {{ font-weight:700; font-size:17px; border-bottom:2px solid var(--ink); padding-top:12px; }}
.muted td, td.muted {{ color:var(--mute); }} .warn {{ color:var(--warn); }}
.summary {{ max-width:460px; }}
footer {{ color:var(--mute); font-size:12px; margin-top:32px; }}
@media print {{ body {{ padding:0; }} section {{ break-inside:avoid; }} }}
</style></head><body><main>
<h1>{e(head)}</h1>
<p class="sub">Owner statement · {pd.Period(st['month'], 'M').strftime('%B %Y')}{(' · managed by ' + e(manager)) if manager else ''}</p>
<div class="summary"><h2>Revenue</h2><table>{rev}<tr class="tot"><td>Revenue</td><td class='n'>{_rm(st['revenue'])}</td></tr>{carry_html}</table>
<h2>Expenses</h2><table>{exp}<tr class="tot"><td>Total expenses</td><td class='n'>{_rm(st['expenses'])}</td></tr>
<tr class="total"><td>Net payout</td><td class="n">{_rm(st['net_payout'])}</td></tr></table>{notes_html}</div>
<div class="wrap">{unit_html}</div>
<footer>Gross + cleaning is what Airbnb or Agoda paid after their fee. Commission is on the gross after cleaning. A stay is counted in the month the guest checks {'in' if st.get('basis', 'checkin') == 'checkin' else 'out'}. Guest details are not included.</footer>
</main></body></html>"""


def run(cfg_path: str, month: str):
    cfg_path = Path(cfg_path)
    root = cfg_path.parent.parent
    cfg = json.loads(cfg_path.read_text())
    d = {"commission": 0.20, "vacancy_wipe": 10, "vacancy_after_nights": 3, "month_basis": "checkin", **cfg.get("owner_defaults", {})}
    data = load(root)
    out = root / "private" / "statements" / month
    out.mkdir(parents=True, exist_ok=True)
    managed = [u for u in cfg["units"] if u.get("active") and u.get("ownership") == "managed"]
    summary = []
    opening = _read_csv(root / "data" / "raw" / "owner_balances.csv")   # month,statement,amount (deficit as negative)
    start = cfg.get("statements_start", month)

    def carry_into(sid, meta, units, m):
        if m <= start:
            if opening is None:
                return 0.0
            o = opening[(opening.statement == sid) & (opening.month == m)]
            return float(o.amount.sum()) if len(o) else 0.0
        prev = str(pd.Period(m, "M") - 1)
        p = build_statement(sid, meta, units, prev, data, d, carry_into(sid, meta, units, prev))
        return min(p["net_payout"], 0.0)

    for sid, meta in cfg.get("statements", {}).items():
        units = [u for u in managed if u.get("owner_terms", {}).get("statement") == sid]
        if not units:
            continue
        dd = {**d, **{k: v for k, v in meta.items() if k in ("commission", "vacancy_wipe", "vacancy_after_nights", "month_basis")}}
        st = build_statement(sid, meta, units, month, data, dd, carry_into(sid, meta, units, month))
        (out / f"{sid}.html").write_text(render(st, cfg.get("manager_name", "")))
        for u in st["units"]:
            summary.append({"statement": sid, "unit": u["short"], "revenue": u["payout"], "commission": u["commission"]})
        summary.append({"statement": sid, "unit": "NET PAYOUT", "revenue": st["revenue"], "commission": st["net_payout"]})
        print(f"{sid}: revenue {_rm(st['revenue'])}, carried {_rm(st['carried'])}, expenses {_rm(st['expenses'])}, net payout {_rm(st['net_payout'])}"
              + (f"  [{st['unpriced']} unpriced direct stay(s)]" if st["unpriced"] else ""))
    unassigned = [u["short"] for u in managed if u.get("owner_terms", {}).get("statement") not in cfg.get("statements", {})]
    if unassigned:
        print("No statement set for: " + ", ".join(unassigned))
    s = pd.DataFrame(summary)
    s.to_csv(out / "summary.csv", index=False)
    fees = s[s.unit != "NET PAYOUT"].commission.sum() if len(s) else 0
    print(f"Management fees {month}: RM{fees:,.2f}   statements in {out}")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
