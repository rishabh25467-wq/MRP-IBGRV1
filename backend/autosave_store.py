"""Generic "last-generated-result" persistence so a live fetch or a
background-job result survives page navigation - the same underlying idea as
mrp_plan_store.py's "autosave" half, generalized for other tabs (Open PO
Demand feed, Production Plan/MPS draft) that only need the single
silently-overwritten slot, not mrp_plan_store's extra "named snapshot" side.

One document per `kind` (e.g. "open_po_demand", "mps_draft"), keyed by _id,
overwritten every time that tab's data is freshly fetched/generated - lets
the frontend restore exactly what a user last saw after navigating away and
back, without implying the restored data is still live/fresh (the UI always
labels it as a past snapshot and offers a fresh re-fetch/regenerate).
"""
from datetime import datetime, timezone

COLLECTION_NAME = "tab_autosaves"


def set_autosave(db, kind: str, data: dict, actor: str = None) -> None:
    db[COLLECTION_NAME].update_one(
        {"_id": kind},
        {"$set": {"data": data, "created_at": datetime.now(timezone.utc), "created_by": actor}},
        upsert=True,
    )


def get_autosave(db, kind: str):
    return db[COLLECTION_NAME].find_one({"_id": kind})
