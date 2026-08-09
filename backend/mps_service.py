"""Master Production Schedule / Production Plan - Tier 1 of the MRP II
cascade (Sales Plan -> **Production Plan [LOCKED]** -> MRP -> Purchasing).

For every finished good that either (a) has open-PO demand right now, or
(b) has any sales history at all in the trailing AMS window (see
demand_planning_service.py) - i.e. the full sellable-item universe, not
just whatever happens to have an order today - nets:

    Net FG Requirement = MAX(0, Gross Open-PO Demand + Safety Stock (AMS)
                               - On-Hand FG Inventory)

Each open-PO line's `target_ship_date` (when goods must leave the plant)
is backward-scheduled by that line's own `lead_day` (a per item+customer
field from the Open-PO feed - confirmed with the user: "we need to start
production around lead_day days prior to the target ship date") into a
`production_start_date` - THIS is the date used for time-phasing (earliest
production-start-date served first from on-hand, after reserving the
safety-stock buffer) and is what mrp_service.py's Tier 2 component
explosion further backs up from using each component's own procurement
lead time. A line with no lead_day on file gets production_start_date ==
target_ship_date (offset 0) rather than guessing a number.

Safety Stock Qty = that FG's AMS (a month of average sales = the 30-day
buffer the user asked for).

**Locking**: unlike every other "live" page in this app, a Production Plan
is deliberately NOT meant to be silently recalculated on every view - MRP
and Purchasing need a STABLE target to plan against, not a number that
shifts every time someone opens the page (the classic "MRP nervousness"
problem). build_production_plan() computes a fresh DRAFT; lock_production_
plan() persists an immutable, timestamped snapshot of that draft into
`production_plan_locks` - mrp_service.py's Tier 2 explosion reads from the
LATEST LOCK, never straight from a live recompute.

Only open-PO lines production has explicitly marked selected-for-production
(po_selection_service, shared with the existing MRP Plan tooling) count -
same convention as mrp_service.py."""
import logging
from datetime import datetime, timedelta, timezone

from demand_planning_service import get_demand_signal
from inventory_service import get_cached_inventory
from po_selection_service import get_selected_keys, selection_key

logger = logging.getLogger(__name__)

LOCKS_COLLECTION = "production_plan_locks"


def _offset_date(date_str: str, offset_days: float) -> str:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - timedelta(days=offset_days or 0)).isoformat()


