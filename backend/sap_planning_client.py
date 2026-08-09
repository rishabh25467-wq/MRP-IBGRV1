"""SAP Business ByDesign Material Supply Planning (Safety Stock / Procurement
Lead Time) client - the read/WRITE counterpart to sap_valuation_client.py.

Backed by a custom OData service ("materialltmsl") exposing the Root node of
the "SupplyPlanningProcessInformation" element on the Material business
object (entity type MaterialSupplyPlanningProcessInformation). Each Material
has ONE row per Supply Planning Area (site) - e.g. P1, P2, P3... - so a
single Material ID maps to MULTIPLE rows, all keyed by their own ObjectID.

Write mechanics (verified live against the tenant): standard HTTP PATCH,
after first fetching a CSRF token via `x-csrf-token: fetch` on any GET to the
same service, reusing the resulting session cookie on the PATCH. SAP briefly
object-locks the parent Material during each write - firing several PATCHes
back-to-back at different rows of the SAME material can occasionally collide
with that lock's release window ("Locking object not possible; object locked
by ..."), so each row write gets a short retry-with-backoff.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

BATCH_SIZE = 15
COLLECTION = "MaterialSupplyPlanningProcessInformationCollection"
WRITE_RETRY_ATTEMPTS = 4
WRITE_RETRY_DELAY_SECONDS = 1.5


def _duration_to_days(value) -> float | None:
    """SAP serializes Edm.Duration-like fields as ISO-8601 strings, e.g.
    'P4D' (4 days). Returns None for empty/unparseable values."""
    if not value:
        return None
    match = re.fullmatch(r"P(\d+(?:\.\d+)?)D", value.strip())
    return float(match.group(1)) if match else None


def _days_to_duration(days) -> str:
    return f"P{int(days)}D"


class SAPPlanningError(Exception):
    pass


class SAPPlanningClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    def _get(self, filter_expr: str):
        with sap_semaphore:
            resp = requests.get(
                f"{self.base_url}/{COLLECTION}",
                auth=HTTPBasicAuth(self.username, self.password),
                timeout=30,
                headers={"Accept": "application/json"},
                params={"$filter": filter_expr, "$format": "json"},
            )
        if resp.status_code != 200:
            raise SAPPlanningError(f"SAP planning service returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPPlanningError(data["error"].get("message", {}).get("value", "Unknown OData error"))
        return data.get("d", {}).get("results", [])

    @staticmethod
    def _chunks(items, size):
        items = list(items)
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def get_planning_data(self, material_uuids: list[str]) -> dict:
        """material_uuid -> {"safety_stock": float, "lead_time_days": float|None,
        "unit_code": str|None, "planning_area_count": int, "rows": [row dict, ...]}
        or None if the material has no planning rows at all. "safety_stock"/
        "lead_time_days" are the representative values (the "P1" row if
        present, else the first row) - purely for display/comparison; `rows`
        carries every planning-area row's ObjectID for push_planning_data."""
        material_uuids = list({uuid.upper() for uuid in material_uuids if uuid})
        if not material_uuids:
            return {}

        rows_by_material = {uuid: [] for uuid in material_uuids}

        def fetch_chunk(chunk):
            filter_expr = " or ".join(f"MaterialUUID eq guid'{uuid}'" for uuid in chunk)
            return self._get(filter_expr)

        chunks = list(self._chunks(material_uuids, BATCH_SIZE))
        with ThreadPoolExecutor(max_workers=6) as executor:
            results_per_chunk = list(executor.map(fetch_chunk, chunks))

        for results in results_per_chunk:
            for row in results:
                material_uuid = (row.get("MaterialUUID") or "").upper()
                if material_uuid in rows_by_material:
                    rows_by_material[material_uuid].append(row)

        planning_data = {}
        for material_uuid, rows in rows_by_material.items():
            if not rows:
                planning_data[material_uuid] = None
                continue
            representative = next((r for r in rows if r.get("SupplyPlanningAreaID") == "P1"), rows[0])
            planning_data[material_uuid] = {
                "safety_stock": float(representative.get("SafetyStockQuantity") or 0),
                "lead_time_days": _duration_to_days(representative.get("PlannedDeliveryDuration")),
                "unit_code": representative.get("unitCode"),
                "planning_area_count": len(rows),
                "rows": rows,
            }
        return planning_data

    def push_planning_data(self, material_uuid: str, rows: list[dict], safety_stock: float = None,
                            lead_time_days: float = None) -> int:
        """Writes safety_stock/lead_time_days to EVERY planning-area row
        passed in `rows` (as returned by get_planning_data) for this
        material - the tenant has no single "canonical" planning area, so we
        keep every site in sync. Returns the number of rows updated."""
        if safety_stock is None and lead_time_days is None:
            raise SAPPlanningError("Nothing to push - provide safety_stock and/or lead_time_days")

        payload = {}
        if safety_stock is not None:
            payload["SafetyStockQuantity"] = str(safety_stock)
        if lead_time_days is not None:
            payload["PlannedDeliveryDuration"] = _days_to_duration(lead_time_days)

        session = requests.Session()
        session.auth = HTTPBasicAuth(self.username, self.password)

        with sap_semaphore:
            csrf_resp = session.get(
                f"{self.base_url}/{COLLECTION}",
                headers={"x-csrf-token": "fetch", "Accept": "application/json"},
                params={"$top": 1},
                timeout=30,
            )
        token = csrf_resp.headers.get("x-csrf-token")
        if not token:
            raise SAPPlanningError("SAP did not return a CSRF token - cannot write")

        updated = 0
        for row in rows:
            object_id = row.get("ObjectID")
            if not object_id:
                continue
            last_error = None
            for attempt in range(1, WRITE_RETRY_ATTEMPTS + 1):
                with sap_semaphore:
                    resp = session.patch(
                        f"{self.base_url}/{COLLECTION}('{object_id}')",
                        headers={"x-csrf-token": token, "Content-Type": "application/json", "Accept": "application/json"},
                        json=payload,
                        timeout=30,
                    )
                if resp.status_code in (200, 204):
                    updated += 1
                    last_error = None
                    break
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                is_lock_error = "locking object not possible" in resp.text.lower()
                if is_lock_error and attempt < WRITE_RETRY_ATTEMPTS:
                    logger.warning(
                        f"SAP object lock on planning area {row.get('SupplyPlanningAreaID')} "
                        f"(attempt {attempt}/{WRITE_RETRY_ATTEMPTS}), retrying: {last_error}"
                    )
                    time.sleep(WRITE_RETRY_DELAY_SECONDS)
                    continue
                break
            if last_error:
                raise SAPPlanningError(
                    f"SAP write failed for planning area {row.get('SupplyPlanningAreaID')}: {last_error}"
                )
        return updated


