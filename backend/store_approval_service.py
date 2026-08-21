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
import logging
import os
import re
import time

from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COLLECTION = "store_requests"
COUNTER_COLLECTION = "store_request_counters"

# Flip SAP_GOODS_MOVEMENT_DRY_RUN=false in .env only after a batch of
# real dry runs has been reviewed and confirmed correct (user's explicit
# Aug 2026 choice). Renamed from RADISH_GOODS_MOVEMENT_DRY_RUN once this
# path moved off Radish onto a direct SAP client - old key kept as a
# fallback so an already-deployed .env doesn't silently revert to dry-run.
# Read LAZILY (a function, not a module-level constant) - testing_agent
# iteration_100 caught that server.py imports this module BEFORE calling
# load_dotenv(), so a module-level `os.environ.get(...)` at import time
# always saw the flag missing and silently stayed dry-run even after the
# .env value was flipped to "false" and the backend restarted.
def is_dry_run() -> bool:
    value = os.environ.get("SAP_GOODS_MOVEMENT_DRY_RUN", os.environ.get("RADISH_GOODS_MOVEMENT_DRY_RUN", "true"))
    return value.lower() != "false"


def _has_sap_log_error(result: dict) -> str | None:
    """Real Aug 2026 bug: SAP can embed an actual error inside a normal-
    looking `GoodsAndActivityConfirmationGoodsMovementResponse` <Log> block
    (SeverityCode 3 = error) rather than raising a SOAP fault - Radish's
    own `ok`/`faults` parsing didn't catch this shape and reported
    `ok: true` for a movement SAP actually rejected ("Source logistics
    area is invalid..."). Belt-and-suspenders check on our side: scan any
    raw XML the response carries for a Log Item with SeverityCode 3+ and
    treat that as a failure regardless of what `ok` says."""
    xml = (result or {}).get("raw_xml") or (result or {}).get("envelope") or (result or {}).get("raw") or ""
    if not xml:
        return None
    if re.search(r"<SeverityCode>\s*[3-9]\s*</SeverityCode>", xml):
        note = re.search(r"<Note>(.*?)</Note>", xml)
        return note.group(1) if note else "SAP logged an error-severity item for this movement"
    return None


# Aug 2026, direct-to-SAP migration: this was pointed at Radish QMS's REST
# wrapper originally - traced 3 consecutive live-call failures ("Radish
# QMS/SAP unavailable") to Radish's OWN Emergent-hosted app gateway timing
# out on its outbound call to SAP (the raw body was literally Emergent's
# "502: Bad gateway" HTML page, not a real SAP/Radish error) - confirmed
# dry_run=true never hits this since it skips the outbound SAP call
# entirely. Fixed by calling SAP directly (sap_goods_movement_client.py),
# removing that extra hop/gateway altogether. Retry-with-backoff kept for
# genuine SAP-side transient network errors (this tenant has documented
# intermittent connect timeouts under load elsewhere in this app) -
# SAPGoodsMovementError has no HTTP-status attribute like the old
# RadishQMSError did, so retry on any exception except an auth failure
# (401 - "authentication failed" in the message), which will never
# self-resolve on retry.
_GOODS_MOVEMENT_MAX_ATTEMPTS = 3
_GOODS_MOVEMENT_RETRY_DELAY_SECONDS = 5


def _trigger_goods_movement(sap_client, owner_party_id, product_id, source_warehouse, target_warehouse, quantity, uom, site_id) -> dict:
    last_error = None
    for attempt in range(_GOODS_MOVEMENT_MAX_ATTEMPTS):
        try:
            result = sap_client.goods_movement(
                owner_party_id=owner_party_id, product_id=product_id,
                source_logistics_area_id=source_warehouse, target_logistics_area_id=target_warehouse,
                quantity=quantity, quantity_uom=uom, site_id=site_id, dry_run=is_dry_run(),
            )
            sap_error = _has_sap_log_error(result)
            if sap_error and result.get("ok"):
                result = {**result, "ok": False, "error": f"SAP rejected the movement: {sap_error}"}
            return {**result, "attempted": True}
        except Exception as e:
            # Broad catch is deliberate (iteration_98 review) - a
            # requests.Timeout/ConnectionError against this external,
            # SAP-backed API is just as likely as a SAPGoodsMovementError,
            # and the docstring above promises the approval itself is
            # NEVER blocked by a failed/unreachable movement call.
            last_error = e
            is_auth_failure = "authentication failed" in str(e).lower()
            logger.error(
                f"Goods movement attempt {attempt + 1}/{_GOODS_MOVEMENT_MAX_ATTEMPTS} for {product_id} "
                f"(site {site_id}, {source_warehouse}->{target_warehouse}) failed: {e}"
            )
            if is_auth_failure or attempt == _GOODS_MOVEMENT_MAX_ATTEMPTS - 1:
                break
            logger.warning(
                f"Goods movement attempt {attempt + 1}/{_GOODS_MOVEMENT_MAX_ATTEMPTS} for {product_id} "
                f"hit a transient error, retrying: {e}"
            )
            time.sleep(_GOODS_MOVEMENT_RETRY_DELAY_SECONDS)
    return {"attempted": True, "ok": False, "error": str(last_error)}

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


