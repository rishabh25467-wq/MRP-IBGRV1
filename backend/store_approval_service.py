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
from typing import Optional

from production_confirmation_service import apply_goods_movement_to_cache, is_usable_stock_status, load_stock_by_product, site_locations_for_product

logger = logging.getLogger(__name__)

COLLECTION = "store_requests"


def refresh_component_locations(db, doc: dict, stock_by_product: dict = None) -> dict:
    """Aug 2026 fix: a component's `locations` breakdown used to be
    captured ONCE when the request was created and never touched again.
    Real incident: a Goods Receipt posted an hour earlier had already
    reached inventory_cache (confirmed live on the Inventory page), but
    an already-open Store Approval request still showed the old, lower
    RM quantity - and worse, start_issue()'s rm_location lookup would
    have silently skipped the movement since the genuinely-new RM
    location didn't exist yet in the frozen snapshot. Called on every
    read (get_request/list_requests/list_balance_pending/journal) AND
    again right before start_issue() computes anything, so both what's
    shown and what the SAP call actually uses are always as fresh as the
    last inventory_cache refresh (~30 min old at worst, not however old
    the request itself is). Mutates and returns `doc` for convenience;
    `stock_by_product` can be pre-loaded once by a caller refreshing many
    docs at once (list views) to avoid re-reading inventory_cache per row.

    Display-only restriction (Aug 2026 user feedback: seeing SFG/FG/other
    warehouses on this screen confused the store person about what's
    actually at THEIR RM warehouse) - only RM and QC (Quality Hold)
    warehouse rows are kept; every other warehouse at the site (SFG, FG,
    Job Work, RTV, Scrap, Segregation, Production...) is dropped from
    what's shown here. QC is kept ON PURPOSE (not just RM) - a real Aug
    2026 requirement to also clearly surface stock currently sitting in
    Quality Hold, distinct from actual usable RM stock. Safe to filter
    here even though this ALSO feeds start_issue()'s movement logic -
    that logic (see run_issue_movements) only ever matches locations
    against the RM warehouse id anyway, so dropping non-RM/QC rows has
    zero effect on which stock actually gets moved, only on what a store
    person sees."""
    stock_by_product = stock_by_product if stock_by_product is not None else load_stock_by_product(db)
    site_id = doc["site_id"]
    rm_warehouse_id = _rm_warehouse(site_id)
    # Aug 2026, user's ask ("is this a RM item or a manufactured part?") -
    # reuses the app's own SAP BOM cache (bom_node_cache) rather than
    # guessing: if SAP has a real BOM/recipe on file for a product_id, it's
    # manufactured internally (a semi-finished part); if SAP has no BOM at
    # all for it, it's a purchased/bought-out raw material (a true leaf
    # item). `found=True` with `groups` populated ⇒ manufactured, `found=
    # False` ⇒ bought-out, no doc yet ⇒ unknown (never checked). Purely a
    # display flag for now - does not affect what's shown/issuable.
    bom_flags = {
        b["_id"]: bool(b.get("found"))
        for b in db["bom_node_cache"].find({"_id": {"$in": [c["product_id"] for c in doc["components"]]}}, {"found": 1})
    }
    for c in doc["components"]:
        fresh = site_locations_for_product(stock_by_product, c["product_id"], site_id)
        if fresh is not None:
            c["locations"] = [
                loc for loc in fresh
                if loc.get("warehouse_id") == rm_warehouse_id or (loc.get("warehouse_id") or "").endswith("-QC")
            ]
        c["is_manufactured"] = bom_flags.get(c["product_id"])
    return doc
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


