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

# Site ID -> that site's PermanentEstablishmentUUID on
# MaterialValuationDataValuationLevelCollection (Sep 2 2026 - there is no
# SAP service in this app that exposes this mapping directly, so it was
# discovered empirically: for products stocked at exactly one site per our
# own inventory_cache, the SAME single ValuationLevel's PermanentEstablishmentUUID
# was recorded across many products per site; P8/P2W (no single-site
# product available) were resolved by elimination using a product shared
# with an already-known site, e.g. P1+P8, P2+P2W). Used by get_standard_costs
# to fix a real bug: a tied StartDate across two sites' valuation levels for
# the same product used to let the WRONG site's Moving Average price win on
# Stock Transfer prints/challans.
SITE_TO_PERMANENT_ESTABLISHMENT_UUID = {
    "P1": "635BA7F2-7D13-1EED-B8CE-CEBCAEB25CA5",
    "P2": "635BA7F2-7D13-1EDD-B8D1-AD5E5DDCA263",
    "P2W": "FA163E48-7A8A-1FE1-88A4-6FFF493D76F9",
    "P3": "15A43E26-4017-1EEF-B78F-50D714FBEA79",
    "P4": "635BA7F2-7D13-1EDD-B8D5-024941AA759D",
    "P7": "635BA7F2-7D13-1EED-B8D5-8AF511A06182",
    "P8": "635BA7F2-7D13-1EDD-B8D0-D5B7DCECF950",
    "P9": "4D85C88B-953A-1EDF-8B8E-099F70CE3711",
}


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
        """product_uuid -> list of (ValuationLevel UUID (dashed), PermanentEstablishmentUUID) tuples."""
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
                    pe_uuid = (row.get("PermanentEstablishmentUUID") or "").upper() or None
                    mapping[material_uuid].append((_object_id_to_uuid(object_id), pe_uuid))
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

    def has_valuation_level(self, product_uuids, site_id: str):
        """Returns {product_uuid: bool} for whether each product has a
        genuine Valuation LEVEL record at `site_id`'s own
        PermanentEstablishmentUUID (SITE_TO_PERMANENT_ESTABLISHMENT_UUID
        above), or None (not an empty dict) if `site_id` isn't in that
        mapping yet - callers must treat None as "can't tell, don't
        block" rather than "confirmed missing" (Sep 21 2026, user's
        explicit ask: proactively catch "no Valuation at destination
        site" BEFORE creating a Stock Transfer Order, instead of only
        discovering it much later when GRN/receiving fails).

        Unlike get_standard_costs, this deliberately does NOT fall back
        to another site's level - a product whose only Valuation level
        is at a DIFFERENT site correctly comes back False here, matching
        what would actually block a real SAP write at this site."""
        site_pe_uuid = SITE_TO_PERMANENT_ESTABLISHMENT_UUID.get((site_id or "").strip().upper())
        if not site_pe_uuid:
            return None
        product_uuids = list({uuid.upper() for uuid in product_uuids if uuid})
        if not product_uuids:
            return {}
        level_map = self._fetch_valuation_level_ids(product_uuids)
        return {pid: any(pe == site_pe_uuid for _lvl, pe in levels) for pid, levels in level_map.items()}

    def get_standard_costs(self, product_uuids, site_id: str = None):
        """Returns {product_uuid: {"amount": float, "currency": str} | None}
        - despite the method name (kept for callers), this returns the
        Moving Average price (PriceTypeCode "1"), not Standard Cost.

        `site_id` (Sep 2 2026 fix): a material can have one valuation level
        PER SITE, each with its own independent price history. Previously,
        when two sites' currently-valid prices happened to tie on StartDate,
        whichever one came first in SAP's response order silently won - so a
        Stock Transfer print could show another site's rate instead of the
        actual ship-from site's. Pass the ship-from/relevant site_id to only
        ever consider that site's own valuation level. Omit it (existing
        callers, unchanged) to keep the old "best across all sites"
        behavior - not recommended for anything that displays a rate tied to
        one specific site."""
        product_uuids = list({uuid.upper() for uuid in product_uuids if uuid})
        if not product_uuids:
            return {}
        site_pe_uuid = SITE_TO_PERMANENT_ESTABLISHMENT_UUID.get((site_id or "").strip().upper())

        level_map = self._fetch_valuation_level_ids(product_uuids)

        all_level_uuids = [lvl for levels in level_map.values() for lvl, _pe in levels]
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
            levels = level_map.get(product_uuid, [])
            if site_pe_uuid:
                site_levels = [lvl for lvl, pe in levels if pe == site_pe_uuid]
                # If this site genuinely has no valuation level of its own
                # for this product, fall back to "all sites" rather than
                # silently return nothing - better an approximate price than
                # a blank rate on a print.
                # Sep 2 2026 BUG FOUND + FIXED: the "all sites" fallback used
                # to be the raw `levels` list of (lvl, pe) TUPLES (never
                # flattened), and when `site_pe_uuid` was falsy (every
                # non-site-aware caller: BOM Explorer, Inventory, Purchasing
                # Plan) the `if site_pe_uuid:` branch never ran at all, so
                # `levels` stayed as tuples in BOTH cases - `price_map.get
                # (level_uuid, [])` below then always missed (price_map is
                # keyed by plain UUID strings), silently returning None for
                # EVERY product on any caller that doesn't pass site_id, and
                # even for site-aware callers whenever a product has no
                # valuation level at that specific site. Always flatten to
                # plain lvl-string lists now, live-verified fixes "0 of N
                # components have a live SAP cost" on BOM Explorer.
                levels = site_levels or [lvl for lvl, _pe in levels]
            else:
                levels = [lvl for lvl, _pe in levels]
            best_price, best_start = None, None
            best_nonzero_price, best_nonzero_start = None, None
            for level_uuid in levels:
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
