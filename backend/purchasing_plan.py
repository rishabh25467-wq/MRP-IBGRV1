"""Purchasing Plan orchestration.

Pipeline: OMS monthly sales forecast (for a single target month, either
user-picked or defaulted to next month) -> resolve each OMS part number to a
SAP BOM (trying, in order: a manually-saved part_id_overrides correction if
one exists, then the part number directly since SAP recognizes it as a valid
Product/BOM ID for most parts, then the OMS wm-part-map value as a last
resort - see build_purchasing_plan/_resolve_boms for details) -> explode the
resolved BOM via the persistent, Mongo-backed BOM cache (see
bom_cache_service.py - avoids re-exploding live from SAP on every single
regeneration; a background job keeps the cache fresh) -> aggregate required
quantity at LEAF-level components only (sub-assemblies are skipped, only
their own leaf materials count) -> attach live SAP standard costs -> net
against live SAP on-hand inventory (Net Purchase Qty = max(0, Gross Required
Qty - On-Hand Qty)) -> return gross + on-hand + net quantity/value for that
month, plus a list of OMS parts that could not be resolved/exploded into a
SAP BOM (shown as a warning in the UI, each tagged with a `confidence` of
"fetch_error" - SAP was unreachable, likely transient, retryable via
retry_missing_boms() - or "not_found" - SAP cleanly confirmed no BOM exists).
"""
import logging
import re
from datetime import date, datetime, timezone

import bom_cache_service
from bom_cache_service import BomFetchError

logger = logging.getLogger(__name__)


def _default_month() -> str:
    """Next calendar month relative to today, as 'YYYY-MM'."""
    today = date.today()
    year, month = today.year, today.month + 1
    if month > 12:
        month = 1
        year += 1
    return f"{year:04d}-{month:02d}"


def _validate_month(month_str: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month_str or ""):
        raise ValueError(f"Invalid month '{month_str}' - expected format 'YYYY-MM'")
    return month_str


def _collect_leaves(nodes: list, leaves: dict):
    """Walk a BOM tree and aggregate LEAF-level (no children) components'
    per-unit-of-root quantity into `leaves`, keyed by product_id. Nodes with
    children are sub-assemblies and are skipped - only their own descendants
    (down to the leaves) are counted."""
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
            "product_uuid": node.get("product_uuid"),
            "per_unit_qty": 0.0,
        })
        entry["per_unit_qty"] += qty
        if not entry.get("product_uuid") and node.get("product_uuid"):
            entry["product_uuid"] = node["product_uuid"]


def _explode(candidate_id, sap_soap_client, db):
    """Returns (bom_or_none, fetch_failed_bool). fetch_failed=True means SAP
    couldn't be reached even after bom_cache_service's internal retries -
    distinct from SAP cleanly confirming there's no BOM (see BomFetchError)."""
    try:
        return bom_cache_service.build_tree_from_cache(candidate_id, sap_soap_client, db), False
    except BomFetchError as e:
        logger.warning(f"BOM explosion failed for SAP id {candidate_id}: {e}")
        return None, True


def get_part_overrides(db) -> dict:
    """Manual OMS-part-number -> SAP-Material-ID corrections a user has
    saved via the Missing BOMs table's "Fix Mapping" box (e.g. OMS reports
    'E410' but the real SAP Material ID is 'E410_IN') - persisted in Mongo
    so they apply automatically to every future plan regeneration, not just
    a one-off retry check."""
    return {doc["_id"]: doc["sap_id"] for doc in db["part_id_overrides"].find({})}


