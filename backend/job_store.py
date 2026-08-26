"""Shared MongoDB-backed background job tracking.

Replaces what used to be plain in-memory Python dicts in server.py
(purchasing_plan_jobs, mrp_plan_jobs, push_all_to_sap_jobs, inventory_jobs,
deep_backfill_jobs) for every long-running action that runs as a background
task and gets polled by the frontend for status (Purchasing Plan / MRP Plan
generation, Inventory Refresh, Deep Backfill, bulk Push-to-SAP).

An in-memory dict only works if a single process ever handles both the
"start job" request and every later "poll status" request for it. Production
runs multiple backend worker processes/replicas for scale, so a job started
on one worker was invisible to a status poll answered by a different one -
surfacing to users as a 404 "Unknown job_id" (and, since the frontend was
still waiting on a job it thought was running, a popup that looked
permanently stuck). A single Mongo collection, keyed by job_id, is visible
to every worker/replica no matter which one handles a given request.
"""
from datetime import datetime, timezone

COLLECTION_NAME = "background_jobs"


def ensure_indexes(db) -> None:
    """TTL index so finished/abandoned job docs get cleaned up automatically
    after a day instead of growing the collection forever."""
    db[COLLECTION_NAME].create_index("created_at", expireAfterSeconds=86400)


def create_job(db, job_id: str, initial_state: dict) -> None:
    db[COLLECTION_NAME].insert_one({"_id": job_id, "created_at": datetime.now(timezone.utc), **initial_state})


def update_job(db, job_id: str, update: dict) -> None:
    db[COLLECTION_NAME].update_one({"_id": job_id}, {"$set": update})


def get_job(db, job_id: str):
    """Returns the job's state dict (without the _id key), or None if no
    such job_id exists."""
    doc = db[COLLECTION_NAME].find_one({"_id": job_id})
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


# Statuses that only ever exist while an asyncio task is actively running
# INSIDE this process - "waiting_store_approval"/"partial_pending_planner"
# are deliberately excluded (those are real, human-driven pauses that must
# survive a restart). If a job doc is still in one of these at process
# startup, the task that would ever move it forward is gone for good (the
# previous process crashed, was redeployed, or hot-reloaded) - it will
# otherwise sit "stuck forever" in the UI since nothing will ever poll it
# to completion again. Real incident (Aug 2026): a user's in-flight
# Create Production Order job showed "Checking Stock" for 5+ minutes after
# an unrelated backend restart killed its background task mid-run.
ORPHANABLE_JOB_STATUSES = {"running", "checking_stock", "creating_proposal", "waiting_for_order", "releasing_order"}


def recover_orphaned_jobs(db, message: str) -> int:
    """Call once at process startup, before any new job can be created -
    marks every job still sitting in an ORPHANABLE_JOB_STATUSES status as
    failed with `message`, so the UI never keeps polling a job that will
    never resolve on its own. Returns how many were recovered.

    Aug 2026 fix (real incident: Proposal 225857 for PL-0037A) - `result`
    used to stay untouched (null, since these jobs never got a chance to
    set it themselves), which hid the frontend's "Resume" button even
    though a real SAP Proposal (and sometimes an Order) already existed
    and just needed finishing. Every ORPHANABLE job that already has a
    top-level production_proposal_id/production_order_id (set as soon as
    each is known, well before the job ever finishes) now carries them
    into `result`, in the same shape a normal failure uses."""
    orphaned = list(db[COLLECTION_NAME].find(
        {"status": {"$in": list(ORPHANABLE_JOB_STATUSES)}},
        {"production_proposal_id": 1, "production_order_id": 1},
    ))
    for job in orphaned:
        db[COLLECTION_NAME].update_one({"_id": job["_id"]}, {"$set": {
            "status": "failed", "error": message,
            "result": {
                "reason": "pipeline_error",
                "production_proposal_id": job.get("production_proposal_id"),
                "production_order_id": job.get("production_order_id"),
            },
        }})
    return len(orphaned)
