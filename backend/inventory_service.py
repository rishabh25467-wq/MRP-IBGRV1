"""Orchestrates the Inventory page: live SAP on-hand stock (with location
breakdown) joined against SAP Standard Costs for valuation, and against
component_master for description/category when SAP's own description isn't
available or a friendlier one already exists locally."""
import logging

import requests
from sap_valuation_client import SAPValuationError

logger = logging.getLogger(__name__)


def build_inventory(db, sap_inventory_client, sap_valuation_client) -> list:
    """Returns [{product_id, description, category, total_qty, uom,
    unit_cost, currency, total_value, locations: [{site, logistics_area,
    stock_status, qty}]}] sorted by product_id. If the SAP Standard Costs
    endpoint is temporarily unreachable (it's known to be intermittently
    flaky, see materialvaluationdata connectivity notes elsewhere), this
    degrades gracefully to unit_cost/total_value=None rather than failing
    the whole page - quantities are still useful without a live cost."""
    detail_rows = sap_inventory_client.get_inventory_detail()

    by_product = {}
    for row in detail_rows:
        product_id = row["product_id"]
        entry = by_product.setdefault(product_id, {
            "product_id": product_id,
            "description": row.get("description"),
            "total_qty": 0.0,
            "uom": row.get("uom"),
            "locations": [],
        })
        entry["total_qty"] += row["qty"]
        entry["locations"].append({
            "site": row.get("site"),
            "logistics_area": row.get("logistics_area"),
            "stock_status": row.get("stock_status"),
            "qty": row["qty"],
        })

    component_docs = {
        doc["_id"]: doc
        for doc in db["component_master"].find(
            {"_id": {"$in": list(by_product.keys())}}, {"description": 1, "category": 1, "product_uuid": 1}
        )
    }
    for product_id, entry in by_product.items():
        comp = component_docs.get(product_id)
        if comp:
            entry["description"] = comp.get("description") or entry["description"]
            entry["category"] = comp.get("category")
        else:
            entry["category"] = None

    product_uuids = [
        component_docs[pid]["product_uuid"]
        for pid in by_product
        if component_docs.get(pid, {}).get("product_uuid")
    ]

    costs = {}
    if product_uuids:
        try:
            costs = sap_valuation_client.get_standard_costs(product_uuids)
        except (SAPValuationError, requests.exceptions.RequestException) as e:
            logger.warning(f"Standard Costs unavailable for Inventory valuation, showing quantities only: {e}")

    for product_id, entry in by_product.items():
        product_uuid = component_docs.get(product_id, {}).get("product_uuid")
        cost = costs.get(product_uuid.upper()) if product_uuid else None
        if cost:
            entry["unit_cost"] = cost["amount"]
            entry["currency"] = cost["currency"]
            entry["total_value"] = round(cost["amount"] * entry["total_qty"], 2)
        else:
            entry["unit_cost"] = None
            entry["currency"] = None
            entry["total_value"] = None
        entry["locations"].sort(key=lambda loc: -loc["qty"])

    return sorted(by_product.values(), key=lambda e: e["product_id"])
