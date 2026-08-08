"""Quota Arrangement: the formal, AI-assisted, revision-tracked replacement
for the old simple "App-Managed Assignments" table. For a given Product ID,
gathers every supplier we have ANY price/purchase signal for (local
assignments, ERP billing history, SAP Price Specs, SAP Supplier Invoices),
asks an LLM to suggest a fair quota % split (weighted by price + lead
time; quality/OTIF fields are wired in as `None` placeholders today - the
prompt will automatically start using them the moment those data feeds are
populated, no code change needed), and lets the buyer review/edit before
confirming. Every confirm is appended as a new, immutable revision under a
single per-item "arrangement" (`_id` = e.g. "SCR415WM-1" - itemnum +
per-item counter), giving a full audit trail of who changed what and why.

On confirm, also upserts `part_suppliers` (the pre-existing collection) so
the rest of the app - notably the Purchasing Plan supplier badges - keeps
working unchanged; this collection is now the "current state" projection
of the latest confirmed revision, while quota_arrangements/
quota_arrangement_revisions is the source of truth + history.
"""
import json
import uuid
from datetime import datetime, timezone

from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone

from sap_price_spec_client import SAPPriceSpecError
from price_explorer_client import PriceExplorerError
from sap_supplier_invoice_client import SAPSupplierInvoiceError
import supplier_service

ARRANGEMENTS_COLLECTION = "quota_arrangements"
REVISIONS_COLLECTION = "quota_arrangement_revisions"

AI_MODEL_PROVIDER = "openai"
AI_MODEL_NAME = "gpt-5-mini"  # cost-effective tier, per user's explicit choice


def _now():
    return datetime.now(timezone.utc)


def gather_candidate_suppliers(db, product_id: str, price_explorer_client, sap_price_spec_client,
                                sap_supplier_invoice_client) -> list:
    """Unions every supplier we have ANY signal for on this part, merged by
    SAP internal ID (falls back to name if no internal ID is known).
    Returns [{supplier_id (local, may be None if never synced), supplier_name,
    sap_internal_id, price, currency, price_source, lead_time_days}]."""
    by_key = {}

    def upsert(key, name, price=None, currency=None, source=None):
        if key not in by_key:
            by_key[key] = {
                "supplier_id": None, "supplier_name": name, "sap_internal_id": None,
                "price": None, "currency": None, "price_source": None, "lead_time_days": None,
            }
        row = by_key[key]
        if name and not row["supplier_name"]:
            row["supplier_name"] = name
        if price is not None and row["price"] is None:
            row["price"] = price
            row["currency"] = currency
            row["price_source"] = source

    # 1) Existing local assignments (carries forward any lead time already on file)
    for a in supplier_service.list_suppliers_for_part(db, product_id):
        sup = supplier_service.get_supplier(db, a["supplier_id"])
        key = (sup or {}).get("sap_internal_id") or a["supplier_id"]
        upsert(key, (sup or {}).get("name"), a.get("unit_price"), a.get("currency"), "Local assignment")
        by_key[key]["supplier_id"] = a["supplier_id"]
        by_key[key]["sap_internal_id"] = (sup or {}).get("sap_internal_id")
        if a.get("lead_time_days") is not None:
            by_key[key]["lead_time_days"] = a["lead_time_days"]

    # 2) Real ERP billed purchase history
    try:
        for item in price_explorer_client.search(product_id, lookback_days=365, limit=3):
            for point in (item.get("last"), item.get("lowest")):
                if point and point.get("pcode") and point.get("rate") is not None:
                    upsert(point["pcode"].strip(), point.get("supplier"), point["rate"], "INR", "ERP billing history")
                    by_key[point["pcode"].strip()]["sap_internal_id"] = point["pcode"].strip()
    except PriceExplorerError:
        pass

    # 3) SAP native Supplier Invoices (real posted purchases)
    try:
        for row in sap_supplier_invoice_client.get_invoices_for_product(product_id, limit=10):
            if row.get("supplier_internal_id") and row.get("price") is not None:
                upsert(row["supplier_internal_id"], row.get("supplier_name"), row["price"], row.get("currency"), "SAP Supplier Invoice")
                by_key[row["supplier_internal_id"]]["sap_internal_id"] = row["supplier_internal_id"]
    except SAPSupplierInvoiceError:
        pass

    # 4) SAP Price Specifications (Released ones are the most trustworthy price)
    try:
        for spec in sap_price_spec_client.get_price_specs_for_product(product_id):
            if spec.get("release_status_code") == "3" and spec.get("supplier_internal_id") and spec.get("price"):
                key = spec["supplier_internal_id"]
                by_key.setdefault(key, {
                    "supplier_id": None, "supplier_name": spec.get("supplier_name"), "sap_internal_id": key,
                    "price": None, "currency": None, "price_source": None, "lead_time_days": None,
                })
                by_key[key]["price"] = spec["price"]
                by_key[key]["currency"] = spec.get("currency")
                by_key[key]["price_source"] = "SAP Released Price Spec"
                by_key[key]["sap_internal_id"] = key
    except SAPPriceSpecError:
        pass

    # Resolve local supplier_id for any candidate found only via SAP/ERP data
    for row in by_key.values():
        if row["supplier_id"] is None and row["sap_internal_id"]:
            local = db[supplier_service.SUPPLIERS_COLLECTION].find_one({"sap_internal_id": row["sap_internal_id"]})
            if local:
                row["supplier_id"] = local["_id"]
                if not row["supplier_name"]:
                    row["supplier_name"] = local["name"]

    return list(by_key.values())


