"""AI-based BOM component categorization using GPT-5.4, persisted forever.

Classifies each BOM leaf component (by product ID + description) into a
broad material/type category, chosen from a taxonomy stored in the
`category_master` Mongo collection (seeded from DEFAULT_CATEGORIES, growable
via the Admin > Component Master page's "+ Add Category" - see
get_categories/add_category). Every classification result is written to the
`component_master` Mongo collection and read back on every subsequent call
instead of asking the AI again - this is what makes categories CONSISTENT
across BOM loads and Purchasing Plan runs (previously, every run re-asked
the AI fresh, so the same borderline item like an RFID tag could land in a
different category each time depending on non-determinism and which other
items it was batched with). A human correction saved via the Admin page
(category_source="manual") is never overwritten by this module - only the
Admin page's own "Re-Categorise" action can force a fresh AI pass on an item.
"""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from emergentintegrations.llm.chat import LlmChat, UserMessage

from sap_material_client import SAPMaterialError, SAPMaterialAuthError

logger = logging.getLogger(__name__)

BATCH_SIZE = 80
MODEL = "gpt-5.4"
CATEGORY_COLLECTION = "category_master"

# Throttled background drawing-URL backfill - deliberately gentle (same
# spirit as the Inventory page's deep UUID backfill) since this SAP tenant
# is known to hit connection timeouts under concurrent load.
DRAWING_URL_BACKFILL_BATCH_SIZE = 100
DRAWING_URL_BACKFILL_MAX_WORKERS = 3

DEFAULT_CATEGORIES = [
    "Raw Material", "Hardware", "Plastic", "Sheet Metal",
    "Zinc", "Copper Alloy", "Aluminum", "Steel", "Stainless Steel",
    "Rubber/Elastomer", "Electronics", "Packaging", "Stationery",
    "Adhesive", "Label/Printing", "Sub-Assembly", "Finished Goods", "Other",
]


def get_categories(db) -> list[str]:
    """The current category taxonomy - seeded once from DEFAULT_CATEGORIES,
    then whatever's been added since via the Admin > Component Master page's
    "+ Add Category". Every AI categorization call is constrained to
    EXACTLY this (possibly since-grown) list, so a category added today is
    available to both manual selection AND future AI classification."""
    collection = db[CATEGORY_COLLECTION]
    if collection.count_documents({}) == 0:
        now = datetime.now(timezone.utc)
        collection.insert_many([{"_id": cat, "source": "default", "created_at": now} for cat in DEFAULT_CATEGORIES])
    return [doc["_id"] for doc in collection.find({}).sort("_id", 1)]


def add_category(db, name: str) -> list[str]:
    """Adds a new category to the master taxonomy (case-insensitive
    duplicate check - if 'plastic' already exists, adding 'Plastic' is a
    no-op that just returns the existing list unchanged). Returns the full,
    updated category list."""
    name = name.strip()
    if not name:
        return get_categories(db)
    collection = db[CATEGORY_COLLECTION]
    existing = get_categories(db)
    if name.lower() in {c.lower() for c in existing}:
        return existing
    collection.insert_one({"_id": name, "source": "manual", "created_at": datetime.now(timezone.utc)})
    return get_categories(db)


def delete_category(db, name: str) -> list[str]:
    """Removes a category from the master taxonomy. Any components already
    assigned to it keep their existing category value (not cleared) - it
    just stops appearing in the dropdown for future selection/AI runs."""
    db[CATEGORY_COLLECTION].delete_one({"_id": name})
    return get_categories(db)


