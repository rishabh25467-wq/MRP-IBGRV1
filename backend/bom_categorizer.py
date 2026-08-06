"""AI-based BOM component categorization using GPT-5.4 Mini.

Classifies each BOM line item (by product ID + description) into a broad
material/type category (Raw Material, Hardware, Plastic, Packaging, Zinc,
Copper Alloy, etc.) to help quickly scan large BOMs by material type.
"""
import json
import logging
import os
import re

from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger(__name__)

BATCH_SIZE = 80

SUGGESTED_CATEGORIES = [
    "Raw Material", "Hardware", "Fastener", "Plastic", "Sheet Metal",
    "Zinc", "Copper Alloy", "Aluminum", "Steel", "Stainless Steel",
    "Rubber/Elastomer", "Electronics", "Packaging", "Stationery",
    "Adhesive", "Label/Printing", "Sub-Assembly", "Other",
]

SYSTEM_MESSAGE = (
    "You are an expert manufacturing engineer who classifies Bill of Materials (BOM) "
    "line items into a single, concise material/type category based on their Product ID "
    "and Description. Prefer these common categories when they fit: "
    f"{', '.join(SUGGESTED_CATEGORIES)}. If none fit well, use your own short (1-3 word) "
    "category, but reuse the SAME label for similar items so results stay consistent. "
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


async def categorize_items(items: list[dict]) -> dict:
    """items: [{"product_id": str, "description": str | None}, ...]
    Returns {product_id: category}."""
    api_key = os.environ["EMERGENT_LLM_KEY"]
    unique_items = list({item["product_id"]: item for item in items if item.get("product_id")}.values())

    results = {}
    for i in range(0, len(unique_items), BATCH_SIZE):
        batch = unique_items[i:i + BATCH_SIZE]
        prompt_lines = [f"{item['product_id']} :: {item.get('description') or 'No description'}" for item in batch]
        user_text = (
            "Classify each of these BOM items (format 'product_id :: description'):\n\n"
            + "\n".join(prompt_lines)
        )

        chat = LlmChat(
            api_key=api_key,
            session_id=f"bom-categorize-{i}",
            system_message=SYSTEM_MESSAGE,
        ).with_model("openai", "gpt-5.4-mini")

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
