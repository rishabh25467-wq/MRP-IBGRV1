"""Tracks which open-PO lines production has actually committed to
building - a persisted selection flag per PO line (keyed by
`internal_pono::item_code`), with a full audit history of every
select/deselect action (who, when). The MRP Plan computation
(mrp_service.build_mrp_plan) only nets demand for SELECTED PO lines, so
purchasing only ever sees procurement suggestions production has actually
signed off on - not the entire raw open-PO backlog.

No login system exists in this app, so "who" is a free-text name the user
types once in the browser (persisted client-side) and sends with every
toggle - good enough for an internal audit trail, not a security boundary.
"""
from datetime import datetime, timezone


def selection_key(internal_pono, item_code) -> str:
    """`internal_pono` arrives as a float from the OMS feed (e.g. 9003458.0)
    - always normalize to int first so the key is stable regardless of
    whether the caller's JSON serializer keeps or drops the trailing
    `.0` (the frontend builds this exact same key client-side to merge
    selection state onto feed rows without a round-trip)."""
    return f"{int(internal_pono)}::{item_code}"


def get_selected_keys(db) -> set:
    """Set of selection keys currently marked selected=True - used by
    mrp_service to filter the live Open-PO feed before computing MRP."""
    return {doc["_id"] for doc in db["po_selections"].find({"selected": True}, {"_id": 1})}


def list_selections(db) -> list:
    """Every PO line ever touched (selected or since-deselected) - merged
    onto the Open PO Demand tab's rows so checkboxes and the 'who/when'
    caption render correctly on page load."""
    return list(db["po_selections"].find({}))


def toggle_selection(db, internal_pono, item_code, customer_po, customer, selected: bool, actor: str) -> dict:
    key = selection_key(internal_pono, item_code)
    now = datetime.now(timezone.utc)
    db["po_selections"].update_one(
        {"_id": key},
        {"$set": {
            "internal_pono": internal_pono, "item_code": item_code, "customer_po": customer_po,
            "customer": customer, "selected": selected, "selected_by": actor, "selected_at": now,
        }},
        upsert=True,
    )
    db["po_selection_history"].insert_one({
        "key": key, "internal_pono": internal_pono, "item_code": item_code, "customer_po": customer_po,
        "customer": customer, "action": "selected" if selected else "deselected", "by": actor, "at": now,
    })
    return {"key": key, "selected": selected, "selected_by": actor, "selected_at": now}


def get_history(db, internal_pono=None, item_code=None) -> list:
    """Full select/deselect audit trail, optionally scoped to one PO line
    (for the per-row History dialog) - most recent first."""
    query = {}
    if internal_pono is not None and item_code is not None:
        query["key"] = selection_key(internal_pono, item_code)
    return list(db["po_selection_history"].find(query).sort("at", -1).limit(200))
