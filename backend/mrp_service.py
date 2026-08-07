"""Material Requirements Planning (MRP) against the OMS Open-PO Demand feed.

Pipeline (see MRP_OPEN_PO_DEMAND_API.md for the source feed's exact shape):
  1. Pull open PO lines (item_code, qty_open, target_ship_date) from the
     external Open-PO Demand feed (open_po_client.py), then keep ONLY the
     lines production has explicitly marked as selected-for-production
     (po_selection_service - checked off on the Open PO Demand or MRP Plan
     tab) - purchasing only ever sees demand production has actually
     signed off on, not the entire raw open-PO backlog.
  2. Resolve each line's item_code to a SAP BOM through the same persistent,
     Mongo-backed cache every other feature uses (bom_cache_service) - so a
     BOM-alternate choice made on the Production Plan page's own Alternates
     tab (production_plan_service.py) applies here automatically.
  3. Explode to LEAF-level components only (sub-assemblies skipped - same
     convention as purchasing_plan.py's _collect_leaves).
  4. For each leaf demand line: Order-By Date = the PO's target_ship_date
     minus that LEAF component's own Procurement Lead Time (Admin >
     Component Master's lead_time_days - the same field the SAP MSL
     Write-Back feature already maintains). A leaf with no lead_time_days
     set gets order_by_date = target_ship_date and is flagged
     lead_time_missing=True rather than guessing a number.
  5. Net each leaf's aggregate demand against current on-hand inventory
     (read from the existing inventory_cache - no fresh SAP call needed)
     plus its Minimum Stock Level, consuming the EARLIEST-need (by
     target_ship_date) demand lines first (time-phased allocation) - so a
     shortage surfaces against the specific PO(s) actually causing it.
  6. The frontend buckets the resulting demand lines by flat total / month /
     week - all from this same per-line response, no extra server calls.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import bom_cache_service
from bom_cache_service import BomFetchError
from inventory_service import get_cached_inventory
from purchasing_plan import get_part_overrides
from po_selection_service import get_selected_keys, selection_key

logger = logging.getLogger(__name__)


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


def get_component_msl(db, product_ids) -> dict:
    docs = db["component_master"].find({"_id": {"$in": list(product_ids)}, "msl": {"$ne": None}}, {"msl": 1})
    return {d["_id"]: d["msl"] for d in docs}


def build_mrp_plan(open_po_client, sap_soap_client, db, customer: str = None) -> dict:
    """Returns {generated_at, po_data_as_of, total_po_lines,
    total_open_po_lines, unresolved_items: [{item_code, confidence,
    reason}], components: [{product_id, description, unit_of_measure,
    lead_time_days, msl, on_hand_qty, total_gross_qty, total_net_qty,
    demand_lines: [{internal_pono, customer_po, customer, item_code,
    target_ship_date, order_by_date, lead_time_missing, gross_qty,
    net_qty}]}]}, components sorted by total_net_qty descending (biggest
    shortages first). Only PO lines production has explicitly marked
    selected-for-production (po_selection_service) are counted -
    total_open_po_lines is the full feed's count for context/comparison."""
    feed = open_po_client.get_open_po_demand(customer=customer)
    all_rows = feed.get("rows", [])
    selected_keys = get_selected_keys(db)
    rows = [r for r in all_rows if r.get("internal_pono") is not None and r.get("item_code")
            and selection_key(r["internal_pono"], r["item_code"]) in selected_keys]

    item_codes = sorted({r["item_code"] for r in rows if r.get("item_code")})
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

    # Aggregate demand lines per leaf component.
    demand_by_component = defaultdict(list)
    for row in rows:
        item_code = row.get("item_code")
        leaves = leaves_by_item.get(item_code)
        if not leaves:
            continue
        qty_open = row.get("qty_open") or 0
        target_ship_date = row.get("target_ship_date")
        for leaf in leaves.values():
            gross_qty = round(leaf["per_unit_qty"] * qty_open, 4)
            if gross_qty <= 0:
                continue
            demand_by_component[leaf["product_id"]].append({
                "internal_pono": row.get("internal_pono"),
                "customer_po": row.get("customer_po"),
                "customer": row.get("customer"),
                "item_code": item_code,
                "target_ship_date": target_ship_date,
                "gross_qty": gross_qty,
                "description": leaf.get("description"),
                "unit_of_measure": leaf.get("unit_of_measure"),
            })

    lead_times = get_component_lead_times(db, demand_by_component.keys())
    msl_by_product = get_component_msl(db, demand_by_component.keys())
    on_hand_by_product = {it["product_id"]: it["total_qty"] for it in get_cached_inventory(db)["items"]}

    components = []
    for product_id, lines in demand_by_component.items():
        lead_time_days = lead_times.get(product_id)
        for line in lines:
            line["lead_time_missing"] = lead_time_days is None
            line["order_by_date"] = _offset_date(line["target_ship_date"], lead_time_days or 0)

        # Time-phased netting: earliest need (target_ship_date) is served
        # first from current on-hand + MSL buffer - a shortage surfaces
        # against the specific PO(s) actually causing it, not just a single
        # undifferentiated total.
        lines.sort(key=lambda l: (l["target_ship_date"] or "9999-12-31"))
        msl = msl_by_product.get(product_id) or 0.0
        on_hand = on_hand_by_product.get(product_id) or 0.0
        running_available = on_hand - msl
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

        first_line = lines[0]
        components.append({
            "product_id": product_id,
            "description": first_line.get("description"),
            "unit_of_measure": first_line.get("unit_of_measure"),
            "lead_time_days": lead_time_days,
            "msl": msl if msl else None,
            "on_hand_qty": on_hand_by_product.get(product_id),
            "total_gross_qty": round(total_gross, 4),
            "total_net_qty": round(total_net, 4),
            "demand_lines": lines,
        })

    components.sort(key=lambda c: -c["total_net_qty"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "po_data_as_of": feed.get("max_changed_at"),
        "total_po_lines": len(rows),
        "total_open_po_lines": len(all_rows),
        "unresolved_items": unresolved_items,
        "components": components,
    }