def backfill_product_uuids(db) -> int:
    """One-time/repeatable maintenance job: `component_master` only started
    capturing `product_uuid` (needed for the SAP Push-to-SAP write-back)
    from this feature's introduction onward, so every component seen in a
    BOM Explorer search or Purchasing Plan run BEFORE that has no SAP link.
    Most of them are still recoverable "for free" though - they already
    exist as a leaf/child entry somewhere inside the cached BOM trees in
    `bom_node_cache`, which has always carried product_uuid per item. This
    scans that cache once and fills in any missing product_uuid it finds.
    Returns the number of components backfilled."""
    uuid_by_product_id = {}
    for doc in db["bom_node_cache"].find({}, {"groups.items.product_id": 1, "groups.items.product_uuid": 1}):
        for group in doc.get("groups", []):
            for item in group.get("items", []):
                product_id = item.get("product_id")
                product_uuid = item.get("product_uuid")
                if product_id and product_uuid and product_id not in uuid_by_product_id:
                    uuid_by_product_id[product_id] = product_uuid

    missing_ids = [
        doc["_id"] for doc in db["component_master"].find(
            {"product_uuid": {"$in": [None]}, "_id": {"$in": list(uuid_by_product_id.keys())}}, {"_id": 1}
        )
    ] + [
        doc["_id"] for doc in db["component_master"].find(
            {"product_uuid": {"$exists": False}, "_id": {"$in": list(uuid_by_product_id.keys())}}, {"_id": 1}
        )
    ]

    updated = 0
    for product_id in set(missing_ids):
        db["component_master"].update_one(
            {"_id": product_id}, {"$set": {"product_uuid": uuid_by_product_id[product_id]}}
        )
        updated += 1
    return updated


def backfill_drawing_urls(db, sap_material_client, batch_size: int = DRAWING_URL_BACKFILL_BATCH_SIZE) -> dict:
    """Background maintenance job (called on a periodic scheduler loop, see
    server.py's start_drawing_url_backfill_loop) that grows drawing/
    documentation link AND attachment-comment coverage across
    `component_master` over time, mirroring the Inventory page's deep UUID
    backfill's throttled-batch spirit but fully automatic (no manual
    button click needed).

    For each component not yet checked, calls the already-authorized
    QueryMaterialIn service (same one used for UUID resolution) to read its
    Material master's AttachmentFolder.Document node(s) - this tenant uses
    ExternalLinkWebURI to point at drawings hosted on a separate
    shared-drive portal (e.g. rampgroup.net), and occasionally a
    Description field carrying a human comment/ECR note (e.g. "ECR No. 83
    raised to correct the Marked identification of Left and Right Arm.") -
    NOT every material has either. Only marks `drawing_url_checked=True` on
    a genuinely successful SAP response (found something, or confirmed
    there isn't any) - a transient network error or a missing-authorization
    fault leaves the item unchecked so a later cycle retries it, rather
    than silently giving up on it forever. Returns {"checked", "found"}."""
    targets = [
        doc["_id"] for doc in db["component_master"].find(
            {"drawing_url_checked": {"$ne": True}}, {"_id": 1}
        ).limit(batch_size)
    ]
    if not targets:
        return {"checked": 0, "found": 0}

    checked = 0
    found = 0
    auth_error_seen = False

    def fetch_one(product_id):
        info = sap_material_client.resolve_material_info(product_id)
        return product_id, info

    with ThreadPoolExecutor(max_workers=DRAWING_URL_BACKFILL_MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one, pid): pid for pid in targets}
        for future in as_completed(futures):
            product_id = futures[future]
            try:
                _, info = future.result()
            except SAPMaterialAuthError as e:
                auth_error_seen = True
                logger.error(f"Drawing URL backfill: SAP rejected the Material lookup (missing authorization) - stopping this cycle: {e}")
                break
            except SAPMaterialError as e:
                logger.warning(f"Drawing URL backfill: lookup failed for '{product_id}', will retry next cycle: {e}")
                continue
            except Exception as e:
                logger.warning(f"Drawing URL backfill: network error for '{product_id}', will retry next cycle: {e}")
                continue

            update = {
                "drawing_url_checked": True,
                "drawing_url": info["drawing_url"],
                "comments": info.get("comments") or [],
            }
            if info["uuid"]:
                update["product_uuid"] = info["uuid"]
            db["component_master"].update_one({"_id": product_id}, {"$set": update}, upsert=True)
            checked += 1
            if info["drawing_url"] or info.get("comments"):
                found += 1

    return {"checked": checked, "found": found, "auth_error": auth_error_seen}