def save_part_override(db, part_no: str, sap_id: str) -> None:
    db["part_id_overrides"].update_one(
        {"_id": part_no},
        {"$set": {"sap_id": sap_id, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def _resolve_boms(part_nos, part_map, sap_soap_client, db, overrides=None):
    """For each OMS part number, resolves to a SAP BOM. Tries, in priority
    order: (1) a manually-saved override for this exact part number, if any
    (see get_part_overrides - rare, so always attempted), (2) the part
    number itself directly (empirically, SAP recognizes it directly as a
    valid Product/BOM ID for most parts), (3) the OMS wm-part-map's mapped
    value, only as a last-resort fallback (avoids doubling SOAP calls for
    the common case where an earlier candidate already succeeds).
    Resolution goes through the persistent Mongo-backed cache
    (bom_cache_service) so repeat regenerations for the same/overlapping
    parts are near-instant instead of re-exploding live from SAP every time.
    Returns (bom_by_id, resolved_id_by_part_no, missing_boms) - each
    missing_boms entry carries a `confidence` of "fetch_error" (SAP was
    unreachable even after retries - transient, safe to retry) or
    "not_found" (SAP cleanly confirmed no BOM exists for any candidate tried)."""
    overrides = overrides or {}
    bom_by_id = {}
    fetch_failed_by_id = {}

    override_ids = sorted({overrides[p].strip() for p in part_nos if (overrides.get(p) or "").strip()})
    for cid in override_ids:
        bom_by_id[cid], fetch_failed_by_id[cid] = _explode(cid, sap_soap_client, db)

    def already_resolved(part_no):
        override = (overrides.get(part_no) or "").strip()
        return bool(override and bom_by_id.get(override))

    for part_no in sorted(part_nos):
        if not already_resolved(part_no) and part_no not in bom_by_id:
            bom_by_id[part_no], fetch_failed_by_id[part_no] = _explode(part_no, sap_soap_client, db)

    unresolved_part_nos = [p for p in part_nos if not already_resolved(p) and not bom_by_id.get(p)]
    fallback_ids = sorted({
        (part_map.get(p) or "").strip()
        for p in unresolved_part_nos
        if (part_map.get(p) or "").strip() and (part_map.get(p) or "").strip() != p
    })
    for cid in fallback_ids:
        bom_by_id[cid], fetch_failed_by_id[cid] = _explode(cid, sap_soap_client, db)

    resolved_id_by_part_no = {}
    missing_boms = []
    for part_no in part_nos:
        override = (overrides.get(part_no) or "").strip()
        mapped = (part_map.get(part_no) or "").strip()
        candidates = ([override] if override else []) + [part_no] + (
            [mapped] if mapped and mapped != part_no and mapped != override else []
        )
        resolved = next((cid for cid in candidates if bom_by_id.get(cid)), None)
        if resolved:
            resolved_id_by_part_no[part_no] = resolved
        else:
            confidence = "fetch_error" if any(fetch_failed_by_id.get(cid) for cid in candidates) else "not_found"
            reason = (
                f"SAP lookup failed (connection issue) for: {', '.join(candidates)} - likely transient, safe to retry"
                if confidence == "fetch_error"
                else f"No SAP BOM found (tried: {', '.join(candidates)})"
            )
            missing_boms.append({
                "part_no": part_no,
                "sap_id": override or (mapped if mapped != part_no else None),
                "confidence": confidence,
                "reason": reason,
            })

    return bom_by_id, resolved_id_by_part_no, missing_boms


def retry_missing_boms(part_nos: list, part_map: dict, sap_soap_client, db, overrides: dict = None) -> list:
    """Re-attempts BOM resolution for a specific subset of previously-missing
    OMS part numbers (e.g. after a transient SAP timeout, or after the user
    saved a corrected SAP ID via the Missing BOMs table), without re-running
    the full Purchasing Plan pipeline - lets a user instantly re-check just
    the failed lookups instead of waiting several minutes to regenerate the
    whole plan. Any part that now resolves gets cached by
    bom_cache_service.build_tree_from_cache the same as a normal run, so a
    subsequent full "Regenerate" also picks it up near-instantly. Returns
    [{part_no, sap_id, resolved, reason}, ...]."""
    _, resolved_id_by_part_no, missing_boms = _resolve_boms(part_nos, part_map, sap_soap_client, db, overrides=overrides)
    missing_by_part_no = {m["part_no"]: m for m in missing_boms}

    results = []
    for part_no in part_nos:
        resolved_id = resolved_id_by_part_no.get(part_no)
        if resolved_id:
            results.append({
                "part_no": part_no,
                "sap_id": resolved_id if resolved_id != part_no else None,
                "resolved": True,
                "confidence": None,
                "reason": None,
            })
        else:
            missing = missing_by_part_no.get(part_no)
            results.append({
                "part_no": part_no,
                "sap_id": missing.get("sap_id") if missing else None,
                "resolved": False,
                "confidence": missing.get("confidence") if missing else "not_found",
                "reason": missing["reason"] if missing else "No SAP BOM found",
            })
    return results


def build_purchasing_plan(
    oms_client, sap_soap_client, sap_valuation_client, sap_inventory_client, db, target_month: str = None
) -> dict:
    months = [_validate_month(target_month) if target_month else _default_month()]

    # 1. Pull OMS sales forecast (aggregated per part number) for each month.
    part_map = oms_client.get_part_map()
    overrides = get_part_overrides(db)
    demand_by_month = {month: oms_client.get_monthly_demand(month) for month in months}

    all_part_nos = set()
    for demand in demand_by_month.values():
        all_part_nos.update(demand.keys())

    # 2. Resolve + explode each part's BOM (see _resolve_boms).
    bom_by_id, resolved_id_by_part_no, missing_boms = _resolve_boms(all_part_nos, part_map, sap_soap_client, db, overrides=overrides)

    # 3. Precompute per-unit leaf requirements for each successfully exploded BOM.
    leaves_by_id = {}
    for candidate_id, bom in bom_by_id.items():
        if not bom:
            continue
        leaves = {}
        _collect_leaves(bom["tree"], leaves)
        leaves_by_id[candidate_id] = leaves

    # Overall freshness of the BOM data actually used in this plan - the
    # oldest last_checked_at among every resolved BOM (see
    # bom_cache_service.build_tree_from_cache), so the UI can show
    # "BOM data as of ..." rather than silently trusting a possibly-stale cache.
    used_bom_ids = {rid for rid in resolved_id_by_part_no.values()}
    checked_timestamps = [
        bom_by_id[rid]["min_checked_at"] for rid in used_bom_ids if bom_by_id.get(rid) and bom_by_id[rid].get("min_checked_at")
    ]
    bom_data_as_of = min(checked_timestamps) if checked_timestamps else None

    # 4. Aggregate required leaf-component quantity, per month.
    components = {}
    for month, demand in demand_by_month.items():
        for part_no, qty in demand.items():
            resolved_id = resolved_id_by_part_no.get(part_no)
            leaves = leaves_by_id.get(resolved_id) if resolved_id else None
            if not leaves:
                continue
            for leaf_product_id, leaf in leaves.items():
                comp = components.setdefault(leaf_product_id, {
                    "product_id": leaf["product_id"],
                    "description": leaf["description"],
                    "unit_of_measure": leaf["unit_of_measure"],
                    "product_uuid": leaf.get("product_uuid"),
                    "qty_by_month": {m: 0.0 for m in months},
                })
                comp["qty_by_month"][month] += leaf["per_unit_qty"] * qty

    # 5. Attach live SAP standard costs (best-effort - a valuation-service
    # hiccup shouldn't take down the whole plan, just leave costs/values blank).
    uuids = [c["product_uuid"] for c in components.values() if c.get("product_uuid")]
    try:
        costs = sap_valuation_client.get_standard_costs(uuids) if uuids else {}
    except Exception as e:
        logger.warning(f"Standard cost lookup failed for purchasing plan: {e}")
        costs = {}

    # 6. Net against live SAP on-hand inventory (best-effort - same principle
    # as standard costs: a hiccup on the inventory report shouldn't take down
    # the whole plan, it just leaves on-hand/net figures blank for that run).
    # On-hand stock is a point-in-time snapshot (not per-month), so
    # Net Purchase Qty = max(0, Gross Required Qty - On-Hand Qty) is applied
    # independently to each requested month.
    try:
        on_hand_by_product = sap_inventory_client.get_on_hand_stock()
        inventory_as_of = datetime.now(timezone.utc)
    except Exception as e:
        logger.warning(f"On-hand inventory lookup failed for purchasing plan: {e}")
        on_hand_by_product = {}
        inventory_as_of = None

    result_components = []
    for comp in components.values():
        cost = costs.get((comp.get("product_uuid") or "").upper())
        amount = cost["amount"] if cost else None
        currency = cost.get("currency") if cost else None
        qty_by_month = {m: round(q, 4) for m, q in comp["qty_by_month"].items()}
        on_hand_qty = on_hand_by_product.get(comp["product_id"])
        net_qty_by_month = (
            {m: round(max(0.0, q - on_hand_qty), 4) for m, q in qty_by_month.items()}
            if on_hand_qty is not None
            else dict(qty_by_month)
        )
        result_components.append({
            "product_id": comp["product_id"],
            "description": comp["description"],
            "unit_of_measure": comp["unit_of_measure"],
            "qty_by_month": qty_by_month,
            "on_hand_qty": on_hand_qty,
            "net_qty_by_month": net_qty_by_month,
            "unit_cost": amount,
            "currency": currency,
            "value_by_month": {
                m: (round(q * amount, 2) if amount is not None else None)
                for m, q in qty_by_month.items()
            },
            "net_value_by_month": {
                m: (round(q * amount, 2) if amount is not None else None)
                for m, q in net_qty_by_month.items()
            },
        })
    result_components.sort(key=lambda c: c["product_id"])

    missing_boms.sort(key=lambda m: m["part_no"])

    return {
        "months": months,
        "components": result_components,
        "missing_boms": missing_boms,
        "bom_data_as_of": bom_data_as_of.isoformat() if bom_data_as_of else None,
        "inventory_as_of": inventory_as_of.isoformat() if inventory_as_of else None,
    }
