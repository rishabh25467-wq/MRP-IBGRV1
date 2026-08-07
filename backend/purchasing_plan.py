"""Purchasing Plan orchestration.

Pipeline: OMS monthly sales forecast (for a single target month, either
user-picked or defaulted to next month) -> resolve each OMS part number to a
SAP BOM (trying the part number directly first, since SAP recognizes it as a
valid Product/BOM ID for most parts; the OMS wm-part-map value is only a
secondary fallback - see build_purchasing_plan for details) -> explode the
resolved BOM via the persistent, Mongo-backed BOM cache (see
bom_cache_service.py - avoids re-exploding live from SAP on every single
regeneration; a background job keeps the cache fresh) -> aggregate required
quantity at LEAF-level components only (sub-assemblies are skipped, only
their own leaf materials count) -> attach live SAP standard costs -> return
quantity + value for that month, plus a list of OMS parts that could not be
resolved/exploded into a SAP BOM (shown as a warning in the UI).
"""
import logging
import re
from datetime import date

import bom_cache_service

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


def build_purchasing_plan(oms_client, sap_soap_client, sap_valuation_client, db, target_month: str = None) -> dict:
    months = [_validate_month(target_month) if target_month else _default_month()]

    # 1. Pull OMS sales forecast (aggregated per part number) for each month.
    part_map = oms_client.get_part_map()
    demand_by_month = {month: oms_client.get_monthly_demand(month) for month in months}

    all_part_nos = set()
    for demand in demand_by_month.values():
        all_part_nos.update(demand.keys())

    # 2. Explode each part's BOM using the OMS part number ITSELF first -
    # empirically, SAP recognizes it directly as a valid Product/BOM ID for
    # most parts. Only for parts where that fails do we try the wm-part-map's
    # mapped value as a second-wave fallback (avoids doubling SOAP calls for
    # the common case where the direct lookup already succeeds). Resolution
    # goes through the persistent Mongo-backed cache (bom_cache_service) so
    # repeat regenerations for the same/overlapping parts are near-instant
    # instead of re-exploding live from SAP every time; explosions run
    # sequentially to avoid overloading the SAP tenant with too many
    # simultaneous connections whenever a genuinely new/uncached part shows up.
    def explode(candidate_id):
        try:
            return candidate_id, bom_cache_service.build_tree_from_cache(candidate_id, sap_soap_client, db)
        except Exception as e:
            logger.warning(f"BOM explosion failed for SAP id {candidate_id}: {e}")
            return candidate_id, None

    bom_by_id = dict(explode(part_no) for part_no in sorted(all_part_nos))

    unresolved_part_nos = [p for p in all_part_nos if not bom_by_id.get(p)]
    fallback_ids = sorted({
        (part_map.get(p) or "").strip()
        for p in unresolved_part_nos
        if (part_map.get(p) or "").strip() and (part_map.get(p) or "").strip() != p
    })
    if fallback_ids:
        bom_by_id.update(explode(cid) for cid in fallback_ids)

    # Pick, for each part_no, whichever candidate actually resolved to a BOM.
    resolved_id_by_part_no = {}
    missing_boms = []
    for part_no in all_part_nos:
        mapped = (part_map.get(part_no) or "").strip()
        candidates = [part_no] + ([mapped] if mapped and mapped != part_no else [])
        resolved = next((cid for cid in candidates if bom_by_id.get(cid)), None)
        if resolved:
            resolved_id_by_part_no[part_no] = resolved
        else:
            missing_boms.append({
                "part_no": part_no,
                "sap_id": mapped if mapped and mapped != part_no else None,
                "reason": f"No SAP BOM found (tried: {', '.join(candidates)})",
            })

    # 4. Precompute per-unit leaf requirements for each successfully exploded BOM.
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

    # 5. Aggregate required leaf-component quantity, per month.
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

    # 6. Attach live SAP standard costs (best-effort - a valuation-service
    # hiccup shouldn't take down the whole plan, just leave costs/values blank).
    uuids = [c["product_uuid"] for c in components.values() if c.get("product_uuid")]
    try:
        costs = sap_valuation_client.get_standard_costs(uuids) if uuids else {}
    except Exception as e:
        logger.warning(f"Standard cost lookup failed for purchasing plan: {e}")
        costs = {}

    result_components = []
    for comp in components.values():
        cost = costs.get((comp.get("product_uuid") or "").upper())
        amount = cost["amount"] if cost else None
        currency = cost.get("currency") if cost else None
        result_components.append({
            "product_id": comp["product_id"],
            "description": comp["description"],
            "unit_of_measure": comp["unit_of_measure"],
            "qty_by_month": {m: round(q, 4) for m, q in comp["qty_by_month"].items()},
            "unit_cost": amount,
            "currency": currency,
            "value_by_month": {
                m: (round(q * amount, 2) if amount is not None else None)
                for m, q in comp["qty_by_month"].items()
            },
        })
    result_components.sort(key=lambda c: c["product_id"])

    missing_boms.sort(key=lambda m: m["part_no"])

    return {
        "months": months,
        "components": result_components,
        "missing_boms": missing_boms,
        "bom_data_as_of": bom_data_as_of.isoformat() if bom_data_as_of else None,
    }