def suggest_allocation(product_id: str, candidates: list, emergent_llm_key: str) -> dict:
    """Returns {allocations: [{supplier_id, supplier_name, sap_internal_id,
    price, currency, lead_time_days, quota_percent, rationale}],
    overall_rationale}. Skips the LLM entirely for the trivial 0/1-candidate
    cases (nothing to weigh)."""
    if not candidates:
        return {"allocations": [], "overall_rationale": "No known suppliers found for this part yet - add one manually below."}
    if len(candidates) == 1:
        c = candidates[0]
        return {
            "allocations": [{**c, "quota_percent": 100.0, "rationale": "Only known supplier on file for this part."}],
            "overall_rationale": "Only one supplier is known for this part, so it gets 100% by default.",
        }

    prompt_candidates = [
        {
            "supplier_id": c["sap_internal_id"] or c["supplier_name"],
            "supplier_name": c["supplier_name"],
            "price": c["price"],
            "currency": c["currency"],
            "lead_time_days": c["lead_time_days"],
            "quality_score": None,
            "otif_percent": None,
        }
        for c in candidates
    ]
    system_message = (
        "You are a procurement analyst. Given candidate suppliers for one purchased part, suggest a fair quota "
        "allocation percentage split across them that sums to EXACTLY 100. Favor lower price and shorter lead "
        "time. If lead_time_days is null for a supplier, treat that factor as neutral (average) rather than "
        "penalizing it, but still weigh price. quality_score and otif_percent are reserved for future quality-"
        "failure and on-time-in-full delivery performance data that does not exist in the system yet - whenever "
        "you see them as null, you MUST explicitly say in that supplier's rationale that quality/delivery "
        "performance data is not yet available, so the buyer knows the suggestion is price/lead-time only. "
        "Respond with STRICT JSON only, no markdown, no prose outside the JSON, in exactly this schema: "
        '{"allocations": [{"supplier_id": "...", "quota_percent": 0, "rationale": "..."}], "overall_rationale": "..."}'
    )
    user_text = json.dumps({"product_id": product_id, "candidates": prompt_candidates})

    chat = LlmChat(
        api_key=emergent_llm_key,
        session_id=f"quota-suggest-{product_id}-{uuid.uuid4().hex[:8]}",
        system_message=system_message,
    ).with_model(AI_MODEL_PROVIDER, AI_MODEL_NAME)

    async def _run():
        buffer = []
        async for event in chat.stream_message(UserMessage(text=user_text)):
            if isinstance(event, TextDelta):
                buffer.append(event.content)
            elif isinstance(event, StreamDone):
                break
        return "".join(buffer)

    import asyncio
    raw = asyncio.run(_run())

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    parsed = json.loads(raw)

    by_supplier_id = {(c["sap_internal_id"] or c["supplier_name"]): c for c in candidates}
    allocations = []
    for a in parsed.get("allocations", []):
        base = by_supplier_id.get(a.get("supplier_id"))
        if not base:
            continue
        allocations.append({**base, "quota_percent": round(float(a.get("quota_percent", 0)), 1), "rationale": a.get("rationale")})
    return {"allocations": allocations, "overall_rationale": parsed.get("overall_rationale")}


