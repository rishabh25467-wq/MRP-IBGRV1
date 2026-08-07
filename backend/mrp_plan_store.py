"""Persistence for MRP Plan snapshots (mrp_service.build_mrp_plan() output).

Two kinds of persisted plans, both in the same `saved_mrp_plans` collection:
  - Autosave: exactly one document (_id="autosave"), silently overwritten
    every time a plan finishes generating - purely to protect against
    losing the last-generated plan on a page refresh/browser close, not a
    user-facing "saved plan" (never shown in the Saved Plans list).
  - Named snapshots: one document per explicit "Save As" click, kept
    forever until the user deletes it - for week-to-week comparison or
    sharing, listed on the Production Plan page's "Saved Plans" tab. Each
    snapshot embeds the full plan (every component, every demand line,
    i.e. exactly which PO lines were selected at generation time), so
    reloading it later shows precisely what was seen back then.
"""
import uuid
from datetime import datetime, timezone

AUTOSAVE_ID = "autosave"


def set_autosave(db, plan: dict, actor: str = None):
    db["saved_mrp_plans"].update_one(
        {"_id": AUTOSAVE_ID},
        {"$set": {"plan": plan, "created_at": datetime.now(timezone.utc), "created_by": actor, "is_autosave": True, "name": "Last Generated"}},
        upsert=True,
    )


def get_autosave(db) -> dict:
    return db["saved_mrp_plans"].find_one({"_id": AUTOSAVE_ID})


def save_named_plan(db, name: str, actor: str, plan: dict) -> dict:
    doc = {
        "_id": str(uuid.uuid4()), "name": name, "created_by": actor,
        "created_at": datetime.now(timezone.utc), "is_autosave": False, "plan": plan,
    }
    db["saved_mrp_plans"].insert_one(doc)
    return doc


def list_named_plans(db) -> list:
    return list(db["saved_mrp_plans"].find({"is_autosave": {"$ne": True}}).sort("created_at", -1))


def get_plan(db, plan_id: str) -> dict:
    return db["saved_mrp_plans"].find_one({"_id": plan_id})


def delete_plan(db, plan_id: str) -> bool:
    """Autosave is exempt from deletion - it's not a user-visible entry."""
    result = db["saved_mrp_plans"].delete_one({"_id": plan_id, "is_autosave": {"$ne": True}})
    return result.deleted_count > 0