def _rm_warehouse(site_id: str) -> str:
    return f"{site_id}/{site_id}-RM"


def _sfg_warehouse(site_id: str) -> str:
    return f"{site_id}/{site_id}-SFG"


def submit_issue(db, request_id: str, issued: list, decision: str, store_actor: str, sap_client=None):
    """Store records actual issued quantity per component. If everything
    was issued in full, resolves immediately. If short, the store must pick
    `decision`: "proceed" (continue the order pipeline anyway) or
    "send_to_planner" (defer to the requester to decide).

    Aug 2026, user's explicit business rule: the Goods Movement is ALWAYS
    Raw Material -> Semi-Finished Goods at the request's own site - no
    picking a source warehouse or typing a target bin anymore (that manual
    picker from the previous iteration is gone). `_rm_warehouse`/
    `_sfg_warehouse` build the fixed SAP Logistics Area IDs (confirmed live
    against real inventory data, e.g. "P2/P2-RM" -> "P2/P2-SFG"). Site
    itself is not yet locked to the logged-in user (planned, not built -
    user's words: "will be fixed later") - for now it's whatever site the
    original shortage request was raised for.

    The Owner Party ID can't be picked either now - it's read off whichever
    of the component's own `locations` sits in the RM warehouse (SAP's real
    stock owner for that exact bin, "RI"/"RT"). If the component has no
    stock on file in the RM warehouse at all, the movement is skipped with
    a `{"attempted": False, "reason": ...}` marker (distinguishes "nothing
    to move from" from a genuinely-attempted-and-failed SAP call) - never
    a hard block on the approval itself.

    Also generates a separate `issue_id` (one per submit_issue() call,
    distinct from the Request ID so the two can't be confused when read
    aloud to the requester/planner)."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        return None
    if doc["status"] != "pending":
        raise ValueError(f"This request is no longer pending (current status: {doc['status']})")

    site_id = doc["site_id"]
    source_warehouse = _rm_warehouse(site_id)
    target_warehouse = _sfg_warehouse(site_id)

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
        rm_location = next((loc for loc in (c.get("locations") or []) if loc.get("warehouse_id") == source_warehouse), None)
        movement = None
        if issued_qty > 0 and sap_client is not None:
            if rm_location and rm_location.get("owner"):
                movement = _trigger_goods_movement(
                    sap_client, rm_location["owner"], c["product_id"], source_warehouse, target_warehouse,
                    issued_qty, c.get("unit_of_measure") or "EA", site_id,
                )
            else:
                # Distinguishes "no RM stock on file for this component"
                # (real-world case, e.g. this material's on-hand stock is
                # actually in SFG/QC, not RM) from a genuinely-attempted-
                # and-failed call (testing_agent iteration_100 review).
                movement = {"attempted": False, "ok": False, "reason": "No stock on file in this site's RM warehouse for this component"}
        components.append({
            **c, "issued_qty": issued_qty, "shortfall": shortfall, "goods_movement": movement,
            # Persisted regardless of whether the movement actually fired
            # (testing_agent iteration_98) - the resolved view/journal
            # needs to show "issued from X" without parsing SOAP XML.
            "issued_from_warehouse": source_warehouse if (movement and movement.get("attempted")) else None,
            "issued_from_owner": rm_location.get("owner") if rm_location else None,
        })

    now = datetime.now(timezone.utc)
    update = {
        "components": components, "store_actor": store_actor, "store_decision": decision, "updated_at": now,
        "target_logistics_area_id": target_warehouse,
        "issue_id": _generate_issue_id_for_site(db, site_id),
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
