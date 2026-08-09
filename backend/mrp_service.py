"""Material Requirements Planning (MRP) - Tier 2 of the MRP II cascade
(Sales Plan -> Production Plan [LOCKED] -> **MRP** -> Purchasing).

Pipeline:
  1. Reads the LATEST LOCKED Production Plan (mps_service.get_latest_
     locked_plan) - never a live Open-PO recompute directly (see
     mps_service.py's module docstring for why locking matters). Raises
     NoLockedPlanError if nothing has ever been locked yet.
  2. Resolves each locked FG's item_code to a SAP BOM through the same
     persistent, Mongo-backed cache every other feature uses
     (bom_cache_service) - so a BOM-alternate choice made on the Production
     Plan page's own Alternates tab (production_plan_service.py) applies
     here automatically.
  3. Explodes to LEAF-level components only (sub-assemblies skipped - same
     convention as purchasing_plan.py's _collect_leaves).
  4. Gross component demand = each locked FG demand line's already-NET
     quantity (Tier 1 already netted the FG against its own on-hand +
     safety stock - exploding the NET, not the gross, is what makes this a
     proper MRP II cascade instead of double-counting the FG-level buffer)
     x that leaf's per-unit BOM quantity. Each exploded line inherits its
     FG line's `production_start_date` (already backward-scheduled from
     target_ship_date by that PO's own lead_day - see mps_service.py) as
     the date the COMPONENT must be on hand by, before being backed up
     again in step 6.
  5. Dynamic MSL: every locked FG's AMS (regardless of whether it has a
     current shortfall) is exploded through its BOM too, so a component's
     safety stock = the sum of (each FG's AMS x that FG's BOM qty-per-unit)
     across every FG that uses it - "total sales over the last six months,
     divided by six, becomes the safety stock level for bought-out parts"
     per the user's original ask, replacing the old static, manually-typed
     MSL number entirely.
  6. Order-By Date = the component's need-by date (from step 4) minus that
     LEAF component's own Procurement Lead Time (Admin > Component
     Master's lead_time_days). Per the user's explicit instruction: default
     to 30 days when nothing is on file, but NEVER override a real value
     greater than 0 that's already set.
  7. Nets each leaf's aggregate demand against current on-hand inventory
     (read from the existing inventory_cache - no fresh SAP call needed)
     plus its dynamic MSL, consuming the EARLIEST-need (by order_by_date)
     demand lines first (time-phased allocation) - so a shortage surfaces
     against the specific PO(s)/FG(s) actually causing it.
  8. The frontend buckets the resulting demand lines by flat total / month /
     week - all from this same per-line response, no extra server calls.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import bom_cache_service
import mps_service
from bom_cache_service import BomFetchError
from inventory_service import get_cached_inventory
from purchasing_plan import get_part_overrides

logger = logging.getLogger(__name__)

DEFAULT_COMPONENT_LEAD_TIME_DAYS = 30


class NoLockedPlanError(Exception):
    pass


def _collect_leaves(nodes: list, leaves: dict):
    """Same convention as purchasing_plan._collect_leaves - walk a BOM tree,
    aggregating LEAF-level (no children) per-root-unit quantity. Nodes with
    children are sub-assemblies and are skipped - only their descendants
    down to the leaves are counted."""
    for node in nodes:
        children = node.get("children") or []
        if children:
            _collect_leaves(children, leaves)
            continue
        qty = node.get("quantity")
        if qty is None or not node.get("product_id"):
            continue
        entry = leaves.setdefault(node["product_id"], {
            "product_id": node["product_id"],
            "description": node.get("description"),
            "unit_of_measure": node.get("unit_of_measure"),
            "per_unit_qty": 0.0,
        })
        entry["per_unit_qty"] += qty


def _explode(item_code, sap_soap_client, db):
    """Returns (bom_or_none, fetch_failed_bool) - same contract as
    purchasing_plan._explode, via the shared BOM cache (which already
    applies any resolved BOM-alternate override for this product)."""
    try:
        return bom_cache_service.build_tree_from_cache(item_code, sap_soap_client, db), False
    except BomFetchError as e:
        logger.warning(f"MRP: BOM explosion failed for open-PO item '{item_code}': {e}")
        return None, True


def _resolve_bom_for_item(item_code, sap_soap_client, db, overrides):
    """Resolves one open-PO item_code to a SAP BOM, trying (1) a manually
    saved override for this exact item_code, if any - shared with the
    Purchasing Plan's "Fix Mapping" feature (part_id_overrides collection),
    (2) the item_code itself directly. Returns (bom_or_none,
    unresolved_entry_or_none) where unresolved_entry is {item_code, sap_id,
    confidence, reason}."""
    override = (overrides.get(item_code) or "").strip() if overrides else ""
    candidates = ([override] if override else []) + [item_code]

    bom_by_candidate = {}
    fetch_failed = {}
    for cid in candidates:
        if cid not in bom_by_candidate:
            bom_by_candidate[cid], fetch_failed[cid] = _explode(cid, sap_soap_client, db)

    resolved_id = next((cid for cid in candidates if bom_by_candidate.get(cid)), None)
    if resolved_id:
        return bom_by_candidate[resolved_id], None

    confidence = "fetch_error" if any(fetch_failed.get(cid) for cid in candidates) else "not_found"
    reason = (
        f"SAP lookup failed (connection issue) for: {', '.join(candidates)} - likely transient, safe to retry"
        if confidence == "fetch_error"
        else f"No SAP BOM found (tried: {', '.join(candidates)})"
    )
    return None, {"item_code": item_code, "sap_id": override or None, "confidence": confidence, "reason": reason}


def retry_unresolved_items(item_codes: list, sap_soap_client, db, overrides: dict = None) -> list:
    """Re-attempts BOM resolution for a subset of previously-unresolved
    open-PO item codes (e.g. after a transient SAP timeout, or after the
    user saved a corrected SAP ID via the MRP Plan's unresolved-items
    table) without re-running the whole multi-minute MRP pipeline - same
    pattern as purchasing_plan.retry_missing_boms. Returns [{item_code,
    sap_id, resolved, confidence, reason}, ...]."""
    overrides = overrides if overrides is not None else {}
    results = []
    for item_code in item_codes:
        bom, missing = _resolve_bom_for_item(item_code, sap_soap_client, db, overrides)
        if bom:
            override = (overrides.get(item_code) or "").strip()
            results.append({"item_code": item_code, "sap_id": override or None, "resolved": True, "confidence": None, "reason": None})
        else:
            results.append({
                "item_code": item_code, "sap_id": missing.get("sap_id"), "resolved": False,
                "confidence": missing["confidence"], "reason": missing["reason"],
            })
    return results


def _offset_date(date_str: str, offset_days: float) -> str:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - timedelta(days=offset_days)).isoformat()


def get_component_lead_times(db, product_ids) -> dict:
    """{product_id: lead_time_days} from component_master - the same field
    maintained by the Admin > Component Master page / SAP MSL Write-Back."""
    docs = db["component_master"].find(
        {"_id": {"$in": list(product_ids)}, "lead_time_days": {"$ne": None}}, {"lead_time_days": 1}
    )
    return {d["_id"]: d["lead_time_days"] for d in docs}


def build_mrp_plan(sap_soap_client, db) -> dict:
    """Returns {generated_at, locked_plan_id, locked_at, unresolved_items:
    [{item_code, confidence, reason}], components: [{product_id,
    description, unit_of_measure, lead_time_days, lead_time_is_default,
    dynamic_msl, on_hand_qty, total_gross_qty, total_net_qty, demand_lines:
    [{item_code, internal_pono, customer_po, customer, target_ship_date,
    order_by_date, gross_qty, net_qty}]}]}, components sorted by
    total_net_qty descending (biggest shortages first). Raises
    NoLockedPlanError if the Production Plan has never been locked yet -
    MRP always explodes a LOCKED snapshot, never a live recompute (see
    mps_service.py)."""
    locked = mps_service.get_latest_locked_plan(db)
    if not locked:
        raise NoLockedPlanError("No Production Plan has been locked yet - lock one on the Production Plan page first.")

    fgs = locked.get("fgs", [])
    item_codes = sorted({fg["item_code"] for fg in fgs})
    overrides = get_part_overrides(db)

    leaves_by_item = {}
    unresolved_items = []
    for item_code in item_codes:
        bom, unresolved = _resolve_bom_for_item(item_code, sap_soap_client, db, overrides)
        if bom:
            leaves = {}
            _collect_leaves(bom["tree"], leaves)
            leaves_by_item[item_code] = leaves
        else:
            unresolved_items.append(unresolved)
    unresolved_items.sort(key=lambda u: u["item_code"])

    # Explode each locked FG's NET requirement (Tier 1 already netted the FG
    # against its own on-hand + safety stock - exploding the net, not the
    # gross, avoids double-counting the FG-level buffer at the component
    # level too) through BOM into per-leaf gross component demand. Every
    # FG's AMS is ALSO exploded here (regardless of whether it currently has
    # a shortfall) to build each component's dynamic MSL (step 5 below).
    demand_by_component = defaultdict(list)
    ams_consumption_by_component = defaultdict(float)
    today_iso = date.today().isoformat()

    for fg in fgs:
        item_code = fg["item_code"]
        leaves = leaves_by_item.get(item_code)
        if not leaves:
            continue

        ams = fg.get("ams") or 0.0
        for leaf in leaves.values():
            ams_consumption_by_component[leaf["product_id"]] += ams * leaf["per_unit_qty"]

        demand_lines = fg.get("demand_lines") or []
        if demand_lines:
            for line in demand_lines:
                net_qty = line.get("net_qty") or 0
                if net_qty <= 0:
                    continue
                need_by_date = line.get("production_start_date") or line.get("target_ship_date")
                for leaf in leaves.values():
                    gross_qty = round(leaf["per_unit_qty"] * net_qty, 4)
                    if gross_qty <= 0:
                        continue
                    demand_by_component[leaf["product_id"]].append({
                        "item_code": item_code,
                        "internal_pono": line.get("internal_pono"),
                        "customer_po": line.get("customer_po"),
                        "customer": line.get("customer"),
                        "target_ship_date": line.get("target_ship_date"),
                        "need_by_date": need_by_date,
                        "gross_qty": gross_qty,
                        "description": leaf.get("description"),
                        "unit_of_measure": leaf.get("unit_of_measure"),
                    })
        else:
            # Pure safety-stock-driven FG requirement (no open PO at all) -
            # a single synthetic demand line dated today, so a component
            # shortage caused purely by FG safety stock still surfaces.
            net_qty = fg.get("total_net_qty") or 0
            if net_qty > 0:
                for leaf in leaves.values():
                    gross_qty = round(leaf["per_unit_qty"] * net_qty, 4)
                    if gross_qty <= 0:
                        continue
                    demand_by_component[leaf["product_id"]].append({
                        "item_code": item_code, "internal_pono": None, "customer_po": None, "customer": None,
                        "target_ship_date": today_iso, "need_by_date": today_iso, "gross_qty": gross_qty,
                        "description": leaf.get("description"), "unit_of_measure": leaf.get("unit_of_measure"),
                    })

    all_component_ids = set(demand_by_component.keys()) | set(ams_consumption_by_component.keys())
    lead_times = get_component_lead_times(db, all_component_ids)
    on_hand_by_product = {it["product_id"]: it["total_qty"] for it in get_cached_inventory(db)["items"]}

    components = []
    for product_id in all_component_ids:
        lines = demand_by_component.get(product_id, [])
        raw_lead_time = lead_times.get(product_id)
        # Per the user's explicit instruction: default to 30 days ONLY when
        # nothing is on file - never override a real value greater than 0.
        lead_time_is_default = not raw_lead_time
        lead_time_days = raw_lead_time if raw_lead_time else DEFAULT_COMPONENT_LEAD_TIME_DAYS

        for line in lines:
            line["order_by_date"] = _offset_date(line["need_by_date"], lead_time_days)

        # Time-phased netting: earliest need (order_by_date) is served first
        # from current on-hand + dynamic MSL buffer - a shortage surfaces
        # against the specific PO(s)/FG(s) actually causing it.
        lines.sort(key=lambda l: (l["order_by_date"] or l["need_by_date"] or "9999-12-31"))
        dynamic_msl = round(ams_consumption_by_component.get(product_id, 0.0), 4)
        on_hand = on_hand_by_product.get(product_id) or 0.0
        running_available = on_hand - dynamic_msl
        total_gross = 0.0
        total_net = 0.0
        for line in lines:
            gross = line["gross_qty"]
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

        # No demand lines at all, but the dynamic MSL itself isn't covered
        # by on-hand - still a real purchase requirement.
        if not lines and running_available < 0:
            total_net = -running_available

        if total_gross <= 0 and total_net <= 0:
            continue

        first_line = lines[0] if lines else None
        components.append({
            "product_id": product_id,
            "description": first_line.get("description") if first_line else None,
            "unit_of_measure": first_line.get("unit_of_measure") if first_line else None,
            "lead_time_days": lead_time_days,
            "lead_time_is_default": lead_time_is_default,
            "dynamic_msl": dynamic_msl if dynamic_msl else None,
            "on_hand_qty": on_hand_by_product.get(product_id),
            "total_gross_qty": round(total_gross, 4),
            "total_net_qty": round(total_net, 4),
            "demand_lines": lines,
        })

    components.sort(key=lambda c: -c["total_net_qty"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "locked_plan_id": str(locked["_id"]),
        "locked_at": locked["locked_at"].isoformat(),
        "unresolved_items": unresolved_items,
        "components": components,
    }
