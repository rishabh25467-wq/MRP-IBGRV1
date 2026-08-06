"""Purchasing Plan orchestration.

Pipeline: OMS monthly sales forecast -> resolve each OMS part number to a SAP
BOM (trying the part number directly first, since SAP recognizes it as a
valid Product/BOM ID for most parts; the OMS wm-part-map value is only a
secondary fallback - see build_purchasing_plan for details) -> explode the
resolved BOM (reusing the existing SOAP client) -> aggregate required
quantity at LEAF-level components only (sub-assemblies are skipped, only
their own leaf materials count) -> attach live SAP standard costs -> return
quantity + value broken down by month, for the next 2 months (relative to
today), plus a list of OMS parts that could not be resolved/exploded into a
SAP BOM (shown as a warning in the UI).
"""
import logging
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

    # 2. Explode each part's BOM using the OMS part number ITSELF first -
    # empirically, SAP recognizes it directly as a valid Product/BOM ID for
    # most parts. Only for parts where that fails do we try the wm-part-map's
    # mapped value as a second-wave fallback (avoids doubling SOAP calls for
    # the common case where the direct lookup already succeeds). All calls
    # share one sub-BOM cache since many top-level parts share the same
    # hardware/packaging sub-components - this avoids re-fetching the same
    # sub-BOM over and over across dozens of top-level parts. Explosions run
    # sequentially (not concurrently) so the shared cache is actually
    # populated before the next part needs it, and to avoid overloading the
    # SAP tenant with too many simultaneous connections (each explode_bom()
    # call already fans out internally across BOM levels).
    shared_bom_cache = {}

    def explode(candidate_id):
        try:
            return candidate_id, sap_soap_client.explode_bom(candidate_id, shared_cache=shared_bom_cache)
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
    }
