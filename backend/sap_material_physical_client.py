"""SAP Business ByDesign custom OData client for Net Weight / Surface Area.

Backed by a custom OData service ("materialgeneralinfo", Business Object
"Material", Business Context "Material - General Information") exposing
the Material root node's ObjectID/InternalID/UUID plus the SAP admin's 2
custom fields ("Item Net Weight" / ItemNetWeight1, "Surface
Area(Sq.Inch)" / SurfaceAreaSqInch) as their flattened Key User Tool OData
properties: `<field>content_KUT` (Edm.Decimal, the numeric value) and
`<field>unitCode_KUT` (Edm.String, the unit e.g. KGM/INK). Both fields
initially hit a persistent SOAP ManageMaterialIn write restriction (live-
confirmed 18 Aug 2026 across 3 Communication Scenario/Arrangement
recreations) - exposing them directly via this custom OData service
instead (once the SAP admin ticked their content/unitCode sub-properties
in the OData Service Builder and reactivated) resolved it: live-confirmed
working read+write round trip 18 Aug 2026 on Material 5989825-2.1, same
CSRF+session approach as sap_planning_client.py's already-proven
materialltmsl write-back."""
import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore


def _extract_sap_error_message(resp) -> str:
    """Best-effort pull of the human-readable SAP OData error text
    (error.message.value) out of an error response body, falling back to
    the raw status/text if the body isn't the expected JSON shape."""
    try:
        message = resp.json().get("error", {}).get("message", {}).get("value")
        if message:
            return message
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"

COLLECTION = "MaterialCollection"

# App-facing field name -> (SAP OData content property, SAP OData unitCode
# property, default unit code this app writes/expects).
PHYSICAL_FIELD_CONFIG = {
    "net_weight_kg": ("ItemNetWeight1content_KUT", "ItemNetWeight1unitCode_KUT", "KGM"),
    "surface_area_sqin": ("SurfaceAreaSqInchcontent_KUT", "SurfaceAreaSqInchunitCode_KUT", "INK"),
}
PHYSICAL_FIELD_TO_SAP_PROPERTY = {field: content for field, (content, _, _) in PHYSICAL_FIELD_CONFIG.items()}


class SAPMaterialPhysicalError(Exception):
    pass


class SAPMaterialPhysicalClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    def get_physical_attributes(self, internal_id: str):
        """Returns {"object_id": str, "attributes": {field_name: float},
        "base_uom": str|None} (attributes only includes fields with a
        non-zero/non-empty value), or None if SAP has no material with
        that InternalID."""
        with sap_semaphore:
            resp = requests.get(
                f"{self.base_url}/{COLLECTION}",
                auth=HTTPBasicAuth(self.username, self.password),
                headers={"Accept": "application/json"},
                params={"$filter": f"InternalID eq '{internal_id}'", "$format": "json"},
                timeout=30,
            )
        if resp.status_code >= 400:
            raise SAPMaterialPhysicalError(_extract_sap_error_message(resp))
        results = resp.json().get("d", {}).get("results", [])
        if not results:
            return None
        row = results[0]
        attributes = {}
        for field, (content_property, _, _) in PHYSICAL_FIELD_CONFIG.items():
            raw_value = row.get(content_property)
            if raw_value not in (None, ""):
                try:
                    value = float(raw_value)
                except ValueError:
                    continue
                if value != 0:
                    attributes[field] = value
        # Standard field (not a custom Key User Tool field like the ones
        # above) - exposed Aug 2026 on materialgeneralinfo per user's SAP
        # admin, same OData row, no extra round trip. Used to auto-lock
        # the Create Production Order form's UoM picker against the
        # material's real base unit instead of leaving it a free choice.
        base_uom = row.get("BaseMeasureUnitCode") or None
        return {"object_id": row.get("ObjectID"), "attributes": attributes, "base_uom": base_uom}

    def push_physical_attributes(self, internal_id: str, values: dict):
        """Writes Net Weight/Surface Area (values keyed by
        PHYSICAL_FIELD_CONFIG's field names) to SAP via a PATCH on the
        Material entity - re-reads to get the current ObjectID first
        since it's the OData entity key. Also sets each field's unitCode
        property to its default unit (KGM/INK) alongside the value.
        Returns the number of fields written."""
        current = self.get_physical_attributes(internal_id)
        if not current:
            raise SAPMaterialPhysicalError(f"SAP has no material with InternalID '{internal_id}'")
        object_id = current["object_id"]

        payload = {}
        for field, value in values.items():
            if value is not None and field in PHYSICAL_FIELD_CONFIG:
                content_property, unit_property, default_unit = PHYSICAL_FIELD_CONFIG[field]
                payload[content_property] = str(value)
                payload[unit_property] = default_unit
        if not payload:
            raise SAPMaterialPhysicalError("Nothing to push - set Net Weight and/or Surface Area first")

        session = requests.Session()
        session.auth = HTTPBasicAuth(self.username, self.password)
        with sap_semaphore:
            csrf_resp = session.get(
                f"{self.base_url}/{COLLECTION}",
                headers={"x-csrf-token": "fetch", "Accept": "application/json"},
                params={"$top": 1},
                timeout=30,
            )
            resp = session.patch(
                f"{self.base_url}/{COLLECTION}('{object_id}')",
                headers={
                    "x-csrf-token": csrf_resp.headers.get("x-csrf-token", ""),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=30,
            )
        if resp.status_code not in (200, 204):
            raise SAPMaterialPhysicalError(f"SAP write failed: {_extract_sap_error_message(resp)}")
        return len(values)


def bulk_push_physical_to_sap(db, physical_client, progress_callback=None) -> dict:
    """Bulk counterpart to the per-row 'Push to SAP' in the Weight &
    Surface Area dialog - user's explicit ask (Aug 2026): after importing
    Net Wt./Surface Area for ~600 components from an Excel report, pushing
    them to SAP one row at a time was impractical. Mirrors
    sap_planning_client.bulk_push_to_sap's exact structure (bounded
    ThreadPoolExecutor concurrency - safe here too, each push is one
    material's own PATCH, no cross-material contention). Only pushes
    net_weight_kg/surface_area_sqin - gross_weight_kg has no SAP field
    (Aug 2026 decision: local-only for now). Returns
    {total, pushed, failed: [{product_id, error}]}."""
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor

    docs = list(db["component_master"].find({
        "$or": [{"net_weight_kg": {"$ne": None}}, {"surface_area_sqin": {"$ne": None}}],
    }))
    total = len(docs)
    pushed = 0
    failed = []
    processed = 0

    def push_one(doc):
        values = {field: doc.get(field) for field in PHYSICAL_FIELD_TO_SAP_PROPERTY}
        if not any(v is not None for v in values.values()):
            return doc["_id"], False, "No Net Weight/Surface Area value set"
        try:
            physical_client.push_physical_attributes(doc["_id"], values)
            db["component_master"].update_one({"_id": doc["_id"]}, {"$set": {"sap_physical_pushed_at": datetime.now(timezone.utc)}})
            return doc["_id"], True, None
        except SAPMaterialPhysicalError as e:
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