def get_arrangement(db, product_id: str) -> dict:
    """Returns {arrangement, latest_revision, revisions} or all-None fields
    if no arrangement has ever been confirmed for this part yet."""
    arrangement = db[ARRANGEMENTS_COLLECTION].find_one({"product_id": product_id})
    if not arrangement:
        return {"arrangement": None, "latest_revision": None, "revisions": []}
    revisions = list(db[REVISIONS_COLLECTION].find({"arrangement_id": arrangement["_id"]}).sort("revision_no", -1))
    return {"arrangement": arrangement, "latest_revision": revisions[0] if revisions else None, "revisions": revisions}


def confirm_arrangement(db, product_id: str, allocations: list, remarks: str, source: str, created_by: str) -> dict:
    """Creates the arrangement (quota_seq=1) on first confirm for this
    item, or appends a new revision to the existing one. Also upserts
    part_suppliers so Purchasing Plan badges reflect this immediately."""
    arrangement = db[ARRANGEMENTS_COLLECTION].find_one({"product_id": product_id})
    if not arrangement:
        arrangement = {"_id": f"{product_id}-1", "product_id": product_id, "quota_seq": 1, "current_revision_no": 0, "created_at": _now()}
        db[ARRANGEMENTS_COLLECTION].insert_one(arrangement)

    next_revision_no = arrangement["current_revision_no"] + 1
    revision = {
        "_id": str(uuid.uuid4()),
        "arrangement_id": arrangement["_id"],
        "product_id": product_id,
        "revision_no": next_revision_no,
        "source": source,
        "remarks": remarks,
        "created_by": created_by,
        "created_at": _now(),
        "allocations": allocations,
    }
    db[REVISIONS_COLLECTION].insert_one(revision)
    db[ARRANGEMENTS_COLLECTION].update_one({"_id": arrangement["_id"]}, {"$set": {"current_revision_no": next_revision_no, "updated_at": _now()}})

    # Project the latest revision into part_suppliers (replace this product's rows entirely)
    db[supplier_service.PART_SUPPLIERS_COLLECTION].delete_many({"product_id": product_id})
    for a in allocations:
        supplier_id = a.get("supplier_id")
        if not supplier_id:
            # First time we've ever seen this supplier - create a proper local record for it.
            existing = db[supplier_service.SUPPLIERS_COLLECTION].find_one({"sap_internal_id": a.get("sap_internal_id")}) if a.get("sap_internal_id") else None
            if existing:
                supplier_id = existing["_id"]
            else:
                created = supplier_service.create_supplier(db, a.get("supplier_name") or "Unknown Supplier", sap_internal_id=a.get("sap_internal_id"))
                supplier_id = created["_id"]
        supplier_service.assign_supplier_to_part(
            db, product_id, supplier_id,
            quota_percent=a.get("quota_percent"), lead_time_days=a.get("lead_time_days"),
            unit_price=a.get("price"), currency=a.get("currency"),
            preference="Preferred" if (a.get("quota_percent") or 0) >= 50 else "Backup",
            notes=remarks,
        )

    return get_arrangement(db, product_id)
