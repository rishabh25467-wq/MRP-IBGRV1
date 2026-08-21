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
import os

from datetime import datetime, timezone

COLLECTION = "store_requests"
COUNTER_COLLECTION = "store_request_counters"

# Flip RADISH_GOODS_MOVEMENT_DRY_RUN=false in .env only after a batch of
# real dry runs has been reviewed and confirmed correct (user's explicit
# Aug 2026 choice). Env-driven (not a code constant) so this can be
# flipped without a redeploy, per testing_agent iteration_98's review.
GOODS_MOVEMENT_DRY_RUN = os.environ.get("RADISH_GOODS_MOVEMENT_DRY_RUN", "true").lower() != "false"


def _trigger_goods_movement(radish_client, owner_party_id, product_id, source_warehouse, target_warehouse, quantity, uom, site_id) -> dict:
    try:
        result = radish_client.goods_movement(
            owner_party_id=owner_party_id, product_id=product_id,
            source_logistics_area_id=source_warehouse, target_logistics_area_id=target_warehouse,
            quantity=quantity, quantity_uom=uom, site_id=site_id, dry_run=GOODS_MOVEMENT_DRY_RUN,
        )
        return {**result, "attempted": True}
    except Exception as e:
        # Broad catch is deliberate (iteration_98 review) - a
        # requests.Timeout/ConnectionError against this external,
        # SAP-backed API is just as likely as a RadishQMSError, and the
        # docstring above promises the approval itself is NEVER blocked
        # by a failed/unreachable movement call.
        return {"attempted": True, "ok": False, "error": str(e)}

# Human-shareable, traceable-by-site request/issue ID (Aug 2026, per user
# request - a full uuid4() was unusable to read aloud/type over chat with
# the store team). Format "{site_id}-000123" - an atomic per-site counter
# (Mongo $inc, upsert) guarantees uniqueness without any collision-retry
# loop, and the site prefix alone tells the store team which site's queue
# a shared ID belongs to at a glance.
_SEQUENCE_WIDTH = 6


def _next_sequence(db, counter_key: str) -> int:
    doc = db[COUNTER_COLLECTION].find_one_and_update(
        {"_id": counter_key}, {"$inc": {"seq": 1}}, upsert=True, return_document=True,
    )
    return doc["seq"]


def _generate_id_for_site(db, site_id: str) -> str:
    seq = _next_sequence(db, site_id)
    return f"{site_id}-{seq:0{_SEQUENCE_WIDTH}d}"


def _generate_issue_id_for_site(db, site_id: str) -> str:
    """Separate from the Request ID (own counter, own key "{site_id}:issue"
    so its sequence never collides with/skips numbers in the request
    sequence) - one created every time stock is actually issued (i.e. once
    per submit_issue() call - a request can only be issued once, see the
    "no longer pending" guard below), format "{site_id}-I000123" (user's
    Aug 2026 choice - same shape as the Request ID, "I" marks it as the
    issue-side reference so the two are never confused when read aloud)."""
    seq = _next_sequence(db, f"{site_id}:issue")
    return f"{site_id}-I{seq:0{_SEQUENCE_WIDTH}d}"


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


def list_known_target_bins(db, site_id: str) -> list:
    """Every distinct Target Bin a store person has actually typed in for
    this site so far (Aug 2026) - powers the "Bin list per site" dropdown
    on /storeapproval. No fixed master list exists yet (user's explicit
    choice: "give a dropdown for now, fix later per site/user" - it may
    eventually need to be the SAME warehouse the request came from, still
    undecided), so this just grows organically: the first time a new bin
    is typed for a site it's a one-off free-text entry, and every request
    after that sees it as a dropdown option too."""
    return sorted(b for b in db[COLLECTION].distinct("target_logistics_area_id", {"site_id": site_id}) if b)


def submit_issue(db, request_id: str, issued: list, decision: str, store_actor: str, radish_client=None, target_logistics_area_id: str = None):
    """Store records actual issued quantity per component. If everything
    was issued in full, resolves immediately. If short, the store must pick
    `decision`: "proceed" (continue the order pipeline anyway) or
    "send_to_planner" (defer to the requester to decide).

    Each `issued` entry may also carry `warehouse` (the source Logistics
    Area the store person picked, from that component's `locations`) and
    `owner_party_id` (that location's SAP owner, e.g. "RI"/"RT" - auto-
    derived on the frontend from the picked location, not guessed). When
    both are present, both `radish_client` and `target_logistics_area_id`
    are given, and issued_qty > 0, this also fires a real Goods Movement
    call (Aug 2026, Radish QMS integration) so the physical stock move is
    recorded in SAP alongside this approval. Hardcoded to `dry_run=True`
    for now (GOODS_MOVEMENT_DRY_RUN below) - user's explicit choice until
    a few real dry runs are reviewed - so nothing physically moves in SAP
    yet, only the SOAP envelope preview is captured for review. A failed/
    skipped movement call NEVER blocks the approval itself - it's recorded
    on the component for visibility, not a hard gate. Also generates a
    separate `issue_id` (Aug 2026, user's explicit choice) - one per
    submit_issue() call, distinct from the Request ID so the two can't be
    confused when read aloud to the requester/planner."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        return None
    if doc["status"] != "pending":
        raise ValueError(f"This request is no longer pending (current status: {doc['status']})")

    issued_map = {i["product_id"]: i for i in issued}
    components = []
    shortfall_exists = False
    for c in doc["components"]:
        i = issued_map.get(c["product_id"], {})
        issued_qty = i.get("issued_qty")
        issued_qty = 0 if issued_qty is None else float(issued_qty)
        shortfall = max(0.0, round(c["required_qty"] - issued_qty, 4))
        if shortfall > 0:
            shortfall_exists = True
        movement = None
        if issued_qty > 0 and radish_client is not None and target_logistics_area_id and i.get("warehouse") and i.get("owner_party_id"):
            movement = _trigger_goods_movement(
                radish_client, i["owner_party_id"], c["product_id"], i["warehouse"], target_logistics_area_id,
                issued_qty, c.get("unit_of_measure") or "EA", doc["site_id"],
            )
        components.append({
            **c, "issued_qty": issued_qty, "shortfall": shortfall, "goods_movement": movement,
            # Persisted regardless of whether the movement actually fired
            # (testing_agent iteration_98) - the resolved view/journal
            # needs to show "issued from X" without parsing SOAP XML.
            "issued_from_warehouse": i.get("warehouse"),
            "issued_from_owner": i.get("owner_party_id"),
        })

    now = datetime.now(timezone.utc)
    update = {
        "components": components, "store_actor": store_actor, "store_decision": decision, "updated_at": now,
        "target_logistics_area_id": target_logistics_area_id,
        "issue_id": _generate_issue_id_for_site(db, doc["site_id"]),
    }
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
