"""Public (shareable) version of the dashboard.

A public build keeps every figure but names units only by location and room
type, e.g. "Shah Alam · 1BR" or "Ampang · 1BR (2)". Unit numbers, building
names and listing IDs never appear.

Turn an existing private build into a public copy (no raw exports needed):

    python -m strdash.public config/host.json            # private/ -> docs/
    python -m strdash.public config/host.json private docs
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path


def location_of(u: dict) -> str:
    """Town-level location. Falls back to 'area' only if no location is set,
    so a config that forgets 'location' fails the check in scrub() instead of
    leaking a building name."""
    return u.get("location") or u.get("area") or ""


def labels(units: list[dict], public: bool) -> dict[str, str]:
    """Display name per unit id. Private: the host's short name.
    Public: 'Location · nBR', numbered only when two units would share a label."""
    if not public:
        return {u["id"]: u["short"] for u in units}

    def base(u):
        br = u.get("bedrooms")
        typ = f"{br}BR" if br else "Unit"
        loc = location_of(u)
        return f"{loc} · {typ}" if loc else typ

    counts = Counter(base(u) for u in units)
    seen: Counter = Counter()
    out = {}
    for u in units:
        b = base(u)
        seen[b] += 1
        out[u["id"]] = f"{b} ({seen[b]})" if counts[b] > 1 else b
    return out


def unit_meta(u: dict, name: str, public: bool) -> dict:
    return {"id": u["id"], "name": name,
            "area": location_of(u) if public else u.get("area", ""),
            "bedrooms": u.get("bedrooms"), "ownership": u.get("ownership", "")}


def private_terms(units: list[dict]) -> set[str]:
    """Strings that must not appear in a public build: unit names, short names,
    building/area names that differ from the town, aliases and listing IDs."""
    terms = set()
    for u in units:
        for k in ("name", "short", "agoda_id", "airbnb_id"):
            if u.get(k):
                terms.add(str(u[k]))
        terms.update(u.get("aliases", []))
        if u.get("area") and u.get("area") != u.get("location"):
            terms.add(u["area"])
    return {t for t in terms if t}


def check(text: str, units: list[dict]) -> list[str]:
    return sorted(t for t in private_terms(units) if t in text)


_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$|^\d{4}Q[1-4]$")


def trim_history(data: dict, as_of: str, months: int) -> dict:
    """Keep only the last `months` full months before as_of (plus the forward
    calendar). Each table row is filtered on its first date-like field."""
    y, m = int(as_of[:4]), int(as_of[5:7])
    k = y * 12 + (m - 1) - months
    cut = f"{k // 12:04d}-{k % 12 + 1:02d}"
    cut_q = f"{k // 12:04d}Q{(k % 12) // 3 + 1}"

    def keep(row):
        for v in row:
            if isinstance(v, str) and _DATE.match(v):
                return v >= (cut_q if "Q" in v else cut)
        return True

    for key, rows in data.items():
        if key != "meta" and isinstance(rows, list):
            data[key] = [r for r in rows if keep(r)]
    for u in data["meta"]["units"]:
        if u.get("live_from") and u["live_from"] < cut:
            u["live_from"] = cut + "-01"
    if data["meta"].get("direct_from") and data["meta"]["direct_from"] < cut:
        data["meta"]["direct_from"] = cut + "-01"
    return data


def make_public(cfg_path: str, src_dir: str = "private", out_dir: str = "docs") -> Path:
    cfg_path = Path(cfg_path)
    root = cfg_path.parent.parent
    cfg = json.loads(cfg_path.read_text())
    units = cfg["units"]
    active = {u["id"]: u for u in units if u.get("active")}
    missing = [u["short"] for u in active.values() if not u.get("location")]
    if missing:
        raise SystemExit(f"Set 'location' for: {', '.join(missing)}")

    src, out = root / src_dir, root / out_dir
    data = json.loads((src / "data.json").read_text())
    names = labels(list(active.values()), public=True)
    data["meta"]["host"] = cfg.get("public_host_name", "Demo Host")
    data["meta"]["units"] = [{**unit_meta(active[m["id"]], names[m["id"]], True), "live_from": m.get("live_from")}
                             for m in data["meta"]["units"]]

    data = trim_history(data, data["meta"]["as_of"], cfg.get("public_months", 24))
    text = json.dumps(data, separators=(",", ":"))
    html = (root / "strdash" / "index.html").read_text()
    leaks = check(text, units) + check(html, units)
    if leaks:
        raise SystemExit(f"Public build stopped, private names found: {', '.join(leaks)}")

    out.mkdir(exist_ok=True)
    (out / "data.json").write_text(text)
    shutil.copyfile(root / "strdash" / "index.html", out / "index.html")
    print("\n".join(f"{m['id']}: {m['name']}" for m in data["meta"]["units"]))
    print(f"public build written to {out} (no private names found)")
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    make_public(a[0] if a else "config/host.json", *(a[1:3]))
