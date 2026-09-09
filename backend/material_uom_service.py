"""Material Purchasing UOM cache (Sep 9 2026, user's explicit ask: "fetch
secondary unit of the item while creating purchase order" - e.g. material
6550-002047 has "1 Packet (XPA) = 100 EA" maintained in SAP's own Material
master "Quantity Conversions" grid, confirmed live via
sap_material_client.get_uom_info()).

On-demand only, per user's explicit choice - the buyer clicks a small icon
next to the UOM field for a specific line item; we hit SAP live once per
product_id and cache the result here (this master data essentially never
changes, unlike inventory/pricing), so re-picking the same item later
(same PO or a future one) is instant.
"""
from datetime import datetime, timezone

COLLECTION_NAME = "material_uom_cache"

# UN/CEFACT unit codes actually seen on this SAP tenant (see
# PR_UOM_TO_SAP_CODE in server.py) - only used for a friendlier dropdown
# label, falls back to the raw code for anything not listed here.
UOM_CODE_LABELS = {
    "EA": "Each", "KGM": "Kilogram", "MTR": "Meter", "SET": "Set",
    "XPA": "Packet", "XBX": "Box", "XRO": "Roll", "FTK": "Foot",
    "XCT": "Carton", "LTR": "Litre", "MTK": "Square Meter", "TNE": "Metric Ton",
    "PR": "Pair", "XPK": "Pack",
}


def uom_label(code: str) -> str:
    return UOM_CODE_LABELS.get(code, code)


def get_uom_options(product_id: str, sap_material_client, db) -> dict:
    """Returns {"product_id", "base_unit", "options": [{"unit_code",
    "label", "is_base", "conversion_note"}, ...]} - always includes the
    base unit as one option, plus one option per alternate unit SAP has
    configured for this material (usually none - most items are ordered
    in their base unit only)."""
    collection = db[COLLECTION_NAME]
    doc = collection.find_one({"_id": product_id})
    if doc is None:
        info = sap_material_client.get_uom_info(product_id)
        doc = {
            "_id": product_id,
            "found": info is not None,
            "base_unit": info["base_unit"] if info else None,
            "alternate_units": info["alternate_units"] if info else [],
            "checked_at": datetime.now(timezone.utc),
        }
        collection.replace_one({"_id": product_id}, doc, upsert=True)

    base_unit = doc.get("base_unit") or "EA"
    options = [{
        "unit_code": base_unit, "label": f"{base_unit} - {uom_label(base_unit)} (base unit)",
        "is_base": True, "conversion_note": None,
    }]
    for alt in doc.get("alternate_units", []):
        if alt["unit_code"] == base_unit:
            continue
        per_unit_base_qty = alt["base_qty"] / alt["unit_qty"] if alt["unit_qty"] else alt["base_qty"]
        note = f"1 {uom_label(alt['unit_code'])} = {per_unit_base_qty:g} {base_unit}"
        options.append({
            "unit_code": alt["unit_code"], "label": f"{alt['unit_code']} - {uom_label(alt['unit_code'])} ({note})",
            "is_base": False, "conversion_note": note,
        })
    return {"product_id": product_id, "base_unit": base_unit, "options": options}
