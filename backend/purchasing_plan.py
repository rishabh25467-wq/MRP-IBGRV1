"""Purchasing Plan orchestration.

Pipeline: OMS monthly sales forecast -> map OMS part numbers to SAP parts via
the OMS part-map -> explode each mapped part's SAP BOM (reusing the existing
SOAP client) -> aggregate required quantity at LEAF-level components only
(sub-assemblies are skipped, only their own leaf materials count) -> attach
live SAP standard costs -> return quantity + value broken down by month, for
the next 2 months (relative to today), plus a list of OMS parts that could
not be mapped/exploded into a SAP BOM (shown as a warning in the UI).
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

logger = logging.getLogger(__name__)

MONTHS_AHEAD = 2


def _next_n_months(n: int) -> list:
    today = date.today()
    months = []
    year, month = today.year, today.month
    for _ in range(n):
        month += 1
        if month > 12:
            month = 1
            year += 1
        months.append(f"{year:04d}-{month:02d}")
    return months


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


def build_purchasing_plan(oms_client, sap_soap_client, sap_valuation_client) -> dict:
    months = _next_n_months(MONTHS_AHEAD)

    # 1. Pull OMS sales forecast (aggregated per part number) for each month.
    part_map = oms_client.get_part_map()
    demand_by_month = {month: oms_client.get_monthly_demand(month) for month in months}

    all_part_nos = set()
    for demand in demand_by_month.values():
        all_part_nos.update(demand.keys())

    # 2. Resolve every forecasted OMS part number to its SAP part ID.
    missing_boms = []
    sap_id_by_part_no = {}
    for part_no in all_part_nos:
        sap_id = (part_map.get(part_no) or "").strip()
        if not sap_id:
            missing_boms.append({"part_no": part_no, "sap_id": None, "reason": "No SAP part mapping found in OMS"})
        else:
            sap_id_by_part_no[part_no] = sap_id

    # 3. Explode each distinct mapped SAP part's BOM exactly once (cached), in parallel.
    unique_sap_ids = list(set(sap_id_by_part_no.values()))

    def explode(sap_id):
        try:
            return sap_id, sap_soap_client.explode_bom(sap_id)
        except Exception as e:
            logger.warning(f"BOM explosion failed for SAP id {sap_id}: {e}")
            return sap_id, None

    # Each explode_bom() call already fans out internally (its own thread pool
    # per BOM level) - running many of these top-level explosions concurrently
    # multiplies into far too many simultaneous connections to the SAP tenant
    # and causes connection timeouts. Keep this outer level small.
    with ThreadPoolExecutor(max_workers=2) as executor:
        bom_by_sap_id = dict(executor.map(explode, unique_sap_ids)) if unique_sap_ids else {}

    for part_no, sap_id in sap_id_by_part_no.items():
        if not bom_by_sap_id.get(sap_id):
            missing_boms.append({"part_no": part_no, "sap_id": sap_id, "reason": "No SAP BOM found for this mapped part"})

    # 4. Precompute per-unit leaf requirements for each successfully exploded BOM.
    leaves_by_sap_id = {}
    for sap_id, bom in bom_by_sap_id.items():
        if not bom:
            continue
        leaves = {}
        _collect_leaves(bom["tree"], leaves)
        leaves_by_sap_id[sap_id] = leaves

    # 5. Aggregate required leaf-component quantity, per month.
    components = {}
    for month, demand in demand_by_month.items():
        for part_no, qty in demand.items():
            sap_id = sap_id_by_part_no.get(part_no)
            leaves = leaves_by_sap_id.get(sap_id) if sap_id else None
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

    # De-duplicate missing_boms (same part_no should only appear once).
    seen = set()
    deduped_missing = []
    for item in missing_boms:
        if item["part_no"] in seen:
            continue
        seen.add(item["part_no"])
        deduped_missing.append(item)
    deduped_missing.sort(key=lambda m: m["part_no"])

    return {
        "months": months,
        "components": result_components,
        "missing_boms": deduped_missing,
    }