def build_production_plan(open_po_client, oms_client, db, customer: str = None) -> dict:
    """Returns {generated_at, po_data_as_of, fgs: [{item_code, description,
    ams, safety_stock_qty, on_hand_qty, total_gross_qty, total_net_qty,
    demand_lines: [{internal_pono, customer_po, customer, target_ship_date,
    lead_day, production_start_date, qty_open, net_qty}]}]} - `fgs` sorted
    by total_net_qty descending (biggest shortages first). A finished good
    with sales history but NO open PO today still appears, with an empty
    demand_lines list, if its safety-stock alone creates a net requirement
    (on_hand < AMS)."""
    feed = open_po_client.get_open_po_demand(customer=customer)
    all_rows = feed.get("rows", [])
    selected_keys = get_selected_keys(db)
    rows = [r for r in all_rows if r.get("internal_pono") is not None and r.get("item_code")
            and selection_key(r["internal_pono"], r["item_code"]) in selected_keys]

    demand_signal = get_demand_signal(oms_client)
    on_hand_by_product = {it["product_id"]: it["total_qty"] for it in get_cached_inventory(db)["items"]}

    lines_by_item = {}
    for row in rows:
        item_code = row.get("item_code")
        qty_open = row.get("qty_open") or 0
        if qty_open <= 0:
            continue
        lines_by_item.setdefault(item_code, []).append({
            "internal_pono": row.get("internal_pono"),
            "customer_po": row.get("customer_po"),
            "customer": row.get("customer"),
            "target_ship_date": row.get("target_ship_date"),
            "lead_day": row.get("lead_day"),
            "production_start_date": _offset_date(row.get("target_ship_date"), row.get("lead_day")),
            "qty_open": qty_open,
            "description": row.get("description"),
        })

    # Full sellable-item universe - anything with an open PO right now, PLUS
    # anything with sales history at all (so a steady-selling item with no
    # current order still gets its safety stock accounted for).
    all_item_codes = set(lines_by_item.keys()) | set(demand_signal.keys())

    fgs = []
    for item_code in all_item_codes:
        lines = lines_by_item.get(item_code, [])
        signal = demand_signal.get(item_code) or {}
        ams = signal.get("ams") or 0.0
        safety_stock_qty = ams  # 1 month of AMS == the 30-day safety-stock buffer
        on_hand_qty = on_hand_by_product.get(item_code)
        on_hand_for_netting = on_hand_qty if on_hand_qty is not None else 0.0

        lines.sort(key=lambda l: (l["production_start_date"] or l["target_ship_date"] or "9999-12-31"))
        running_available = on_hand_for_netting - safety_stock_qty
        total_gross = 0.0
        total_net = 0.0
        for line in lines:
            gross = line["qty_open"]
            total_gross += gross
            if running_available >= gross:
                net = 0.0
                running_available -= gross
            elif running_available > 0:
                net = gross - running_available
                running_available = 0.0
            else:
                net = gross
            line["net_qty"] = round(net, 4)
            total_net += net

        # No open PO at all, but the safety stock itself isn't covered by
        # on-hand - still a real production requirement (steady replenishment).
        if not lines and running_available < 0:
            total_net = -running_available

        if total_gross <= 0 and total_net <= 0:
            continue  # nothing to report: no demand, no safety-stock shortfall

        description = lines[0]["description"] if lines else signal.get("description")
        fgs.append({
            "item_code": item_code,
            "description": description,
            "ams": round(ams, 4) if ams else 0.0,
            "safety_stock_qty": round(safety_stock_qty, 4) if safety_stock_qty else 0.0,
            "on_hand_qty": on_hand_qty,
            "total_gross_qty": round(total_gross, 4),
            "total_net_qty": round(total_net, 4),
            "demand_lines": lines,
        })

    fgs.sort(key=lambda f: -f["total_net_qty"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "po_data_as_of": feed.get("max_changed_at"),
        "total_open_po_lines": len(all_rows),
        "total_selected_po_lines": len(rows),
        "fgs": fgs,
    }


def lock_production_plan(db, plan: dict, locked_by: str = None) -> dict:
    """Persists an immutable snapshot of a just-computed draft plan (see
    build_production_plan) - this becomes the stable target every
    downstream MRP/Purchasing calculation reads from until the NEXT lock.
    Returns the stored document (including its new `_id`/locked_at)."""
    doc = {
        "locked_at": datetime.now(timezone.utc),
        "locked_by": locked_by,
        "generated_at": plan["generated_at"],
        "po_data_as_of": plan.get("po_data_as_of"),
        "total_open_po_lines": plan.get("total_open_po_lines"),
        "total_selected_po_lines": plan.get("total_selected_po_lines"),
        "fg_count": len(plan["fgs"]),
        "fgs": plan["fgs"],
    }
    result = db[LOCKS_COLLECTION].insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def get_latest_locked_plan(db) -> dict:
    """Most recently locked snapshot, or None if nothing has ever been
    locked yet."""
    return db[LOCKS_COLLECTION].find_one(sort=[("locked_at", -1)])


def list_locked_plans(db, limit: int = 50) -> list:
    """History of locks, metadata only (no full fgs payload) - most recent
    first, for a future "lock history" UI."""
    docs = db[LOCKS_COLLECTION].find(
        {}, {"fgs": 0}
    ).sort("locked_at", -1).limit(limit)
    return list(docs)
