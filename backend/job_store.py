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
from datetime import datetime, timezone, timedelta

COLLECTION_NAME = "background_jobs"


# 7 days, not 24h (deployment-scan-flagged, fixed 2026-08-29) - these are
# transient UI-polling progress docs, not the business record itself (the
# real outcome lands on the STO/shipment/production-order doc via each
# job's own finalize_* step), but a week gives ops enough room to
# troubleshoot a stuck/failed job before its tracking doc disappears.
JOB_RETENTION_SECONDS = 7 * 86400


def ensure_indexes(db) -> None:
    """TTL index so finished/abandoned job docs get cleaned up automatically
    instead of growing the collection forever."""
    try:
        db[COLLECTION_NAME].create_index("created_at", expireAfterSeconds=JOB_RETENTION_SECONDS, name="created_at_1")
    except Exception:
        # Index already exists with the old 86400 TTL - update it in place
        # instead of dropping (avoids a brief window with no TTL index at all).
        db.command("collMod", COLLECTION_NAME, index={"keyPattern": {"created_at": 1}, "expireAfterSeconds": JOB_RETENTION_SECONDS})


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


def recover_orphaned_jobs(db, message: str) -> list:
    """Call once at process startup, before any new job can be created -
    marks every job still sitting in an ORPHANABLE_JOB_STATUSES status as
    failed with `message`, so the UI never keeps polling a job that will
    never resolve on its own. Returns the orphaned job docs (so the
    caller can react per `kind`, e.g. persisting a receipt_error onto an
    inbound_receipt job's own STO doc - see server.py's startup block).

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
        {"production_proposal_id": 1, "production_order_id": 1, "kind": 1, "sto_id": 1},
    ))
    for job in orphaned:
        db[COLLECTION_NAME].update_one({"_id": job["_id"]}, {"$set": {
            "status": "failed", "phase": "failed", "error": message,
            "result": {
                "reason": "pipeline_error",
                "production_proposal_id": job.get("production_proposal_id"),
                "production_order_id": job.get("production_order_id"),
            },
        }})
    return orphaned


# Sep 2 2026 (user's explicit ask: an internal-only reliability report,
# "between you and me", not for regular staff) - PLAYWRIGHT_JOB_KINDS
# covers every job kind that's actually a Playwright browser-automation
# job (Supplier GRN, Inbound STO Receipt, and STO Outbound combine+
# release/"submit_sto") - every other `kind` in this collection is a
# plain API-driven background job and irrelevant to this report.
PLAYWRIGHT_JOB_KINDS = ["supplier_grn", "inbound_receipt", "submit_sto"]

# Jobs older than JOB_RETENTION_SECONDS (7 days) are already gone via the
# TTL index above - the report can never show a longer window than that,
# no matter what `days` is requested.
RESTART_INTERRUPTION_MARKER = "Interrupted by a backend restart/deploy"


def build_reliability_report(db, days: int) -> dict:
    """Real vs backend-restart-noise success rate per Playwright job
    kind, over the last `days` days (capped by the 7-day job retention
    above). "Real failures" excludes any job whose error is the
    dev-environment backend-restart marker (job_store.recover_orphaned_
    jobs sets this) - those aren't SAP/Playwright reliability at all,
    just this job having been mid-flight during a code deploy/hot-reload."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    by_kind = {}
    for kind in PLAYWRIGHT_JOB_KINDS:
        jobs = list(db[COLLECTION_NAME].find({"kind": kind, "created_at": {"$gte": since}}, {"status": 1, "error": 1, "created_at": 1}))
        total = len(jobs)
        done = sum(1 for j in jobs if j.get("status") == "done")
        failed = [j for j in jobs if j.get("status") == "failed"]
        restart_interrupted = [j for j in failed if RESTART_INTERRUPTION_MARKER in (j.get("error") or "")]
        real_failures = [j for j in failed if j not in restart_interrupted]
        reason_counts = {}
        for j in real_failures:
            reason = (j.get("error") or "Unknown error").strip()[:200]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        real_denominator = total - len(restart_interrupted)
        by_kind[kind] = {
            "total_jobs": total,
            "done": done,
            "restart_interrupted": len(restart_interrupted),
            "real_failures": len(real_failures),
            "real_success_rate_pct": round(done / real_denominator * 100, 1) if real_denominator > 0 else None,
            "real_failure_reasons": sorted(
                [{"error": reason, "count": count} for reason, count in reason_counts.items()],
                key=lambda r: -r["count"],
            ),
        }
    return {"since": since.isoformat(), "days": days, "by_kind": by_kind}

