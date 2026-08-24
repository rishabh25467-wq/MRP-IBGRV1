"""SAP Business ByDesign Material Valuation Data (Moving Average price) client.

Reads live standard-cost data via a custom OData service exposed on the
"MaterialValuationData" business object. The linkage chain is:

  Product UUID (from the BOM SOAP data)
    -> MaterialValuationDataValuationLevelCollection filtered by MaterialUUID
       -> each result's ObjectID (converted to dashed UUID format) identifies
          that material's ValuationLevel record (one per company/site)
    -> MaterialValuationDataValuationPriceCollection filtered by
       ValuationLevelUUID (= the ValuationLevel's own ObjectID)
       -> each result is a historical price with a StartDate/EndDate validity
          window; the currently valid one (today falls within the window) is
          the live standard cost.

Queries are batched (OR'd together in a single $filter) in chunks to avoid
one HTTP round-trip per product, since a BOM can have 100+ components.

Each price row also carries a PriceTypeCode: "1" = Inventory Cost (this
tenant's Moving Average, recalculated every period) vs "2" = Estimated Cost
(Standard Cost). This client always resolves to the Moving Average price
(see MOVING_AVERAGE_PRICE_TYPE_CODE on SAPValuationClient) - Standard Cost
rows are never considered, per explicit user decision (Aug 2026).
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

BATCH_SIZE = 15
# SAP's "open-ended" validity sentinel (9999-12-31) - anything at/after this
# should be treated as "still valid, no known end date".
OPEN_ENDED_EPOCH_MS = 253402214400000


def _object_id_to_uuid(object_id: str) -> str:
    """SAP's 32-char hex ObjectID is the same value as the record's own UUID,
    just missing the dashes ('6F51C7A2C1DB1EDE90CE3ADAD23AEA34' ->
    '6F51C7A2-C1DB-1EDE-90CE-3ADAD23AEA34')."""
    return f"{object_id[0:8]}-{object_id[8:12]}-{object_id[12:16]}-{object_id[16:20]}-{object_id[20:32]}"


def _parse_odata_date(value):
    """OData v2 JSON dates are serialized as '/Date(epoch_ms)/'."""
    if not value or not value.startswith("/Date("):
        return None
    epoch_ms = int(value[len("/Date("):-len(")/")])
    if epoch_ms >= OPEN_ENDED_EPOCH_MS:
        return None  # open-ended, no real end date
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


class SAPValuationError(Exception):
    pass


class SAPValuationClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def _get(self, path: str, filter_expr: str):
        url = f"{self.base_url}/{path}"
        with sap_semaphore:
            resp = requests.get(
                url,
                auth=self.auth,
                timeout=30,
                headers={"Accept": "application/json"},
                params={"$filter": filter_expr, "$format": "json"},
            )
        if resp.status_code != 200:
            raise SAPValuationError(f"SAP valuation service returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPValuationError(data["error"].get("message", {}).get("value", "Unknown OData error"))
        return data.get("d", {}).get("results", [])

    @staticmethod
    def _chunks(items, size):
        items = list(items)
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def _fetch_valuation_level_ids(self, product_uuids):
        """product_uuid -> list of ValuationLevel UUIDs (dashed)."""
        mapping = {uuid: [] for uuid in product_uuids}

        def fetch_chunk(chunk):
            filter_expr = " or ".join(f"MaterialUUID eq guid'{uuid}'" for uuid in chunk)
            try:
                return self._get("MaterialValuationDataValuationLevelCollection", filter_expr)
            except (SAPValuationError, requests.exceptions.RequestException) as e:
                # One flaky chunk (of ~BATCH_SIZE products) must not wipe out
                # every OTHER chunk's already-successful results - this tenant
                # is prone to intermittent timeouts under load, and treating
                # the whole multi-thousand-item batch as all-or-nothing meant
                # a single hiccup could permanently starve valuation coverage
                # (see get_standard_costs docstring/inventory_service.py sticky
                # comment). Skip just this chunk - it's retried on the next
                # refresh cycle like any other unresolved item.
                logger.warning(f"Standard Costs: valuation-level lookup failed for a chunk of {len(chunk)} product(s), skipping this round: {e}")
                return []

        chunks = list(self._chunks(product_uuids, BATCH_SIZE))
        with ThreadPoolExecutor(max_workers=6) as executor:
            results_per_chunk = list(executor.map(fetch_chunk, chunks))

        for results in results_per_chunk:
            for row in results:
                material_uuid = (row.get("MaterialUUID") or "").upper()
                object_id = row.get("ObjectID")
                if material_uuid in mapping and object_id:
                    mapping[material_uuid].append(_object_id_to_uuid(object_id))
        return mapping

    def _fetch_valuation_prices(self, valuation_level_uuids):
        """valuation_level_uuid -> list of price rows."""
        mapping = {uuid: [] for uuid in valuation_level_uuids}

        def fetch_chunk(chunk):
            filter_expr = " or ".join(f"ValuationLevelUUID eq guid'{uuid}'" for uuid in chunk)
            try:
                return self._get("MaterialValuationDataValuationPriceCollection", filter_expr)
            except (SAPValuationError, requests.exceptions.RequestException) as e:
                # Same per-chunk tolerance as _fetch_valuation_level_ids above.
                logger.warning(f"Standard Costs: valuation-price lookup failed for a chunk of {len(chunk)} level(s), skipping this round: {e}")
                return []

        chunks = list(self._chunks(valuation_level_uuids, BATCH_SIZE))
        with ThreadPoolExecutor(max_workers=6) as executor:
            results_per_chunk = list(executor.map(fetch_chunk, chunks))

        for results in results_per_chunk:
            for row in results:
                level_uuid = (row.get("ValuationLevelUUID") or "").upper()
                if level_uuid in mapping:
                    mapping[level_uuid].append(row)
        return mapping

    # SAP's PriceTypeCode on MaterialValuationDataValuationPriceCollection:
    # "1" = Inventory Cost (this tenant's Moving Average price, recalculated
    # every period - many historical rows per level) vs "2" = Estimated Cost
    # (Standard Cost - typically a single current/future-dated row). Verified
    # live (Aug 2026) against a real material: a type-"2" row can have a
    # LATER start date than every type-"1" row (e.g. a future standard-cost
    # revision already loaded into SAP ahead of its effective date), which
    # made the old "just pick whichever currently-valid row has the latest
    # start date" logic silently return the Standard Cost instead of the
    # Moving Average. User's explicit ask (Aug 2026): always use the Moving
    # Average ("1"), never Standard Cost ("2"), for every cost shown in this
    # app (BOM screen, Inventory, Purchasing Plan all route through here).
    MOVING_AVERAGE_PRICE_TYPE_CODE = "1"

    def get_standard_costs(self, product_uuids):
        """Returns {product_uuid: {"amount": float, "currency": str} | None}
        - despite the method name (kept for callers), this returns the
        Moving Average price (PriceTypeCode "1"), not Standard Cost."""
        product_uuids = list({uuid.upper() for uuid in product_uuids if uuid})
        if not product_uuids:
            return {}

        level_map = self._fetch_valuation_level_ids(product_uuids)

        all_level_uuids = [lvl for levels in level_map.values() for lvl in levels]
        price_map = self._fetch_valuation_prices(all_level_uuids) if all_level_uuids else {}

        now = datetime.now(timezone.utc)
        costs = {}
        for product_uuid in product_uuids:
            # A material can have multiple valuation levels (one per plant/
            # site), each independently "currently valid" - these are NOT
            # revisions of the same price and must not be compared against
            # each other by date. Empirically some plants carry a genuine
            # $0 valuation level (e.g. never stocked/costed there) alongside
            # other plants with the material's real moving-average price.
            # Prefer any currently-valid NON-ZERO Moving Average price; only
            # fall back to a zero Moving Average price if that's genuinely
            # the only option. Standard Cost ("2") rows are never considered.
            best_price, best_start = None, None
            best_nonzero_price, best_nonzero_start = None, None
            for level_uuid in level_map.get(product_uuid, []):
                for price_row in price_map.get(level_uuid, []):
                    if str(price_row.get("PriceTypeCode")) != self.MOVING_AVERAGE_PRICE_TYPE_CODE:
                        continue
                    start = _parse_odata_date(price_row.get("StartDate"))
                    end = _parse_odata_date(price_row.get("EndDate"))
                    is_current = (start is None or start <= now) and (end is None or now <= end)
                    if not is_current:
                        continue
                    if best_start is None or (start or now) > best_start:
                        best_price, best_start = price_row, (start or now)
                    if float(price_row.get("Amount") or 0) != 0:
                        if best_nonzero_start is None or (start or now) > best_nonzero_start:
                            best_nonzero_price, best_nonzero_start = price_row, (start or now)
            chosen = best_nonzero_price or best_price
            costs[product_uuid] = (
                {"amount": float(chosen["Amount"]), "currency": chosen.get("currencyCode")}
                if chosen
                else None
            )
        return costs
