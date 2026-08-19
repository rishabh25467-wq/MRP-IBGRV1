"""SAP Business ByDesign multi-level BOM explosion via QueryProductionBillofMaterialsIn SOAP service."""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

SOAP_ACTION = "http://sap.com/xi/A1S/Global/QueryProductionBillofMaterialsIn/QueryProductionBillOfMaterialByElementsRequest"
MAX_DEPTH = 6
MAX_LOOKUPS = 300
# Max distinct products packed into ONE batched SelectionByOutputProductID
# query (see _fetch_boms_by_output_products_batch). Kept comfortably under
# the query's QueryHitsMaximumNumberValue=500 even if every product in the
# batch happens to have several BOM revisions/alternates (empirically rare,
# but a few products in this tenant do have 3-4) - 60 products x up to ~6
# revisions each = 360 hits, safe margin under 500.
BATCH_CHUNK_SIZE = 60


class SAPSoapError(Exception):
    pass


class SAPSoapBOMClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def _query(self, selection_xml: str) -> str:
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:ProductionBillsOfMaterialsQueryByElementsMessage xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <ProcessingConditions>
        <QueryHitsMaximumNumberValue>500</QueryHitsMaximumNumberValue>
        <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
      </ProcessingConditions>
      <ProductionBillOfMaterials>
        {selection_xml}
      </ProductionBillOfMaterials>
    </n0:ProductionBillsOfMaterialsQueryByElementsMessage>
  </soapenv:Body>
