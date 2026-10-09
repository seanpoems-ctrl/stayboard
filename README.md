# Stayboard

A performance dashboard for short-term rental hosts. It combines Airbnb, Agoda,
Booking.com and direct bookings into one dataset per unit, and publishes a static,
phone-friendly dashboard that runs on GitHub Pages.

```
config/host.json        one file per host: units, platform IDs, data sources
data/raw/               platform exports (never committed)
data/clean/             bookings.csv, blocks.csv, adjustments.csv, nightly.csv.gz (never committed)
strdash/adapters/       one reader per data source
strdash/model.py        bookings + blocks -> one row per unit per night
strdash/metrics.py      monthly cubes, gaps, forward calendar
strdash/build.py        runs everything and writes private/data.json
strdash/public.py       makes the shareable copy in docs/ (location + room type labels)
strdash/index.html      the dashboard page (copied into private/ and docs/)
private/                your own dashboard with real unit names (never committed)
docs/                   public dashboard served by GitHub Pages
tests/                  parser tests
```

## Run it

```
pip install -r requirements.txt
python -m strdash.build config/host.json
python -m http.server -d private 8000   # your dashboard: http://localhost:8000
python -m strdash.public config/host.json   # refresh the public copy in docs/
```

## Data sources

| Source | Where it comes from | What it provides |
|---|---|---|
| `agoda_transactions` | YCS → Finance → Transactions (Excel per property, or the API pull) | Bookings, cancellations, booking value, payout, adjustments |
| `airbnb_reservations` | Airbnb Reservations export (all statuses) | Every booking incl. cancelled and upcoming, net earnings |
| `airbnb_transactions` | Airbnb Earnings → Transaction history CSV | Exact gross / service fee / cleaning split |
| `airbnb_calendar` | Airbnb host calendar blocks and notes | Direct stays, maintenance, personal-use and unexplained blocks |
| `bookingcom` | Booking.com reservations export, or a host-kept sheet | Bookings, commission, cleaning |

Every source is optional. A host with only Airbnb sets only `airbnb_reservations`.

## Onboarding a new host

1. Copy `config/example.json` to `config/<host>.json` (host configs are not committed). List each unit once with its
   platform IDs (`agoda_id`, `airbnb_id`), `active`, and `ownership` (owned/managed).
2. Put their exports in `data/raw/` and point `sources` at them.
3. Run the build and check the printed summary (bookings per source, priced direct
   stays, overlaps resolved).
4. For a new platform, write `strdash/adapters/<platform>.py` that returns the
   BOOKINGS columns in `strdash/schema.py`. Nothing downstream changes.

## How figures are derived

- **Agoda** pays `booking value × (1 − commission) + cleaning fee`. Commission and the
  per-stay cleaning fee are recovered per unit and half-year by a least-squares fit.
  Status: one row with payout > 0 = stayed; payout 0 = free cancellation; rows that
  net to 0 = cancelled after charge.
- **Airbnb** earnings are the host payout (net). The transaction export gives the
  exact fee split. Where it doesn't cover a booking, the fee rate is the one Airbnb
  charged on bookings made around the same date (rolling median). This follows fee
  model changes, e.g. split fee ~3.2% → host-only fee ~16.7% from Sep 2026.
- **Direct stays** come from calendar notes: a price, phone number or "additional
  night" marks a paying guest; a name alone is a direct stay with unknown revenue.
  Dates written in the note ("18-29/8") override the block's span. Note text is
  never written to the clean tables or the dashboard, because it holds guest names
  and phone numbers.
- **Nights open for sale** exclude maintenance, personal and unexplained blocks, so
  those nights never count as unsold or as gaps.
- **Gap nights**: unbooked runs of up to 14 nights with a stay on both sides.

## Publishing on GitHub Pages

Settings → Pages → Deploy from a branch → `main` / `/docs`.

`docs/` only ever holds the public build. Your own dashboard, with real unit names,
builds into `private/`, which is never committed.

To refresh the public demo after a build:

```
python -m strdash.public config/host.json
```

Units are labelled by `location` and bedrooms ("Shah Alam · 2BR (1)", "Ampang · 1BR (3)").
Every active unit needs a `location` (town, not building). The command stops without
writing anything if any unit name, building name, alias or listing ID would appear in
the public files. Setting `"public_mode": true` makes `strdash.build` write the
public version to `docs/` directly. Revenue figures stay real, but only the last 24 months are published (`public_months` in the config).

## Owner statements (managed units)

```
python -m strdash.build config/host.json            # refresh the clean tables first
python -m strdash.statements config/host.json 2026-09
```

One printable page per owner statement in `private/statements/2026-09/` (open in a
browser, print to PDF) and `summary.csv` with the management fee per unit. The layout
and arithmetic follow the host's own monthly revenue sheets (tested against Aug 2026
to the cent). Statements stay in `private/` and are never published.

- A stay counts in the month it checks in (`month_basis` in `owner_defaults`).
- Cleaning per stay = the unit's `cleaning_cost`, plus `vacancy_wipe` when the unit
  sat empty for `vacancy_after_nights` or more before the stay.
- Commission = 20% of (payout - cleaning), on the unit's monthly total.
- Monthly charges per statement (`statements.<id>.monthly`) and per unit
  (`owner_terms.monthly`); one-off items in `data/raw/owner_expenses.csv`
  (`month,unit_id,category,item,amount`).
- A negative net payout carries into the next month. The opening deficit for
  `statements_start` goes in `data/raw/owner_balances.csv` (`month,statement,amount`).
- Prices for direct stays whose note had none: `data/raw/direct_prices.csv`
  (`unit_id,checkin,amount`).
