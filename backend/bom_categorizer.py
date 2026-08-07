"""AI-based BOM component categorization using GPT-5.4, persisted forever.

Classifies each BOM leaf component (by product ID + description) into a
broad material/type category (Raw Material, Hardware, Plastic, Packaging,
Zinc, Copper Alloy, etc.). Every result is written to the `component_master`
Mongo collection and read back on every subsequent call instead of asking
the AI again - this is what makes categories CONSISTENT across BOM loads and
Purchasing Plan runs (previously, every run re-asked the AI fresh, so the
same borderline item like an RFID tag could land in a different category
each time depending on non-determinism and which other items it was batched
with). A human correction saved via the Admin > Component Master page
(category_source="manual") is never overwritten by this module - only the
Admin page's own "Re-Categorise" action can force a fresh AI pass on an item.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger(__name__)

BATCH_SIZE = 80
MODEL = "gpt-5.4"

SUGGESTED_CATEGORIES = [
    "Raw Material", "Hardware", "Plastic", "Sheet Metal",
    "Zinc", "Copper Alloy", "Aluminum", "Steel", "Stainless Steel",
    "Rubber/Elastomer", "Electronics", "Packaging", "Stationery",
    "Adhesive", "Label/Printing", "Sub-Assembly", "Other",
]

SYSTEM_MESSAGE = (
    "You are an expert manufacturing engineer who classifies Bill of Materials (BOM) "
    "line items into a single, concise material/type category based on their Product ID "
    "and Description. You MUST choose from EXACTLY this list of categories - do not invent "
    f"new ones, do not rename them, reuse the exact spelling: {', '.join(SUGGESTED_CATEGORIES)}. "
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
    "-> 'Label/Printing'.\n\n"
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


async def _ai_categorize(items: list[dict]) -> dict:
    """items: [{"product_id": str, "description": str | None}, ...] (already
    deduplicated by caller). Returns {product_id: category}. Raises
    BomCategorizerError on any AI/parsing failure."""
    api_key = os.environ["EMERGENT_LLM_KEY"]
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
            system_message=SYSTEM_MESSAGE,
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

    if not to_categorize:
        return results

    ai_results = await _ai_categorize(to_categorize)
    now = datetime.now(timezone.utc)
    for product_id, category in ai_results.items():
        collection.update_one(
            {"_id": product_id},
            {"$set": {"category": category, "category_source": "ai", "categorized_at": now}},
        )
    results.update(ai_results)
    return results
