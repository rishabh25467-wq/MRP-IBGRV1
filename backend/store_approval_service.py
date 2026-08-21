"""Requester -> Store Approval workflow (Aug 2026).

When the auto-release pre-flight stock check (production_confirmation_
service.check_component_availability) finds one or more BOM components
short at the target site, the create-and-release job no longer just fails
the whole run. Instead it still creates the SAP Production Proposal right
away (per user's explicit choice - "create proposals in SAP immediately"),
then pauses and opens a `store_requests` doc so a warehouse/store user can
record what was ACTUALLY issued via the unauthenticated `/storeapproval`
screen before the automated Proposal -> Order pipeline (server.py's
`_continue_order_creation`) is allowed to resume.

Lifecycle: pending -> resolved (store had enough, or store chose to
proceed with a partial issue) OR pending -> partial_pending_planner (store
issued less than required and asked the requester/planner to decide) ->
resolved (planner approved) or cancelled (planner rejected).

No SAP "delete proposal" step exists for `cancelled` - confirmed via
research that SAP ByDesign's ManageProductionProposalIn only exposes
CreateBundle, no Cancel/Delete operation. A cancelled request simply never
triggers the Release action, so the Proposal sits un-converted in SAP -
the exact same harmless/documented outcome as any other Proposal a
planner chooses not to act on."""
from datetime import datetime, timezone

COLLECTION = "store_requests"
COUNTER_COLLECTION = "store_request_counters"

# Human-shareable, traceable-by-site request/issue ID (Aug 2026, per user
# request - a full uuid4() was unusable to read aloud/type over chat with
# the store team). Format "{site_id}-000123" - an atomic per-site counter
# (Mongo $inc, upsert) guarantees uniqueness without any collision-retry
# loop, and the site prefix alone tells the store team which site's queue
# a shared ID belongs to at a glance.
_SEQUENCE_WIDTH = 6


def _next_sequence(db, site_id: str) -> int:
    doc = db[COUNTER_COLLECTION].find_one_and_update(
        {"_id": site_id}, {"$inc": {"seq": 1}}, upsert=True, return_document=True,
    )
    return doc["seq"]


def _generate_id_for_site(db, site_id: str) -> str:
    seq = _next_sequence(db, site_id)
    return f"{site_id}-{seq:0{_SEQUENCE_WIDTH}d}"


def ensure_indexes(db) -> None:
    db[COLLECTION].create_index("job_id")
    db[COLLECTION].create_index("status")


def create_request(db, job_id: str, payload_dict: dict, proposal_id: str, short_components: list, actor: str) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": _generate_id_for_site(db, payload_dict["site_id"]),
        "job_id": job_id,
        "production_proposal_id": proposal_id,
        "material_id": payload_dict["material_id"],
        "site_id": payload_dict["site_id"],
        "quantity": payload_dict["quantity"],
        "unit_code": payload_dict["unit_code"],
        "requester": actor,
        "components": [
            {
                "product_id": c["product_id"],
                "description": c.get("description"),
                "unit_of_measure": c.get("unit_of_measure"),
                "required_qty": c["required_qty"],
                "available_qty": c.get("available_qty"),
                "locations": c.get("locations") or [],
                "issued_qty": None,
                "shortfall": None,
            }
            for c in short_components
        ],
        "status": "pending",
        "store_actor": None,
        "store_decision": None,
        "planner_actor": None,
        "planner_decision": None,
        "resolution": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }
    db[COLLECTION].insert_one(doc)
    return doc


def list_requests(db) -> list:
    """Public queue for the unauthenticated /storeapproval screen - every
    request still needing action from either the store or the planner."""
    return list(db[COLLECTION].find({"status": {"$in": ["pending", "partial_pending_planner"]}}).sort("created_at", -1))


def list_all_requests(db) -> list:
    """Full journal/history for /storeapproval - every request regardless
    of status, most recent first."""
    return list(db[COLLECTION].find({}).sort("created_at", -1))


def get_request(db, request_id: str):
    return db[COLLECTION].find_one({"_id": request_id})


def get_request_by_job(db, job_id: str):
    return db[COLLECTION].find_one({"job_id": job_id}, sort=[("created_at", -1)])


def submit_issue(db, request_id: str, issued: list, decision: str, store_actor: str):
    """Store records actual issued quantity per component. If everything
    was issued in full, resolves immediately. If short, the store must pick
    `decision`: "proceed" (continue the order pipeline anyway) or
    "send_to_planner" (defer to the requester to decide)."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        return None
    if doc["status"] != "pending":
        raise ValueError(f"This request is no longer pending (current status: {doc['status']})")

    issued_map = {i["product_id"]: i["issued_qty"] for i in issued}
    components = []
    shortfall_exists = False
    for c in doc["components"]:
        issued_qty = issued_map.get(c["product_id"])
        issued_qty = 0 if issued_qty is None else float(issued_qty)
        shortfall = max(0.0, round(c["required_qty"] - issued_qty, 4))
        if shortfall > 0:
            shortfall_exists = True
        components.append({**c, "issued_qty": issued_qty, "shortfall": shortfall})

    now = datetime.now(timezone.utc)
    update = {"components": components, "store_actor": store_actor, "store_decision": decision, "updated_at": now}
    if not shortfall_exists:
        update.update({"status": "resolved", "resolution": "full_issue", "resolved_at": now})
    elif decision == "proceed":
        update.update({"status": "resolved", "resolution": "store_proceeded_partial", "resolved_at": now})
    elif decision == "send_to_planner":
        update.update({"status": "partial_pending_planner"})
    else:
        raise ValueError("Some components are short - choose 'proceed' or 'send_to_planner'")

    db[COLLECTION].update_one({"_id": request_id}, {"$set": update})
    return db[COLLECTION].find_one({"_id": request_id})


def planner_decision(db, request_id: str, decision: str, planner_actor: str):
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        return None
    if doc["status"] != "partial_pending_planner":
        raise ValueError(f"This request is not awaiting a planner decision (current status: {doc['status']})")

    now = datetime.now(timezone.utc)
    new_status = "resolved" if decision == "approve" else "cancelled"
    update = {"status": new_status, "planner_actor": planner_actor, "planner_decision": decision, "updated_at": now}
    if new_status == "resolved":
        update.update({"resolution": "planner_approved_partial", "resolved_at": now})
    db[COLLECTION].update_one({"_id": request_id}, {"$set": update})
    return db[COLLECTION].find_one({"_id": request_id})