def _build_system_message(categories: list[str]) -> str:
    return (
        "You are an expert manufacturing engineer who classifies Bill of Materials (BOM) "
        "line items into a single, concise material/type category based on their Product ID "
        "and Description. You MUST choose from EXACTLY this list of categories - do not invent "
        f"new ones, do not rename them, reuse the exact spelling: {', '.join(categories)}. "
        "Only use 'Other' if truly nothing else is a reasonable fit.\n\n"
        "DISAMBIGUATION RULES (apply these before general judgment):\n"
        "- Any screw, washer, nut, bolt, rivet, or threaded fastener -> ALWAYS 'Hardware', never a "
        "separate 'Fastener' category.\n"
        "- Spacers, standoffs, bushings, and grommets are NOT fasteners even though they sound "
        "mechanical - classify them by their actual material (usually 'Plastic' unless the "
        "description explicitly says metal/steel/aluminum/zinc, in which case use that metal "
        "category). Do NOT default them to 'Hardware'.\n"
        "- RFID tags, RFID labels, chips, sensors, and any embedded electronic component -> ALWAYS "
        "'Electronics', even if the item also has a printed label/adhesive component. Do not use "
        "'Label/Printing' for anything with 'RFID' in its description.\n"
        "- Plain printed labels, stickers, tags, or barcode labels WITHOUT any electronic component "
        "-> 'Label/Printing'.\n"
        "- An item whose description clearly names it as a final saleable product/kit (a complete "
        "device, mount, or unit customers buy or ship as-is) -> 'Finished Goods'. An item described "
        "as an '...Assy' or sub-unit that reads like it gets built INTO something bigger (e.g. an "
        "arm, bracket, or wall-plate assembly that is part of a larger mount) -> 'Sub-Assembly' "
        "instead, even though it is also an assembled multi-part item. When genuinely unsure which "
        "of the two, prefer 'Sub-Assembly'.\n\n"
        "Respond with ONLY a valid JSON object mapping each product_id to its category string, "
        "no markdown, no explanation."
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence_match:
        text = fence_match.group(1).strip()
    return json.loads(text)


class BomCategorizerError(Exception):
    pass


async def _ai_categorize(items: list[dict], categories: list[str]) -> dict:
    """items: [{"product_id": str, "description": str | None}, ...] (already
    deduplicated by caller). categories: the current taxonomy (get_categories).
    Returns {product_id: category}. Raises BomCategorizerError on any
    AI/parsing failure."""
    api_key = os.environ["EMERGENT_LLM_KEY"]
    system_message = _build_system_message(categories)
    results = {}
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        prompt_lines = [f"{item['product_id']} :: {item.get('description') or 'No description'}" for item in batch]
        user_text = (
            "Classify each of these BOM items (format 'product_id :: description'):\n\n"
            + "\n".join(prompt_lines)
        )

        chat = LlmChat(
            api_key=api_key,
            session_id=f"bom-categorize-{i}",
            system_message=system_message,
        ).with_model("openai", MODEL)

        try:
            response_text = await chat.send_message(UserMessage(text=user_text))
        except Exception as e:
            raise BomCategorizerError(f"AI categorization request failed: {e}")

        try:
            parsed = _extract_json(response_text)
        except (json.JSONDecodeError, TypeError) as e:
            raise BomCategorizerError(f"AI returned an unparseable response: {e}")

        results.update(parsed)

    return results


async def categorize_items(items: list[dict], db) -> dict:
    """items: [{"product_id": str, "description": str | None}, ...].
    Returns {product_id: category} for every item that has (or gets) one.
    Items already categorized (AI or manual) are read straight from Mongo -
    no AI call, no chance of drifting to a different label this run. Only
    genuinely new product_ids are sent to the AI; results are persisted
    immediately so every future call reuses them."""
    collection = db["component_master"]
    unique_items = list({item["product_id"]: item for item in items if item.get("product_id")}.values())
    if not unique_items:
        return {}

    ids = [item["product_id"] for item in unique_items]
    existing = {doc["_id"]: doc for doc in collection.find({"_id": {"$in": ids}})}

    results = {}
    to_categorize = []
    for item in unique_items:
        doc = existing.get(item["product_id"])
        if doc and doc.get("category"):
            results[item["product_id"]] = doc["category"]
        else:
            to_categorize.append(item)
        # First time this product_id is ever seen, seed a bare record (no
        # category yet) so it shows up on the Admin > Component Master page
        # even before an AI/manual category is assigned.
        if not doc:
            collection.update_one(
                {"_id": item["product_id"]},
                {"$set": {"description": item.get("description")},
                 "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        # product_uuid is needed for the SAP Safety Stock/Lead Time write-back
        # (Admin > Push to SAP) - persist it whenever we see it, even for
        # already-categorized items, since older records may predate this.
        if item.get("product_uuid"):
            collection.update_one({"_id": item["product_id"]}, {"$set": {"product_uuid": item["product_uuid"]}})

    if not to_categorize:
        return results

    categories = get_categories(db)
    ai_results = await _ai_categorize(to_categorize, categories)
    now = datetime.now(timezone.utc)
    for product_id, category in ai_results.items():
        collection.update_one(
            {"_id": product_id},
            {"$set": {"category": category, "category_source": "ai", "categorized_at": now}},
        )
    results.update(ai_results)
    return results


async def categorize_full_inventory(db, items: list[dict]) -> dict:
    """Bulk-categorizes every item CURRENTLY on the Inventory page - unlike
    categorize_items above, which BOM Explorer/Purchasing Plan only ever
    feed BOM LEAF nodes (see their own collectAllItems()/leaf-filtering), so
    a BOM ROOT/top-level assembled product never gets sent through either of
    those flows at all. Cross-references the already-cached bom_node_cache
    (no live SAP calls - same "never trigger fresh SAP lookups from this
    page" rule as the rest of inventory_service.py) to find which of these
    product_ids has its OWN BOM (found=True), then splits those into:
    - Consumed as a CHILD inside some OTHER product's BOM tree -> it's an
      intermediate 'Sub-Assembly', not the final saleable item, even though
      it also happens to have its own BOM.
    - Never appears as a child anywhere else -> a genuine top-level
      'Finished Goods' item.
    Both are deterministic, more reliable signals than asking the AI to
    guess "is this an assembly, and is it the TOP of the tree?" from a bare
    description alone - overwriting any previous AI/rule guess (never a
    manual one) so a previously-mis-categorized item gets corrected too.
    Everything else still goes through the normal AI categorizer. Returns
    {product_id: category} for every item that has (or gets) one."""
    add_category(db, "Finished Goods")
    add_category(db, "Sub-Assembly")
    collection = db["component_master"]
    ids = [item["product_id"] for item in items if item.get("product_id")]
    if not ids:
        return {}

    has_own_bom_ids = {
        doc["_id"] for doc in db["bom_node_cache"].find({"_id": {"$in": ids}, "found": True}, {"_id": 1})
    }

    # Also re-check any item ALREADY tagged 'Finished Goods' elsewhere in
    # component_master (even one that has since dropped out of the current
    # Inventory snapshot, e.g. its on-hand qty briefly hit zero between
    # runs) - a stale mis-tag from before this rule existed must not be
    # left uncorrected just because it's temporarily outside today's
    # inventory list.
    existing_fg_ids = {
        doc["_id"] for doc in collection.find(
            {"category": "Finished Goods", "category_source": {"$ne": "manual"}}, {"_id": 1}
        )
    }
    check_ids = has_own_bom_ids | existing_fg_ids

    used_as_component_ids = set()
    if check_ids:
        cursor = db["bom_node_cache"].find(
            {"groups.items.product_id": {"$in": list(check_ids)}},
            {"groups.items.product_id": 1},
        )
        for doc in cursor:
            for group in doc.get("groups", []):
                for child in group.get("items", []):
                    child_id = child.get("product_id")
                    if child_id in check_ids:
                        used_as_component_ids.add(child_id)

    finished_goods_ids = has_own_bom_ids - used_as_component_ids
    sub_assembly_ids = (has_own_bom_ids | existing_fg_ids) & used_as_component_ids

    manual_ids = {
        doc["_id"] for doc in collection.find(
            {"_id": {"$in": list(check_ids)}, "category_source": "manual"}, {"_id": 1}
        )
    }

    now = datetime.now(timezone.utc)
    results = {}
    for product_id, category in [(pid, "Finished Goods") for pid in finished_goods_ids] + \
                                  [(pid, "Sub-Assembly") for pid in sub_assembly_ids]:
        if product_id in manual_ids:
            continue
        collection.update_one(
            {"_id": product_id},
            {"$set": {"category": category, "category_source": "rule", "categorized_at": now}},
            upsert=True,
        )
        results[product_id] = category

    remaining_items = [item for item in items if item.get("product_id") not in results]
    ai_results = await categorize_items(remaining_items, db)
    results.update(ai_results)
    return results
