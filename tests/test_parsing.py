"""Run with:  python -m pytest -q"""
import pandas as pd

from strdash.adapters.calendar_notes import classify, parse_amount, text_range


def test_classify():
    assert classify("Rentokil 13/10 @ 9am") == "maintenance"
    assert classify("Aircond service @1pm") == "maintenance"
    assert classify("Ana (Ben's Relative)") == "personal"
    assert classify("Guest A RM650") == "direct"
    assert classify("Direct guest 22-23/11 +60 10-000 0000") == "direct"
    assert classify("Danny") == "direct_unpriced"


def test_parse_amount():
    assert parse_amount("Guest RM650", 5) == 650
    assert parse_amount("19-23/7 RM200 x 4= RM800", 4) == 800
    assert parse_amount("Guest RM290 2 nights RM115 x 2 + RM60", 2) == 290
    assert parse_amount("Guest RM2,400 / month", 30) == 2400
    assert parse_amount("Guest no price", 2) is None


def test_text_range():
    ci, co = text_range("18-29/8 Guest RM1260", pd.Timestamp("2026-08-23"))
    assert (ci, co) == (pd.Timestamp("2026-08-18"), pd.Timestamp("2026-08-29"))
    ci, co = text_range("30-2/1 Guest", pd.Timestamp("2025-12-30"))
    assert (ci, co) == (pd.Timestamp("2025-12-30"), pd.Timestamp("2026-01-02"))
    assert text_range("Rentokil 21/9", pd.Timestamp("2026-09-20")) is None


def test_public_labels():
    from strdash.public import check, labels
    units = [
        {"id": "a", "short": "B 101", "area": "Condo B", "location": "Ampang", "bedrooms": 1},
        {"id": "b", "short": "B 202", "area": "Condo B", "location": "Ampang", "bedrooms": 1},
        {"id": "c", "short": "3BR House", "area": "Shah Alam", "location": "Shah Alam", "bedrooms": 3},
    ]
    assert labels(units, public=True) == {"a": "Ampang · 1BR (1)", "b": "Ampang · 1BR (2)", "c": "Shah Alam · 3BR"}
    assert labels(units, public=False)["a"] == "B 101"
    assert check('{"name":"Ampang · 1BR (1)"}', units) == []
    assert check('{"area":"Condo B"}', units) == ["Condo B"]


def test_trim_history():
    from strdash.public import trim_history
    d = {"meta": {"units": [{"id": "a", "live_from": "2019-01-01"}], "direct_from": "2023-01-01"},
         "stays": [["a", "2024-09", 1], ["a", "2024-10", 2]],
         "fees": [["agoda", "2024Q3", 1], ["agoda", "2024Q4", 2]],
         "gaps": [["a", "2024-09-30", 1], ["a", "2024-10-01", 2]]}
    out = trim_history(d, "2026-10-09", 24)
    assert out["stays"] == [["a", "2024-10", 2]]
    assert out["fees"] == [["agoda", "2024Q4", 2]]
    assert out["gaps"] == [["a", "2024-10-01", 2]]
    assert out["meta"]["units"][0]["live_from"] == "2024-10-01"





D = {"commission": 0.2, "vacancy_wipe": 10, "vacancy_after_nights": 3, "month_basis": "checkin"}


def test_owner_statement_arithmetic():
    """Made-up month: two units, a wipe, a carried deficit and one-off items."""
    import pandas as pd
    from strdash.statements import build_statement
    T = pd.Timestamp
    rows = [("x", 2, 4, 300.00), ("x", 10, 11, 160.00), ("y", 5, 8, 420.00)]
    b = pd.DataFrame([dict(booking_id=str(i), channel="agoda", unit_id=u, checkin=T(f"2026-05-{ci:02d}"),
                           checkout=T(f"2026-05-{co:02d}"), nights=co - ci, status="confirmed", net_revenue=p,
                           cleaning_fee=float("nan"), revenue_known=True) for i, (u, ci, co, p) in enumerate(rows)])
    stay = {("x", d) for d in (2, 3, 10)} | {("y", d) for d in (5, 6, 7)}
    night = pd.DataFrame([{"unit_id": u, "date": T(f"2026-05-{d:02d}"), "state": "stay" if (u, d) in stay else "open", "past": True}
                          for u in "xy" for d in range(1, 32)])
    exp = pd.DataFrame([{"month": "2026-05", "unit_id": "y", "category": "Maintenance", "item": "Tap", "amount": 30}])
    units = [{"id": "x", "short": "X", "owner_terms": {"cleaning_cost": 40, "monthly": {"Parking": 80}}},
             {"id": "y", "short": "Y", "owner_terms": {"cleaning_cost": 50, "vacancy_wipe": 0, "monthly": {"Netflix": 20}}}]
    s = build_statement("o", {"monthly": {"Ad": 100}}, units, "2026-05", (b, night, None, exp, None), D, carried=-50)
    u = {x["short"]: x for x in s["units"]}
    assert u["X"]["cleaning_fees"] == 40 + 50          # second stay follows 6 empty nights: wipe
    assert u["X"]["commission"] == round((460 - 90) * 0.2, 2) and u["Y"]["commission"] == round(370 * 0.2, 2)
    assert s["net_payout"] == round(880 - 50 - (100 + 74 + 74 + 90 + 50 + 80 + 20 + 30), 2)


def test_unpriced_direct_stay():
    import pandas as pd
    from strdash.statements import unit_month
    T = pd.Timestamp
    b = pd.DataFrame([dict(booking_id="1", channel="direct", unit_id="u", checkin=T("2026-09-10"), checkout=T("2026-09-12"),
                           nights=2, status="confirmed", net_revenue=float("nan"), cleaning_fee=float("nan"), revenue_known=False)])
    days = pd.date_range("2026-09-01", "2026-09-30")
    night = pd.DataFrame({"unit_id": "u", "date": days, "state": ["stay" if d.day in (10, 11) else "open" for d in days], "past": True})
    x = unit_month({"id": "u", "owner_terms": {"cleaning_cost": 40}}, "2026-09", b, night, None, None, D)
    assert x["unpriced"] == 1 and x["payout"] == 0 and x["cleaning_fees"] == 50 and x["wipes_n"] == 1
    dp = pd.DataFrame({"unit_id": ["u"], "checkin": [T("2026-09-10")], "amount": [300.0]})
    y = unit_month({"id": "u", "owner_terms": {"cleaning_cost": 40}}, "2026-09", b, night, None, dp, D)
    assert y["payout"] == 300 and y["commission"] == 50.0


def test_monthly_charge_from_month():
    from strdash.statements import _charge
    c = {"amount": 108.80, "from": "2026-09"}
    assert _charge(c, "2026-08") == (False, 0.0) and _charge(c, "2026-09") == (True, 108.80)
    assert _charge(None, "2026-09") == (True, None) and _charge(50, "2026-01") == (True, 50.0)