def bulk_push_to_sap(db, planning_client, progress_callback=None) -> dict:
    """Pushes MSL->Safety Stock and Lead Time (Days)->Procurement Lead Time
    for EVERY component that has a captured SAP link (product_uuid) and at
    least one of msl/lead_time_days set - powers the Admin page's "Push All
    to SAP" button (the bulk counterpart to the per-row Push to SAP).
    Uses bounded concurrency ACROSS DIFFERENT materials (safe - SAP's
    object-lock only contends on writes to the SAME material's rows, which
    push_planning_data already retries) to keep a large run from taking
    forever. progress_callback(processed, total) is invoked after each
    material finishes, for job-status polling. Returns
    {total, pushed, failed: [{product_id, error}]}."""
    docs = list(db["component_master"].find({
        "product_uuid": {"$exists": True, "$ne": None},
        "$or": [{"msl": {"$ne": None}}, {"lead_time_days": {"$ne": None}}],
    }))
    total = len(docs)
    pushed = 0
    failed = []
    processed = 0

    def push_one(doc):
        try:
            planning = planning_client.get_planning_data([doc["product_uuid"]])
            data = planning.get(doc["product_uuid"].upper())
            if not data:
                return doc["_id"], False, "SAP has no Supply Planning record for this material"
            planning_client.push_planning_data(doc["product_uuid"], data["rows"], doc.get("msl"), doc.get("lead_time_days"))
            db["component_master"].update_one({"_id": doc["_id"]}, {"$set": {"sap_pushed_at": datetime.now(timezone.utc)}})
            return doc["_id"], True, None
        except SAPPlanningError as e:
            return doc["_id"], False, str(e)
        except Exception as e:
            return doc["_id"], False, f"Unexpected error: {e}"

    with ThreadPoolExecutor(max_workers=5) as executor:
        for product_id, success, error in executor.map(push_one, docs):
            processed += 1
            if success:
                pushed += 1
            else:
                failed.append({"product_id": product_id, "error": error})
            if progress_callback:
                progress_callback(processed, total)

    return {"total": total, "pushed": pushed, "failed": failed}