# Aug 2026 - the store screen was showing the raw SOAP Fault/Log XML
# straight to the warehouse floor (illegible - the actual failure reason
# was buried after 400+ chars of envelope/namespace boilerplate). This
# turns the raw text into a short bilingual (English/Hindi) message,
# matched against the real SAP Application Log wording seen live this
# session; the raw text is kept in `error_detail` (shown only as a hover
# tooltip) so IT/support can still look it up if a pattern isn't covered.
def _clarify_goods_movement_error(raw_error: str, material_id: str) -> dict:
    text = raw_error or ""
    # Sep 22 2026 fix (real incident, STO-000063) - "No inventory items
    # found for external id..." is SAP's OTHER common wording for the
    # exact same "source warehouse doesn't actually have this stock"
    # rejection, just phrased differently - it was leaking straight
    # through as raw SAP text (with internal MOV-xxx/I-xxx IDs) on the
    # Inbound STO Receipt page since only "negative stock" was matched.
    if re.search(r"negative stock not permitted|no inventory items found for external id", text, re.IGNORECASE):
        return {
            "error": f"Not enough stock in the source warehouse to issue this quantity for {material_id}. Issue a lower quantity or check the RM warehouse balance in SAP.",
            "error_hi": f"{material_id} के लिए इतनी मात्रा जारी करने हेतु सोर्स गोदाम (RM) में पर्याप्त स्टॉक नहीं है। कृपया कम मात्रा जारी करें या SAP में गोदाम का बैलेंस जांचें।",
        }
    if re.search(r"logistics area is invalid", text, re.IGNORECASE):
        return {
            "error": f"SAP rejected this movement for {material_id} - the warehouse ID sent was invalid. Contact IT.",
            "error_hi": f"{material_id} के लिए यह मूवमेंट SAP द्वारा अस्वीकृत किया गया - भेजा गया गोदाम ID अमान्य है। कृपया IT टीम से संपर्क करें।",
        }
    if re.search(r"authentication failed", text, re.IGNORECASE):
        return {
            "error": "SAP login failed while trying to move this stock. Contact IT.",
            "error_hi": "यह स्टॉक मूव करने के लिए SAP लॉगिन विफल रहा। कृपया IT टीम से संपर्क करें।",
        }
    if re.search(r"unreachable|timeout|connection", text, re.IGNORECASE):
        return {
            "error": f"Could not reach SAP to move stock for {material_id}. Please retry in a moment.",
            "error_hi": f"{material_id} का स्टॉक मूव करने के लिए SAP से संपर्क नहीं हो सका। कृपया कुछ देर बाद पुनः प्रयास करें।",
        }
    return {
        "error": f"SAP rejected this stock movement for {material_id}. Contact IT with the Request/Issue ID if this keeps happening.",
        "error_hi": f"{material_id} के लिए यह स्टॉक मूवमेंट SAP द्वारा अस्वीकृत किया गया। यदि यह बार-बार हो रहा है तो Request/Issue ID के साथ IT टीम से संपर्क करें।",
    }


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
                result = {
                    **result, "ok": False, "error_detail": f"SAP rejected the movement: {sap_error}",
                    **_clarify_goods_movement_error(sap_error, product_id),
                }
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
    return {"attempted": True, "ok": False, "error_detail": str(last_error), **_clarify_goods_movement_error(str(last_error), product_id)}

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


