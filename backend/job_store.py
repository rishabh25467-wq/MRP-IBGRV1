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
