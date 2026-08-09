"""Demand planning - Average Monthly Sales (AMS), the "Sales Plan" layer at
the top of the MRP II cascade (Sales Plan -> Production Plan [locked] ->
MRP -> Purchasing - see PRD.md for the full design discussion).

AMS = a finished good's actual sales quantity, trailing N complete calendar
months (default 6), averaged per month. Two things read this number:
  (a) it IS that finished good's safety stock target (1 month of AMS is
      treated as the 30-day safety buffer the user asked for)
  (b) exploded through the BOM (see mrp_service.py), it becomes each
      bought-out component's dynamic Minimum Stock Level (MSL) - replacing
      a manually-typed static number with "what this component's real
      consumption rate actually is right now"

Sourced from OMS's per-customer monthly Sales view (oms_client.get_parts(
view=None) -> actual_qty) - the same feed that already powers the
Purchasing Plan page's "Sales Plan Lookup" popup, just aggregated across
the trailing window instead of a single month.

Extensibility note: get_demand_signal() is the ONE function every planning
layer (mps_service.py, mrp_service.py) should call for "how much of this FG
do we typically need per month" - it currently always returns the AMS
value, but is deliberately the single seam to swap in a real per-item
weekly forecast once OMS exposes one (per user's Session 17 note: "OMS is
going to expose weekly forecasts too... use that instead of AMS for only
those items that have a forecast flowing in" - not available yet, so
today every item falls back to AMS unconditionally)."""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

logger = logging.getLogger(__name__)

TRAILING_MONTHS_DEFAULT = 6


def _trailing_complete_months(n: int) -> list:
    """Returns the last `n` 'YYYY-MM' strings, ending with the last fully
    COMPLETE calendar month (never the current, still-in-progress one -
    that would understate the average since the month isn't over yet)."""
    today = date.today()
    year, month = today.year, today.month - 1
    if month == 0:
        month = 12
        year -= 1
    months = []
    for _ in range(n):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


def get_average_monthly_sales(oms_client, months: int = TRAILING_MONTHS_DEFAULT) -> dict:
    """Returns {part_no: {"ams": float, "description": str, "trailing_months":
    [...]}} - `ams` is total actual sales qty across the trailing `months`
    complete calendar months, divided by `months` (a part with zero rows in
    some of those months still divides by the full window - a genuinely
    slow-selling part should show a smaller AMS, not get inflated by only
    averaging over the months it happened to sell in)."""
    month_list = _trailing_complete_months(months)

    def fetch_customers(month):
        try:
            return month, oms_client.get_customers(month)
        except Exception as e:
            logger.warning(f"AMS: could not fetch OMS customers for {month}: {e}")
            return month, []

    with ThreadPoolExecutor(max_workers=6) as executor:
        customers_by_month = dict(executor.map(fetch_customers, month_list))

    fetch_jobs = [
        (month, c["customer_name"])
        for month, customers in customers_by_month.items()
        for c in customers if c.get("customer_name")
    ]

    def fetch_parts(job):
        month, customer_name = job
        try:
            return oms_client.get_parts(month, customer_name, view=None)
        except Exception as e:
            logger.warning(f"AMS: could not fetch OMS parts for {customer_name}/{month}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=8) as executor:
        all_parts_lists = list(executor.map(fetch_parts, fetch_jobs))

    totals = {}
    for parts in all_parts_lists:
        for part in parts:
            part_no = part.get("part_no")
            if not part_no:
                continue
            qty = part.get("actual_qty") or 0
            entry = totals.setdefault(part_no, {"total_qty": 0.0, "description": part.get("name")})
            entry["total_qty"] += qty
            if not entry["description"] and part.get("name"):
                entry["description"] = part.get("name")

    return {
        part_no: {
            "ams": round(data["total_qty"] / months, 4),
            "description": data["description"],
            "trailing_months": month_list,
        }
        for part_no, data in totals.items()
    }


def get_demand_signal(oms_client, months: int = TRAILING_MONTHS_DEFAULT) -> dict:
    """Single entrypoint for "how much of this FG do we typically need per
    month" - see module docstring. Today this always resolves to AMS; once
    a per-item weekly forecast feed exists, this is the one place to make
    it take priority over AMS for the items that have one."""
    return get_average_monthly_sales(oms_client, months=months)