</soapenv:Envelope>"""
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                # Separate connect/read timeouts (8s/15s), not one shared 15s:
                # live traffic showed the actual failure mode is the TCP
                # connect stalling (intermittent, ~every few minutes,
                # ConnectTimeoutError), not a slow response body - a single
                # blended timeout wastes up to 15s just discovering a dead
                # connect attempt. Failing the connect stage fast (8s) frees
                # that budget for the retry in _safe_fetch_boms_batch
                # instead, without shrinking the 15s a genuinely slow-but-
                # working SAP response is still allowed to take.
                response = requests.post(self.endpoint, data=body.encode("utf-8"), headers=headers, auth=self.auth, timeout=(8, 15))
        except requests.exceptions.RequestException as e:
            raise SAPSoapError(str(e))

        if response.status_code == 401:
            raise SAPSoapError("SAP SOAP authentication failed. Check communication arrangement credentials.")
        if response.status_code != 200:
            raise SAPSoapError(f"SAP SOAP service responded with HTTP {response.status_code}")
        return response.text

    def check_connection(self):
        selection = """<SelectionByProductionBillOfMaterialID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundaryIdentifier>__CONNECTION_CHECK__</LowerBoundaryIdentifier>
        </SelectionByProductionBillOfMaterialID>"""
        self._query(selection)
        return True

    def _fetch_bom_by_id(self, bom_id: str):
        selection = f"""<SelectionByProductionBillOfMaterialID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundaryIdentifier>{bom_id}</LowerBoundaryIdentifier>
        </SelectionByProductionBillOfMaterialID>"""
        return self._parse(self._query(selection))

    def _fetch_bom_by_output_product(self, product_id: str):
        selection = f"""<SelectionByOutputProductID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundaryIdentifier>{product_id}</LowerBoundaryIdentifier>
        </SelectionByOutputProductID>"""
        return self._parse(self._query(selection))

    def _fetch_boms_by_output_products_batch(self, product_ids: list) -> dict:
        """Batched sub-BOM lookup (Feb 2026 speedup): fetches MANY products'
        BOMs in a SINGLE SOAP round-trip instead of one request per product.
        Empirically confirmed against this tenant - QueryProductionBillof
        MaterialsIn accepts multiple SelectionByOutputProductID blocks in
        one request and returns hits for all of them together (18 products
        batched -> 1.6s total, vs. what would be many sequential 3-at-a-
        time rounds given the tenant's hard 3-concurrent-session cap, see
        sap_rate_limiter.py). This is a pure latency win with NO increase in
        concurrent SAP load - one connection carrying N products' worth of
        work is strictly less load than N separate connections, so it does
        not risk the self-inflicted-timeout problem that raising
        concurrency limits would. Returns {product_id: bom_dict}; a
        product_id absent from the result had no BOM in SAP (same meaning
        as _fetch_bom_by_output_product returning None for it)."""
        if not product_ids:
            return {}
        selection_xml = "".join(
            f"""<SelectionByOutputProductID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundaryIdentifier>{pid}</LowerBoundaryIdentifier>
        </SelectionByOutputProductID>"""
            for pid in product_ids
        )
        return self._parse_batch(self._query(selection_xml))

    @staticmethod
    def _eco_matches_own_product(eco_id: str, product_id: str) -> bool:
        """A well-formed, part-specific Engineering Change Order ID follows the
        SAP convention '{product_id}_{revision}'. When an item's change
        history mixes naming conventions (e.g. a shared/batch ECO numbered
        after the parent BOM vs. a proper part-specific ECO), the part-specific
        one is the trustworthy signal of which change record actually applies
        to that exact input product - it should be preferred over one merely
        because it has a numerically higher suffix from an unrelated counter."""
        if not eco_id or not product_id:
            return False
        return re.sub(r"_[\d.]+$", "", eco_id) == product_id

    @staticmethod
    def _revision_number(value: str) -> float:
        """Extract the trailing revision suffix after the last '_' (e.g.
        '..._2' -> 2.0, '..._4.4' -> 4.4) so revisions can be compared numerically."""
        match = re.search(r"_([\d.]+)$", value)
        try:
            return float(match.group(1)) if match else -1.0
        except ValueError:
            return -1.0

    @classmethod
    def _parse(cls, xml_text: str):
        """A SelectionByOutputProductID query can return multiple BOM
        revisions for the same product. Two very different situations look
        identical at first glance (same product, multiple revision
        suffixes) and must be told apart:

          1. Administrative supersession - an old revision replaced by a
             newer one using the exact same materials (just a date/metadata
             change). Auto-resolve to the latest Consistent one, as before.
          2. GENUINE ALTERNATE BOMs - multiple revisions that are ALL
             currently Consistent/active, but use DIFFERENT raw materials
             to make the SAME output (e.g. a bracket makeable in either
             Cold-Rolled or Hot-Rolled sheet steel, kept as two parallel
             BOMs because production picks whichever material happens to be
             in stock/cheaper for a given run). Auto-picking one of these
             and discarding the other is wrong - it's a genuine business
             decision that belongs to production planning, not something
             this app should silently guess. Confirmed empirically against
             this tenant (e.g. HTBS-SPAIN has 3 Consistent revisions: two
             share identical materials - a normal revision chain - the
             third genuinely differs, description "...WITH PALLET").

        Distinguishing them: group Consistent revisions by a "recipe
        fingerprint" (the set of input product IDs used) - revisions
        sharing a fingerprint are the SAME recipe (collapse to the latest,
        as before); revisions with a DIFFERENT fingerprint are true
        alternates. The chosen default stays exactly what it always was
        (highest revision among Consistent) - callers/production only need
        to look at the `alternates` list when it's non-empty and decide."""
        hit_blocks = re.findall(r"<ProductionBillOfMaterials>(.*?)</ProductionBillOfMaterials>", xml_text, re.S)
        return cls._build_bom_from_hit_blocks(hit_blocks)

    @classmethod
    def _parse_batch(cls, xml_text: str) -> dict:
        """Batched analogue of _parse() for a response covering MULTIPLE
        different output products in one query (see
        _fetch_boms_by_output_products_batch) - splits the response's hit
        blocks by each hit's OWN output product ID first, then applies the
        exact same per-product revision/alternate logic as _parse()
        (via the shared _build_bom_from_hit_blocks) independently per group.
        Returns {product_id: bom_dict}; a product with zero hits (no BOM in
        SAP) simply won't be a key in the returned dict - callers treat a
        missing key the same as _parse() returning None for that product."""
        hit_blocks = re.findall(r"<ProductionBillOfMaterials>(.*?)</ProductionBillOfMaterials>", xml_text, re.S)
        grouped: dict = {}
        for block in hit_blocks:
            variant_match = re.search(r"<ProductionBillOfMaterialVariant>(.*?)</ProductionBillOfMaterialVariant>", block, re.S)
            if not variant_match:
                continue
            # The outer <ProductID> wrapping this variant's own output
            # product wraps ProductTypeCode/ProductIdentifierTypeCode/an
            # INNER <ProductID>VALUE</ProductID> - a plain non-nested
            # `[^<]*` search naturally skips the outer tag (its content
            # starts with another '<', so it can't match `[^<]*</ProductID>`)
            # and lands on the true leaf value, same trick already used
            # for InputProductID elsewhere in this parser.
            pid_match = re.search(r"<ProductID>([^<]*)</ProductID>", variant_match.group(1))
            if not pid_match:
                continue
            grouped.setdefault(pid_match.group(1), []).append(block)
        return {pid: cls._build_bom_from_hit_blocks(blocks) for pid, blocks in grouped.items()}

    @classmethod
    def _build_bom_from_hit_blocks(cls, hit_blocks: list):
        """Shared by _parse() (hit_blocks all belong to one already-known
        product) and _parse_batch() (hit_blocks pre-split by output product,
        one call per product) - picks the winning revision/alternates and
        builds that single product's bom dict. Returns None if hit_blocks
        is empty."""
        if not hit_blocks:
            return None

        candidates = []
        for block in hit_blocks:
            id_match = re.search(r"<ProductionBillOfMaterialID>([^<]*)</ProductionBillOfMaterialID>", block)
            if not id_match:
                continue
            bom_id = id_match.group(1)
            consistency_match = re.search(r"<ConsistencyStatus>([^<]*)</ConsistencyStatus>", block)
            is_consistent = bool(consistency_match) and consistency_match.group(1).strip() == "3"

            variant_match = re.search(r"<ProductionBillOfMaterialVariant>(.*?)</ProductionBillOfMaterialVariant>", block, re.S)
            variant_block = variant_match.group(1) if variant_match else ""
            desc_match = re.search(r'<VariantDescription[^>]*>([^<]*)</VariantDescription>', variant_block)
            item_ids = re.findall(r"<InputProductID>.*?<ProductID>([^<]*)</ProductID>", variant_block, re.S)
            item_desc_match = re.search(r"<InputProductDescription>([^<]*)</InputProductDescription>", variant_block)

            candidates.append({
                "bom_id": bom_id, "block": block, "revision": cls._revision_number(bom_id),
                "is_consistent": is_consistent,
                "description": desc_match.group(1) if desc_match else None,
                "fingerprint": frozenset(item_ids),
                "sample_item_id": item_ids[0] if item_ids else None,
                "sample_item_description": item_desc_match.group(1) if item_desc_match else None,
            })

        if not candidates:
            return None

        # "No BOM" transparency (Aug 2026, per user decision): the active-
        # BOM flag logic below stays untouched, but we ALSO want to surface
        # (as a separate, non-authoritative note) when a component only
        # shows up in an OLD/superseded revision of this parent - so a user
        # investigating a "No BOM" flag isn't left thinking SAP has zero
        # record of the part ever being used here. `historical_input_ids`
        # is a superset covering EVERY input product ID seen in ANY
        # candidate revision (Consistent or not) - collected for free from
        # data already parsed below, no extra SAP round-trip.
        historical_input_ids = set()
        for c in candidates:
            historical_input_ids.update(c["fingerprint"])

        # Same consistency-first tie-break as always: only consider
        # non-Consistent candidates if NOTHING is Consistent at all.
        consistent = [c for c in candidates if c["is_consistent"]]
        pool = consistent if consistent else candidates

        by_fingerprint = {}
        for c in pool:
            fp = c["fingerprint"]
            if fp not in by_fingerprint or c["revision"] > by_fingerprint[fp]["revision"]:
                by_fingerprint[fp] = c
        representatives = sorted(by_fingerprint.values(), key=lambda c: -c["revision"])

        best = representatives[0]
        best_block, best_id = best["block"], best["bom_id"]
        alternates = [
            {
                "bom_id": r["bom_id"], "description": r["description"],
                "sample_item_id": r["sample_item_id"], "sample_item_description": r["sample_item_description"],
            }
            for r in representatives
        ] if len(representatives) > 1 else []

        # The root product's OWN UUID (needed for Standard Costs / SAP
        # Planning lookups on items that are BOM roots themselves, e.g.
        # finished/semi-finished goods, which otherwise never get a
        # product_uuid captured since they'd only get one by appearing as
        # someone ELSE's ingredient) - carried on the winning revision's
        # own ProductionBillOfMaterialVariant, not per-item.
        variant_match = re.search(r"<ProductionBillOfMaterialVariant>(.*?)</ProductionBillOfMaterialVariant>", best_block, re.S)
        variant_block_text = variant_match.group(1) if variant_match else ""
        root_uuid_match = re.search(r"<ProductUUID>([^<]*)</ProductUUID>", variant_block_text)

        # Aug 2026 fix: the nested <ItemGroupItem><ProductionBillOfMaterial
        # ItemGroupChangeState> records used below to build `groups` do NOT
        # carry an EngineeringChangeOrderValidFromDate themselves - dates
        # only live in a SEPARATE flat list directly under the variant,
        # <ProductionBillOfMaterialVariantItemChangeState> (same ItemGroupID
        # + ItemID + EngineeringChangeOrderID, just a different/summary view
        # of the same underlying change). Cross-referencing this flat list
        # gives an unambiguous chronological signal for picking the truly
        # CURRENT input product on a line whose component was swapped over
        # time - confirmed live on P26675's line 10/10: CS14X48X1.2 (ECO
        # "CUST14X4812", ValidFrom 2023-08-01) is genuinely current, but the
        # naming-convention heuristic alone picked the OLDER CUST14X48X1.2MM
        # (ECO "P26675_1", ValidFrom 2023-06-01) just because that ECO name
        # happened to match the parent product's own ID convention.
        valid_from_by_key = {}
        for flat_match in re.finditer(
            r"<ProductionBillOfMaterialVariantItemChangeState>(.*?)</ProductionBillOfMaterialVariantItemChangeState>",
            variant_block_text, re.S,
        ):
            flat_block = flat_match.group(1)
            g_match = re.search(r"<ItemGroupID>([^<]*)</ItemGroupID>", flat_block)
            i_match = re.search(r"<ItemID>([^<]*)</ItemID>", flat_block)
            eco_match2 = re.search(r"<EngineeringChangeOrderID>([^<]*)</EngineeringChangeOrderID>", flat_block)
            date_match = re.search(r"<EngineeringChangeOrderValidFromDate>([^<]*)</EngineeringChangeOrderValidFromDate>", flat_block)
            if g_match and i_match and eco_match2 and date_match:
                valid_from_by_key[(g_match.group(1), i_match.group(1), eco_match2.group(1))] = date_match.group(1).strip()

        bom = {
            "bom_id": best_id, "product_uuid": root_uuid_match.group(1) if root_uuid_match else None,
            "groups": [], "alternates": alternates,
            "historical_input_ids": list(historical_input_ids),
        }

        for group_match in re.finditer(r"<ProductionBillOfMaterialItemGroup>(.*?)</ProductionBillOfMaterialItemGroup>", best_block, re.S):
            group_block = group_match.group(1)
            group_id_match = re.search(r"<ItemGroupID>([^<]*)</ItemGroupID>", group_block)
            group_id = group_id_match.group(1) if group_id_match else None
            items = []

            for item_match in re.finditer(r"<ItemGroupItem>(.*?)</ItemGroupItem>", group_block, re.S):
                item_block = item_match.group(1)
                item_id_match = re.search(r"<ItemGroupItemID>([^<]*)</ItemGroupItemID>", item_block)

                # An ItemGroupItem can carry MULTIPLE ChangeState entries (revision
                # history for that specific line). Aug 2026 fix: an item's
                # EngineeringChangeOrderValidFromDate (an unambiguous ISO
                # date straight from SAP) is compared FIRST - confirmed live
                # on P26675's line 10/10: CS14X48X1.2 (ECO "CUST14X4812",
                # ValidFrom 2023-08-01) is the genuinely CURRENT input, but
                # the naming-convention heuristic below alone picked the
                # OLDER CUST14X48X1.2MM (ECO "P26675_1", ValidFrom
                # 2023-06-01) instead, purely because that ECO's name
                # happened to match the parent product's own ID convention.
                # The naming heuristic only serves as a tie-break now, for
                # the (rarer) case a ValidFromDate is missing or genuinely
                # identical - see _eco_matches_own_product's docstring for
                # why it still matters then.
                change_states = re.findall(
                    r"<ProductionBillOfMaterialItemGroupChangeState>(.*?)</ProductionBillOfMaterialItemGroupChangeState>",
                    item_block, re.S,
                )
                if not change_states:
                    continue

                best_state, best_state_key = None, None
                item_group_item_id = item_id_match.group(1) if item_id_match else None
                for state_block in change_states:
                    eco_match = re.search(r"<EngineeringChangeOrderID>([^<]*)</EngineeringChangeOrderID>", state_block)
                    pid_match = re.search(r"<InputProductID>.*?<ProductID>([^<]*)</ProductID>", state_block, re.S)
                    eco_id = eco_match.group(1) if eco_match else None
                    revision = cls._revision_number(eco_id) if eco_id else -1.0
                    valid_from = valid_from_by_key.get((group_id, item_group_item_id, eco_id), "")
                    self_matched = cls._eco_matches_own_product(eco_id, pid_match.group(1) if pid_match else None)
                    state_key = (valid_from, self_matched, revision)
                    if pid_match:
                        # Every change-state's input product ID (not just the
                        # winning one) - a line whose input product itself
                        # changed over time (e.g. swapped for a different
                        # part) leaves the OLD product ID only reachable
                        # here, never in the final `groups` below.
                        historical_input_ids.add(pid_match.group(1))
                    if best_state is None or state_key > best_state_key:
                        best_state, best_state_key = state_block, state_key

                product_id_match = re.search(r"<InputProductID>.*?<ProductID>([^<]*)</ProductID>", best_state, re.S)
                if not product_id_match:
                    continue

                desc_match = re.search(r"<InputProductDescription>([^<]*)</InputProductDescription>", best_state)
                qty_match = re.search(r"<InputProductQuantity[^>]*>([^<]*)</InputProductQuantity>", best_state)
                uom_match = re.search(r"<InputProductQuantityUoM>([^<]*)</InputProductQuantityUoM>", best_state)
                eco_match = re.search(r"<EngineeringChangeOrderID>([^<]*)</EngineeringChangeOrderID>", best_state)
                deleted_match = re.search(r"<DeletionIndicator>([^<]*)</DeletionIndicator>", best_state)
                uuid_match = re.search(r"<InputProductUUID>([^<]*)</InputProductUUID>", best_state)

                items.append({
                    "item_id": item_id_match.group(1) if item_id_match else None,
                    "product_id": product_id_match.group(1),
                    "product_uuid": uuid_match.group(1) if uuid_match else None,
                    "description": desc_match.group(1) if desc_match else None,
                    "quantity": float(qty_match.group(1)) if qty_match and qty_match.group(1) else None,
                    "unit_of_measure": uom_match.group(1) if uom_match else None,
                    "eco_id": eco_match.group(1) if eco_match else None,
                    "active": (deleted_match.group(1) if deleted_match else "false") != "true",
                })

            bom["groups"].append({"group_id": group_id, "items": items})

        return bom

    def explode_bom(self, bom_id: str, shared_cache: dict = None):
        """Recursively explode a BOM into a hierarchical tree (each node with its
        own children list), resolving each component's own sub-BOM (if any) by
        output product, matching SAP's native Multi-Level BoM Visualization
        report. Sub-BOM lookups are resolved level-by-level (BFS) concurrently for
        speed, then assembled into a tree. Accepts either an exact BOM ID
        (e.g. 'P26584_2') or a bare product/part ID (e.g. 'P26584'), in which case
        the latest active revision is resolved automatically via output product.

        `shared_cache` optionally lets a caller re-use sub-BOM lookups across
        MULTIPLE explode_bom() calls (e.g. the Purchasing Plan feature explodes
        many top-level parts that commonly share the same hardware/packaging
        sub-components) - pass the same dict into successive calls to avoid
        redundant SAP round-trips. Defaults to a fresh, call-local cache."""
        root = self._fetch_bom_by_id(bom_id) or self._fetch_bom_by_output_product(bom_id)
        if root is None:
            return None

        sub_bom_cache = shared_cache if shared_cache is not None else {}
        total_components = 0
        max_level_seen = 0
        lookups_done = 0
        # Hard wall-clock cap on the whole sub-BOM exploration phase. During
        # a severe/sustained SAP outage, per-batch retries (_safe_fetch_
        # boms_batch, 2x8s connect timeout each) can still add up across
        # multiple tree levels - once this deadline passes, we stop
        # fetching further sub-BOMs and return the tree AS-IS - any
        # not-yet-reached components simply show as leaf nodes (degraded
        # but instant, instead of a total failure).
        deadline = time.time() + 40
        # each frontier entry: (bom, level, ancestors, children_list_to_append_into, parent_cum_qty)
        root_children = []
        frontier = [(root, 1, frozenset({bom_id, root["bom_id"]}), root_children, 1.0)]

        while frontier and lookups_done < MAX_LOOKUPS and time.time() < deadline:
            candidate_ids = set()
            for bom, level, ancestors, _, _ in frontier:
                if level >= MAX_DEPTH:
                    continue
                for group in bom["groups"]:
                    for item in group["items"]:
                        pid = item["product_id"]
                        if item["active"] and pid not in ancestors and pid not in sub_bom_cache:
                            candidate_ids.add(pid)

            to_fetch = list(candidate_ids)[: max(0, MAX_LOOKUPS - lookups_done)]
            if to_fetch:
                # Split into batched SOAP requests (BATCH_CHUNK_SIZE products
                # per request) instead of one request per product - see
                # _fetch_boms_by_output_products_batch. Chunks themselves are
                # still submitted through a small pool, but each one is a
                # SINGLE round-trip covering many products, so the typical
                # case (a level with well under BATCH_CHUNK_SIZE candidates)
                # collapses to exactly ONE SAP call instead of one-per-item.
                chunks = [to_fetch[i:i + BATCH_CHUNK_SIZE] for i in range(0, len(to_fetch), BATCH_CHUNK_SIZE)]
                with ThreadPoolExecutor(max_workers=8) as executor:
                    chunk_results = list(executor.map(self._safe_fetch_boms_batch, chunks))
                for pid in to_fetch:
                    sub_bom_cache[pid] = None
                for result in chunk_results:
                    sub_bom_cache.update(result)
                lookups_done += len(to_fetch)

            next_frontier = []
            for bom, level, ancestors, children_out, parent_cum_qty in frontier:
                max_level_seen = max(max_level_seen, level)
                for group in bom["groups"]:
                    for item in group["items"]:
                        if not item["active"]:
                            continue
                        cum_qty = round(item["quantity"] * parent_cum_qty, 6) if item["quantity"] is not None else None
                        node = {
                            "level": level,
                            "group_id": group["group_id"],
                            "item_id": item["item_id"],
                            "product_id": item["product_id"],
                            "product_uuid": item["product_uuid"],
                            "description": item["description"],
                            "quantity": cum_qty,
                            "unit_of_measure": item["unit_of_measure"],
                            "eco_id": item["eco_id"],
                            "active": item["active"],
                            "has_sub_bom": False,
                            "children": [],
                        }
                        children_out.append(node)
                        total_components += 1

                        sub_bom = sub_bom_cache.get(item["product_id"])
                        if sub_bom and sub_bom["groups"] and item["product_id"] not in ancestors:
                            node["has_sub_bom"] = True
                            next_frontier.append((
                                sub_bom, level + 1, ancestors | {item["product_id"]},
                                node["children"], cum_qty if cum_qty is not None else parent_cum_qty,
                            ))

            frontier = next_frontier

        return {
            "bom_id": root["bom_id"],
            "total_components": total_components,
            "max_level": max_level_seen,
            "tree": root_children,
        }

    def _safe_fetch_boms_batch(self, product_ids: list) -> dict:
        # 2 attempts, flat 1.5s backoff - same retry policy already applied
        # to the (now-removed) per-item fetch. A failed batch degrades every
        # product in it to "unresolved this run" (shown as leaf nodes) -
        # never partially executed, matching the existing graceful-
        # degradation behavior for a single item's failure.
        #
        # On a SUCCESSFUL response, explicitly backfill None for every
        # requested id the parsed response didn't mention - SAP's own
        # response simply omits ids with no BOM (see
        # _fetch_boms_by_output_products_batch's docstring) rather than
        # marking them explicitly, so without this backfill a caller can't
        # tell "SAP confirmed no BOM for this id" apart from "the whole
        # batch failed and this id was never actually resolved" - both
        # looked identical (id absent from the dict). That ambiguity was a
        # real bug: bom_cache_service.refresh_stale_nodes() treated
        # confirmed-no-BOM leaf/raw-material items as permanent failures,
        # never advancing their last_checked_at, which is why plans could
        # show "BOM data as of N days ago" even when the refresh job was
        # running fine on schedule (found live, Aug 2026). Only a
        # genuinely failed batch (exception on every attempt) should still
        # return {} so the caller retries it next cycle.
        attempts = 2
        for attempt in range(attempts):
            try:
                result = self._fetch_boms_by_output_products_batch(product_ids)
                for pid in product_ids:
                    result.setdefault(pid, None)
                return result
            except SAPSoapError as e:
                logger.warning(f"Batch sub-BOM lookup failed for {len(product_ids)} product(s) (attempt {attempt + 1}/{attempts}): {e}")
                if attempt < attempts - 1:
                    time.sleep(1.5)
        return {}
