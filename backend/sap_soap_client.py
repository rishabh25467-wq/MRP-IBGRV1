"""SAP Business ByDesign multi-level BOM explosion via QueryProductionBillofMaterialsIn SOAP service."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

SOAP_ACTION = "http://sap.com/xi/A1S/Global/QueryProductionBillofMaterialsIn/QueryProductionBillOfMaterialByElementsRequest"
MAX_DEPTH = 6
MAX_LOOKUPS = 300


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
            response = requests.post(self.endpoint, data=body.encode("utf-8"), headers=headers, auth=self.auth, timeout=30)
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

    @staticmethod
    def _parse(xml_text: str):
        bom_id_match = re.search(r"<ProductionBillOfMaterialID>([^<]*)</ProductionBillOfMaterialID>", xml_text)
        if not bom_id_match:
            return None

        bom = {"bom_id": bom_id_match.group(1), "groups": []}

        for group_match in re.finditer(r"<ProductionBillOfMaterialItemGroup>(.*?)</ProductionBillOfMaterialItemGroup>", xml_text, re.S):
            group_block = group_match.group(1)
            group_id_match = re.search(r"<ItemGroupID>([^<]*)</ItemGroupID>", group_block)
            group_id = group_id_match.group(1) if group_id_match else None
            items = []

            for item_match in re.finditer(r"<ItemGroupItem>(.*?)</ItemGroupItem>", group_block, re.S):
                item_block = item_match.group(1)
                item_id_match = re.search(r"<ItemGroupItemID>([^<]*)</ItemGroupItemID>", item_block)
                product_id_match = re.search(r"<InputProductID>.*?<ProductID>([^<]*)</ProductID>", item_block, re.S)
                desc_match = re.search(r"<InputProductDescription>([^<]*)</InputProductDescription>", item_block)
                qty_match = re.search(r"<InputProductQuantity[^>]*>([^<]*)</InputProductQuantity>", item_block)
                uom_match = re.search(r"<InputProductQuantityUoM>([^<]*)</InputProductQuantityUoM>", item_block)
                eco_match = re.search(r"<EngineeringChangeOrderID>([^<]*)</EngineeringChangeOrderID>", item_block)
                deleted_match = re.search(r"<DeletionIndicator>([^<]*)</DeletionIndicator>", item_block)

                if not product_id_match:
                    continue

                items.append({
                    "item_id": item_id_match.group(1) if item_id_match else None,
                    "product_id": product_id_match.group(1),
                    "description": desc_match.group(1) if desc_match else None,
                    "quantity": float(qty_match.group(1)) if qty_match and qty_match.group(1) else None,
                    "unit_of_measure": uom_match.group(1) if uom_match else None,
                    "eco_id": eco_match.group(1) if eco_match else None,
                    "active": (deleted_match.group(1) if deleted_match else "false") != "true",
                })

            bom["groups"].append({"group_id": group_id, "items": items})

        return bom

    def explode_bom(self, bom_id: str):
        """Recursively explode a BOM into a flat multi-level list, resolving each
        component's own sub-BOM (if any) by output product, matching SAP's native
        Multi-Level BoM Visualization report. Processed level-by-level (BFS) with
        concurrent sub-BOM lookups for speed."""
        root = self._fetch_bom_by_id(bom_id)
        if root is None:
            return None

        sub_bom_cache = {}
        rows = []
        lookups_done = 0
        frontier = [(root, 1, frozenset({bom_id}))]

        while frontier and lookups_done < MAX_LOOKUPS:
            candidate_ids = set()
            for bom, level, ancestors in frontier:
                if level >= MAX_DEPTH:
                    continue
                for group in bom["groups"]:
                    for item in group["items"]:
                        pid = item["product_id"]
                        if pid not in ancestors and pid not in sub_bom_cache:
                            candidate_ids.add(pid)

            to_fetch = list(candidate_ids)[: max(0, MAX_LOOKUPS - lookups_done)]
            if to_fetch:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    fetched = list(executor.map(self._safe_fetch_by_output_product, to_fetch))
                for pid, sub in zip(to_fetch, fetched):
                    sub_bom_cache[pid] = sub
                lookups_done += len(to_fetch)

            next_frontier = []
            for bom, level, ancestors in frontier:
                for group in bom["groups"]:
                    for item in group["items"]:
                        row = {
                            "level": level,
                            "group_id": group["group_id"],
                            "item_id": item["item_id"],
                            "product_id": item["product_id"],
                            "description": item["description"],
                            "quantity": item["quantity"],
                            "unit_of_measure": item["unit_of_measure"],
                            "eco_id": item["eco_id"],
                            "active": item["active"],
                            "has_sub_bom": False,
                        }
                        rows.append(row)

                        sub_bom = sub_bom_cache.get(item["product_id"])
                        if sub_bom and sub_bom["groups"] and item["product_id"] not in ancestors:
                            row["has_sub_bom"] = True
                            next_frontier.append((sub_bom, level + 1, ancestors | {item["product_id"]}))

            frontier = next_frontier

        return {
            "bom_id": root["bom_id"],
            "total_components": len(rows),
            "rows": rows,
        }

    def _safe_fetch_by_output_product(self, product_id: str):
        try:
            return self._fetch_bom_by_output_product(product_id)
        except SAPSoapError as e:
            logger.warning(f"Sub-BOM lookup failed for {product_id}: {e}")
            return None
