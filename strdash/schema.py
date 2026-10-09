"""Common tables every adapter must produce.

Adding a new host or a new channel means writing one adapter that returns
these columns; everything downstream (nightly model, metrics, dashboard)
works off these tables only.

BOOKINGS - one row per reservation
    booking_id        str   unique within channel
    channel           str   'airbnb' | 'agoda' | 'direct' | <any other>
    unit_id           str   from config units[].id
    booked_date       date  when the guest booked (NaT if unknown, e.g. direct)
    checkin           date
    checkout          date  (exclusive: checkout - checkin = nights)
    nights            int
    status            str   'confirmed' | 'cancelled'
    gross_revenue     float what the guest paid for the stay excl. taxes, incl. cleaning
    cleaning_fee      float part of gross_revenue that is cleaning (NaN if unknown)
    platform_fee      float commission / service fee kept by the channel
    net_revenue       float what the host receives (gross - platform_fee); for a
                            cancelled booking, any cancellation payout retained
    revenue_estimated bool  True when gross/fee were inferred rather than reported
    revenue_known     bool  False when the source has no revenue (e.g. name-only note)

BLOCKS - one row per unit-night that is closed to sale but not a stay
    unit_id   str
    date      date
    category  str  'maintenance' | 'personal' | 'unknown'

ADJUSTMENTS - money movements not tied to a stay night (refunds, penalties)
    channel, unit_id, date, amount, ref
"""

BOOKING_COLS = [
    "booking_id", "channel", "unit_id", "booked_date", "checkin", "checkout", "nights",
    "status", "gross_revenue", "cleaning_fee", "platform_fee", "net_revenue",
    "revenue_estimated", "revenue_known",
]
BLOCK_COLS = ["unit_id", "date", "category"]
ADJ_COLS = ["channel", "unit_id", "date", "amount", "ref"]