def find_open_request(db, site_id: str, material_id: str, quantity: float, requester: str) -> Optional[dict]:
    """Sep 12 2026 bug fix (user's explicit report): a real SAP timeout
    (or any other pipeline failure) followed by the user simply retrying
    "Create Production Order" for the exact same material/site/quantity
    used to open a SECOND, genuinely duplicate Store Request every time -
    the store team then saw two (or more) open asks for what was really
    the same need, even though the first one was often already sitting
    there unresolved. Called BEFORE the SAP Proposal is even created (see
    _run_create_and_release_job) so a duplicate retry is blocked before
    it wastes a second real SAP write too, not just before a second
    request doc. No time cutoff needed - if an identical request is
    still open (unresolved), there is never a legitimate reason to open
    a second one for it; a genuinely NEW need can always be created once
    the first is resolved."""
    return db[COLLECTION].find_one({
        "status": {"$in": ["pending", "issuing", "partial_pending_planner"]},
        "site_id": site_id, "material_id": material_id, "quantity": quantity, "requester": requester,
    })


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
        # Sep 14 2026 bug fix (real user report, P9-000110): this used to
        # store the FULL BOM requirement (c["required_qty"]) here even
        # though `available_qty` (SFG stock already staged/ready for THIS
        # production - see production_confirmation_service.
        # _check_availability_against_stock) had already been computed
        # and was itself proof some of the requirement is already covered.
        # E.g. PDQ80110-2 needed 320, had 80 already in P9-SFG -> the
        # Store should only ever be asked to issue the net 240 shortfall,
        # not the full 320 (a real over-issue risk otherwise). Every
        # downstream reader of this field (shortfall math below, the
        # /storeapproval screen, MyStockRequestsTab.jsx, RequestPrintSlip.
        # jsx) already treats `required_qty` as "what the store must
        # issue", so netting it here - once, at creation - keeps all of
        # them correct with no other change needed.
        "components": [
            {
                "product_id": c["product_id"],
                "description": c.get("description"),
                "unit_of_measure": c.get("unit_of_measure"),
                "required_qty": max(0.0, round(c["required_qty"] - (c.get("available_qty") or 0), 4)),
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
    request still needing action or currently mid-issue ("issuing" - a
    background Goods Movement job is actively running against it)."""
    docs = list(db[COLLECTION].find({"status": {"$in": ["pending", "issuing", "partial_pending_planner"]}}).sort("created_at", -1))
    stock_by_product = load_stock_by_product(db)
    return [refresh_component_locations(db, d, stock_by_product) for d in docs]


def list_all_requests(db) -> list:
    """Full journal/history for /storeapproval - every request regardless
    of status, most recent first."""
    docs = list(db[COLLECTION].find({}).sort("created_at", -1))
    stock_by_product = load_stock_by_product(db)
    return [refresh_component_locations(db, d, stock_by_product) for d in docs]


def get_request(db, request_id: str):
    doc = db[COLLECTION].find_one({"_id": request_id})
    return refresh_component_locations(db, doc) if doc else None


def get_request_by_job(db, job_id: str):
    doc = db[COLLECTION].find_one({"job_id": job_id}, sort=[("created_at", -1)])
    return refresh_component_locations(db, doc) if doc else None


# Aug 2026, user's explicit ask: site P3 has NO standard "-RM" warehouse
# in SAP at all - confirmed live (0 rows at "P3/P3-RM" vs 422 at
# "P3/P3-Z1-01-A") that its raw material stock lives entirely in this
# one zone/bin warehouse instead ("P3-RM-Zone-1-01-A"). Every other site
# keeps the default "{site}-RM" pattern - this is the ONLY per-site
# override, not a general zone-scanning mechanism (user's explicit
# choice: just this one warehouse for now, not "any P3-Z*-*").
_SITE_RM_WAREHOUSE_OVERRIDE = {"P3": "P3-Z1-01-A"}


def _rm_warehouse(site_id: str) -> str:
    return f"{site_id}/{_SITE_RM_WAREHOUSE_OVERRIDE.get(site_id, f'{site_id}-RM')}"


def _sfg_warehouse(site_id: str) -> str:
    return f"{site_id}/{site_id}-SFG"


def submit_issue(db, request_id: str, issued: list, decision: str, store_actor: str, sap_client=None):
    """DEPRECATED (Aug 2026) for the live endpoint - fully synchronous,
    kept only because tests/test_gm_edge_cases.py, tests/test_direct_sap_
    goods_movement.py and tests/test_gm_warehouse_id_fix.py still call it
    directly to exercise the RM-matching/goods-movement business logic
    (which is otherwise identical to run_issue_movements() below). The
    original problem: this loop could take long enough (up to 3 retries x
    5s per component x N components) to exceed the platform's ingress/
    Cloudflare timeout, leaving the browser with a hard error even though
    the backend kept working. The live POST /store-requests/{id}/issue
    endpoint uses start_issue() + run_issue_movements() instead, run as a
    trackable background job from server.py (same pattern as
    create-and-release-order) - do not call this from any new code."""
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
        rm_locations_all = [loc for loc in (c.get("locations") or []) if loc.get("warehouse_id") == source_warehouse]
        # Never source a movement from Quality Inspection/Blocked/
        # Restricted-Use stock - confirmed live this tenant's RM stock can
        # carry an "Inspection" status OR a separate CRESTRICTED_IND flag
        # even while stock_status itself reads "Not Assigned" (see
        # production_confirmation_service.is_usable_stock_status).
        rm_location = next((loc for loc in rm_locations_all if is_usable_stock_status(loc.get("stock_status"), loc.get("restricted"))), None)
        movement = None
        if issued_qty > 0 and sap_client is not None:
            if rm_location and rm_location.get("owner"):
                movement = _trigger_goods_movement(
                    sap_client, rm_location["owner"], c["product_id"], source_warehouse, target_warehouse,
                    issued_qty, c.get("unit_of_measure") or "EA", site_id,
                )
            elif rm_locations_all:
                # Real stock exists in this warehouse, but only in a
                # Quality Inspection/Blocked status - not usable, and NOT
                # the same case as "no stock on file at all" below.
                movement = {"attempted": False, "ok": False, "reason": "Stock on file in this site's RM warehouse is held in Quality Inspection/Blocked status - not usable for production until released."}
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


def start_issue(db, request_id: str, issued: list, decision: str, store_actor: str) -> dict:
    """Fast, synchronous first half of the issue flow (Aug 2026 split) -
    validates the request is pending OR "resolved_balance_pending" (a
    reopened request with an outstanding balance - user's explicit Rule 2:
    "if store has 3 of 5, issue 3 now and come back for the balance 2
    later"), computes issued_qty/shortfall per component (pure math, no
    SAP calls), and flips status to "issuing" so a second submit attempt
    is rejected immediately instead of racing the background job. The
    actual SAP Goods Movement calls happen afterward in
    run_issue_movements(), run as a background asyncio task by server.py
    so this endpoint always returns in well under a second - never
    risking the platform's ingress timeout.

    On a reopen, `issued_qty` on each component becomes the CUMULATIVE
    total ever issued (previous rounds' total + this round's new amount,
    clamped to required_qty) - only the NEW delta (`issued_this_round`) is
    ever sent to SAP, never the whole cumulative amount again."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        return None
    if doc["status"] not in ("pending", "resolved_balance_pending"):
        raise ValueError(f"This request is not awaiting a stock issue (current status: {doc['status']})")
    is_reopen = doc["status"] == "resolved_balance_pending"
    # Refresh locations one more time right before computing anything -
    # the store may be acting minutes/hours after they last viewed this
    # request, and run_issue_movements()'s rm_location lookup below reads
    # straight off doc["components"][i]["locations"], so this is what
    # actually determines whether a genuinely-new RM location is found.
    doc = refresh_component_locations(db, doc)

    issued_map = {i["product_id"]: i for i in issued}
    components = []
    shortfall_exists = False
    for c in doc["components"]:
        i = issued_map.get(c["product_id"], {})
        issued_this_round = i.get("issued_qty")
        issued_this_round = 0 if issued_this_round is None else float(issued_this_round)
        if issued_this_round < 0:
            raise ValueError("Issued quantities must be valid, non-negative numbers")
        prev_cumulative = (c.get("issued_qty") or 0) if is_reopen else 0
        # Sep 12 2026 bug fix (real incident, request P1-000007/SI-4426SF):
        # store staff can deliberately issue MORE than the calculated
        # required_qty (e.g. rounding up to a convenient pack size) - the
        # frontend already allows this (validates against actual usable
        # RM stock, not required_qty - see StoreApprovalPage.js's
        # usableRmQty check). This used to clamp the stored cumulative
        # down to required_qty, silently discarding the store's real
        # over-issue (10 KG typed -> only 9.3 KG ever recorded/shown) even
        # though the correct full amount was already being sent to SAP
        # via issued_this_round below - display and SAP were out of sync.
        # No cap here anymore; shortfall still floors at 0 for an over-issue.
        cumulative = prev_cumulative + issued_this_round
        shortfall = max(0.0, round(c["required_qty"] - cumulative, 4))
        if shortfall > 0:
            shortfall_exists = True
        components.append({**c, "issued_qty": cumulative, "issued_this_round": issued_this_round, "shortfall": shortfall})

    if not is_reopen and shortfall_exists and decision not in ("proceed", "send_to_planner"):
        raise ValueError("Some components are short - choose 'proceed' or 'send_to_planner'")

    now = datetime.now(timezone.utc)
    update = {"components": components, "store_actor": store_actor, "status": "issuing", "updated_at": now}
    if not is_reopen:
        update["store_decision"] = decision if shortfall_exists else None
    db[COLLECTION].update_one({"_id": request_id}, {"$set": update})
    return db[COLLECTION].find_one({"_id": request_id})


def run_issue_movements(db, request_id: str, sap_client, progress_cb=None) -> dict:
    """Second half - the actual SAP Goods Movement loop (the slow part,
    up to 3 retries x 5s per component x N components). Runs entirely
    inside the background job; persists each component's real result
    right after its own SAP call (not batched at the end) so progress is
    visible mid-run and never lost if the process is interrupted partway.
    `progress_cb(current, total, product_id)` if given, is called after
    each component so the job doc can be updated with live progress.

    Only moves `issued_this_round` (the NEW delta) per component, never
    the cumulative `issued_qty` - critical on a reopen round, otherwise
    already-moved stock from a previous round would be moved again. A
    component with nothing new to move this round (issued_this_round==0,
    e.g. the store only had stock for SOME of the short components) is
    skipped entirely, preserving its previous round's goods_movement
    result rather than overwriting it with a fresh "not attempted"."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        raise ValueError("Store request not found")
    is_reopen_round = doc.get("resolution") is not None

    site_id = doc["site_id"]
    source_warehouse = _rm_warehouse(site_id)
    target_warehouse = _sfg_warehouse(site_id)
    components = doc["components"]
    total = len(components)

    for idx, c in enumerate(components):
        issued_this_round = c.get("issued_this_round") or 0
        if (c.get("goods_movement") or {}).get("ok") is True:
            # Sep 14 2026 fix (real production incident, request 685734147/
            # P9-000121: L792 moved successfully - SAP Goods Movement
            # 276768 - then the background job died before reaching
            # PALL3286, leaving the request stuck in "issuing" forever
            # since revert_to_prior_status_if_safe correctly refuses to
            # touch a request with a real movement already on it - see
            # that function's docstring). This job must be safely
            # RESUMABLE (see the /resume-issue endpoint in server.py) -
            # a component that already succeeded THIS round must never
            # be re-sent to SAP, or the resume would physically double-
            # move real stock.
            if progress_cb:
                progress_cb(idx + 1, total, c["product_id"])
            continue
        if issued_this_round > 0 and sap_client is not None:
            rm_locations_all = [loc for loc in (c.get("locations") or []) if loc.get("warehouse_id") == source_warehouse]
            rm_location = next((loc for loc in rm_locations_all if is_usable_stock_status(loc.get("stock_status"), loc.get("restricted"))), None)
            if rm_location and rm_location.get("owner"):
                movement = _trigger_goods_movement(
                    sap_client, rm_location["owner"], c["product_id"], source_warehouse, target_warehouse,
                    issued_this_round, c.get("unit_of_measure") or "EA", site_id,
                )
                if movement.get("ok") and not is_dry_run():
                    # Movement genuinely landed in SAP - reflect it in
                    # inventory_cache RIGHT NOW so the Production
                    # Confirmation Stock/Short badge doesn't have to wait
                    # up to 30 min for the next scheduled refresh (Aug
                    # 2026 user feedback).
                    apply_goods_movement_to_cache(db, c["product_id"], source_warehouse, target_warehouse, issued_this_round)
            elif rm_locations_all:
                movement = {"attempted": False, "ok": False, "reason": "Stock on file in this site's RM warehouse is held in Quality Inspection/Blocked status - not usable for production until released."}
            else:
                movement = {"attempted": False, "ok": False, "reason": "No stock on file in this site's RM warehouse for this component"}
            c["goods_movement"] = movement
            c["issued_from_warehouse"] = source_warehouse if movement.get("attempted") else None
            c["issued_from_owner"] = rm_location.get("owner") if rm_location else None
            db[COLLECTION].update_one({"_id": request_id}, {"$set": {f"components.{idx}": c}})
        if progress_cb:
            progress_cb(idx + 1, total, c["product_id"])

    now = datetime.now(timezone.utc)
    # Sep 2026 fix (user's explicit report: request P1-000014 closed as
    # "resolved" even though SAP rejected the goods movement outright -
    # "Negative stock not permitted" - and 0 EA actually moved). The
    # store's recorded issued_qty covering required_qty is only "no
    # shortfall" on paper; if SAP itself rejected THIS round's attempt,
    # nothing actually landed in SAP's ledger, so the request must stay
    # reopenable rather than closing as fully resolved.
    sap_rejected_this_round = any(
        (c.get("issued_this_round") or 0) > 0 and not (c.get("goods_movement") or {}).get("ok")
        for c in components
    )
    shortfall_exists = sap_rejected_this_round or any(c["shortfall"] > 0 for c in components)
    decision = doc.get("store_decision")
    update = {
        "updated_at": now, "target_logistics_area_id": target_warehouse,
        "issue_id": _generate_issue_id_for_site(db, site_id),
    }
    if not shortfall_exists:
        update.update({"status": "resolved", "resolution": "balance_completed" if is_reopen_round else "full_issue", "resolved_at": now})
    elif sap_rejected_this_round:
        # SAP itself rejected the movement - nothing actually landed in
        # SAP's ledger, so this MUST stay reopenable (closing would
        # falsely claim material moved to production when it didn't).
        # User's explicit ask (Sep 11 2026): this is the ONLY case that
        # still uses "resolved_balance_pending" - every other partial
        # issue below now closes for good, see next branch.
        update.update({"status": "resolved_balance_pending", "resolution": "balance_pending" if is_reopen_round else "store_proceeded_partial", "resolved_at": now})
    elif not is_reopen_round and decision == "send_to_planner":
        update.update({"status": "partial_pending_planner"})
    else:
        # Sep 11 2026 bug fix, user's explicit ask - reverses "Rule 2":
        # a genuine partial issue (SAP actually accepted whatever amount
        # was issued, just less than required) now closes the request
        # for good, exactly like a full issue - it is NEVER reopenable
        # again for the remaining shortfall (the old "Issue Remaining
        # Balance" screen). Real production incident: request 685734147
        # stayed reopenable (PALL3286, 32 of 83.87 issued) when it
        # should have closed immediately.
        update.update({"status": "resolved", "resolution": "balance_completed" if is_reopen_round else "store_proceeded_partial", "resolved_at": now})
    db[COLLECTION].update_one({"_id": request_id}, {"$set": update})
    return db[COLLECTION].find_one({"_id": request_id})


def mark_order_resumed(db, request_id: str) -> None:
    """Called once, right after _resume_order_creation_job() actually
    fires for this request - guards against firing it a second time on a
    later reopen round (the order-creation job has already moved on)."""
    db[COLLECTION].update_one({"_id": request_id}, {"$set": {"order_resumed": True}})


# Statuses (Aug 2026) that only ever exist while an asyncio task is
# actively running the Goods Movement loop inside this process - same
# reasoning as job_store.ORPHANABLE_JOB_STATUSES.
ORPHANABLE_REQUEST_STATUSES = {"issuing"}


def revert_to_prior_status_if_safe(db, request_id: str) -> bool:
    """Shared by recover_orphaned_issues() (startup) and the background
    job's own except-handler (a mid-run exception, no restart needed) -
    only safe to hand the request back to its PRIOR status (so it can be
    cleanly resubmitted) if NONE of its components got a NEW goods_movement
    result yet this round. The prior status is "resolved_balance_pending"
    if this was a reopen round (doc already has a `resolution` from an
    earlier round), otherwise plain "pending" - reverting a reopen to
    "pending" would silently reset the cumulative-issued bookkeeping on
    the next resubmit.

    Sep 14 2026: if a request already has a real SAP movement this round,
    it is NOT reverted (unchanged) - but it's no longer a dead end either.
    See `can_resume_stuck_issue` + the `/store-requests/{id}/resume-issue`
    endpoint in server.py, which safely re-runs run_issue_movements
    (idempotent now - see its own comment) to finish the REMAINING
    components instead of leaving the request stuck in "issuing" forever."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc or doc["status"] != "issuing":
        return False
    if any(c.get("issued_this_round") and c.get("goods_movement") for c in doc.get("components", [])):
        logger.warning(
            f"Store request {request_id} is stuck in 'issuing' AND already has at least one real SAP Goods "
            f"Movement recorded this round - left as-is, needs manual review before any resubmission."
        )
        return False
    prior_status = "resolved_balance_pending" if doc.get("resolution") is not None else "pending"
    db[COLLECTION].update_one({"_id": request_id}, {"$set": {"status": prior_status}})
    return True


def can_resume_stuck_issue(db, request_id: str) -> dict:
    """Sep 14 2026 fix - real production incident, request 685734147/
    P9-000121 (see run_issue_movements' comment above). A request that's
    genuinely stuck in "issuing" (its background job died - crashed,
    process restarted/redeployed - with at least one real SAP movement
    already on it, so revert_to_prior_status_if_safe correctly left it
    alone) used to have NO way forward at all short of a direct DB edit.
    Returns {"can_resume": bool, "reason": str}."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc or doc["status"] != "issuing":
        return {"can_resume": False, "reason": "This request is not currently stuck issuing stock."}
    return {"can_resume": True, "reason": None}


def reconcile_untracked_movement(db, request_id: str, product_id: str, external_id: str, actor: str) -> dict:
    """Sep 14 2026 fix - covers the WORSE version of the 685734147/
    P9-000121 incident: SAP genuinely confirmed the movement (by our own
    `_EMERGENTBOM` technical user - the SAP call itself DID succeed) but
    the process died in the gap between that call returning and this
    component's own `db.update_one` a few lines later in
    run_issue_movements, so our own record never learned about it.
    Simply calling `/resume-issue` on a request like this would call SAP
    AGAIN for the same component - a real, physical DUPLICATE stock
    move. This lets an admin record an externally-confirmed SAP
    Goods Movement ID onto the exact component/round WITHOUT ever
    calling SAP, so a subsequent resume-issue correctly skips it (see
    run_issue_movements' idempotency check) instead of re-sending it."""
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        raise ValueError("Store request not found")
    if doc["status"] != "issuing":
        raise ValueError(f"This request is not stuck issuing stock (current status: {doc['status']})")
    components = doc["components"]
    idx = next((i for i, c in enumerate(components) if c["product_id"] == product_id), None)
    if idx is None:
        raise ValueError(f"No component {product_id} on this request")
    existing = components[idx].get("goods_movement") or {}
    if existing.get("ok") is True:
        raise ValueError(f"{product_id} already has a recorded successful movement ({existing.get('external_id')}) - refusing to overwrite it")
    if not (components[idx].get("issued_this_round") or 0) > 0:
        raise ValueError(f"{product_id} has no issued quantity this round to reconcile")
    now = datetime.now(timezone.utc)
    movement = {
        "attempted": True, "ok": True, "external_id": external_id,
        "reconciled_manually": True, "reconciled_by": actor, "reconciled_at": now.isoformat(),
        "reconciled_note": "SAP confirmed this movement already posted (by our own SAP technical user) - our own record of it was lost when the background job died mid-run. Recorded without a new SAP call to avoid a duplicate physical stock move.",
    }
    components[idx]["goods_movement"] = movement
    components[idx]["issued_from_warehouse"] = _rm_warehouse(doc["site_id"])
    db[COLLECTION].update_one({"_id": request_id}, {"$set": {f"components.{idx}": components[idx], "updated_at": now}})
    return db[COLLECTION].find_one({"_id": request_id})


def recover_orphaned_issues(db, message: str) -> int:
    """Call once at process startup. A request stuck in "issuing" means
    the background job that would move it forward died with the previous
    process."""
    recovered = 0
    for doc in db[COLLECTION].find({"status": {"$in": list(ORPHANABLE_REQUEST_STATUSES)}}):
        if revert_to_prior_status_if_safe(db, doc["_id"]):
            db[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": {"recovery_note": message}})
            recovered += 1
    return recovered


def list_balance_pending(db) -> list:
    """Dedicated view for the store team of requests where SAP itself
    REJECTED the movement (nothing actually posted) - the only case that
    still stays reopenable (Sep 11 2026, user's explicit ask reversed the
    old "Rule 2": a genuine partial issue that SAP actually accepted now
    closes for good instead of staying open for a later balance).

    Extra defensive filter (Sep 11 2026, real incident: legacy request
    P9-000051, created under the OLD rule before this reversal, sat here
    with a component whose movement had actually SUCCEEDED - "Moved
    (275119)" - yet still showed as actionable) - only surface requests
    that genuinely have a failed/not-ok movement on a short component.
    Doesn't trust the status field alone, since old records predating
    this fix can carry it for a different reason."""
    docs = list(db[COLLECTION].find({"status": "resolved_balance_pending"}).sort("updated_at", -1))
    docs = [d for d in docs if any(c.get("shortfall", 0) > 0 and c.get("goods_movement") and c["goods_movement"].get("ok") is not True for c in d.get("components", []))]
    stock_by_product = load_stock_by_product(db)
    return [refresh_component_locations(db, d, stock_by_product) for d in docs]


def planner_decision(db, request_id: str, decision: str, planner_actor: str):
    doc = db[COLLECTION].find_one({"_id": request_id})
    if not doc:
        return None
    if doc["status"] != "partial_pending_planner":
        raise ValueError(f"This request is not awaiting a planner decision (current status: {doc['status']})")

    now = datetime.now(timezone.utc)
    if decision == "approve":
        # Sep 11 2026, same rule as run_issue_movements above: approving
        # a partial now closes for good - it does NOT stay reopenable
        # just because a shortfall remains.
        new_status = "resolved"
    else:
        new_status = "cancelled"
    update = {"status": new_status, "planner_actor": planner_actor, "planner_decision": decision, "updated_at": now}
    if new_status == "resolved":
        update.update({"resolution": "planner_approved_partial", "resolved_at": now})
    db[COLLECTION].update_one({"_id": request_id}, {"$set": update})
    return db[COLLECTION].find_one({"_id": request_id})
