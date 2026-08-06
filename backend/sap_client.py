"""SAP Business ByDesign BOM extraction client (OData, Basic Auth)."""
import logging
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class SAPConnectionError(Exception):
    pass


class SAPBOMClient:
    def __init__(self, instance_url: str, username: str, password: str):
        self.instance_url = instance_url.rstrip("/")
        self.hierarchy_url = f"{self.instance_url}/sap/byd/odata/cust/v1/bom_service/ProductionBillOfMaterialCollection"
        self.details_url = f"{self.instance_url}/sap/byd/odata/cust/v1/bom_service1/ProductionBillOfMaterialItemGroupItemChangeStateCollection"
        self.auth = HTTPBasicAuth(username, password)
        self.headers = {"Accept": "application/json"}

    def _get(self, url: str, params: dict):
        try:
            response = requests.get(url, params=params, headers=self.headers, auth=self.auth, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPConnectionError(str(e))

        if response.status_code == 401:
            raise SAPConnectionError("SAP authentication failed. Check username/password.")
        if response.status_code != 200:
            raise SAPConnectionError(f"SAP responded with HTTP {response.status_code}")
        return response.json()

    def check_connection(self):
        self._get(self.hierarchy_url, {"$top": 1, "$format": "json"})
        return True

    @staticmethod
    def _convert_sap_date(sap_date_str):
        if not sap_date_str or "/Date(" not in sap_date_str:
            return None
        try:
            timestamp = int(sap_date_str.split("(")[1].split(")")[0]) / 1000
            return datetime.fromtimestamp(timestamp).isoformat()
        except Exception:
            return None

    def get_bom_hierarchy(self, bom_id: str):
        params = {
            "$filter": f"ID eq '{bom_id}'",
            "$expand": "ProductionBillOfMaterialItemGroup1/ProductionBillOfMaterialItemGroupItem",
            "$format": "json",
        }
        data = self._get(self.hierarchy_url, params)
        results = data.get("d", {}).get("results", [])
        if not results:
            return None

        bom = results[0]
        hierarchy = {
            "bom_id": bom.get("ID"),
            "object_id": bom.get("ObjectID"),
            "groups": [],
        }

        for item_group in bom.get("ProductionBillOfMaterialItemGroup1", []):
            group = {
                "group_id": item_group.get("BillOfMaterialItemGroupID"),
                "group_number": item_group.get("ID"),
                "start_date": self._convert_sap_date(item_group.get("StartDate")),
                "end_date": self._convert_sap_date(item_group.get("EndDate")),
                "item_ids": [
                    item.get("ObjectID")
                    for item in item_group.get("ProductionBillOfMaterialItemGroupItem", [])
                ],
            }
            hierarchy["groups"].append(group)

        return hierarchy

    CHANGE_STATE_OFFSET = 0x4000

    @classmethod
    def _to_change_state_id(cls, item_object_id: str) -> str:
        """Each ItemGroupItem's component detail lives in bom_service1's
        ChangeStateCollection at a deterministic offset from its own ObjectID
        (verified empirically: change_state_id == item_object_id + 0x4000)."""
        return format(int(item_object_id, 16) + cls.CHANGE_STATE_OFFSET, "032X")

    def get_component_details_by_ids(self, item_object_ids: list):
        """Direct-key lookup of component details for specific hierarchy items,
        chunked into OR filters to avoid relying on shared ECO/BOM identifiers
        (which don't always match between hierarchy items and their details)."""
        id_map = {self._to_change_state_id(item_id): item_id for item_id in item_object_ids}
        change_state_ids = list(id_map.keys())
        details = {}
        chunk_size = 20

        for i in range(0, len(change_state_ids), chunk_size):
            chunk = change_state_ids[i:i + chunk_size]
            params = {
                "$filter": " or ".join([f"ObjectID eq '{cid}'" for cid in chunk]),
                "$expand": "ProductionBillOfMaterialAssignedVariant",
                "$format": "json",
            }
            data = self._get(self.details_url, params)
            for change_state in data.get("d", {}).get("results", []):
                item_id = id_map.get(change_state.get("ObjectID"))
                if not item_id:
                    continue
                variants = change_state.get("ProductionBillOfMaterialAssignedVariant", [])
                variant = variants[0] if variants else {}

                details[item_id] = {
                    "eco_id": change_state.get("EngineeringChangeOrderID"),
                    "quantity": variant.get("Quantity"),
                    "unit_of_measure": variant.get("unitCode"),
                    "material_uuid": variant.get("MaterialUUID"),
                    "material_id": variant.get("InternalID") or variant.get("MaterialUUID"),
                    "quantity_fixed": change_state.get("QuantityFixedIndicator"),
                    "deleted": change_state.get("DeletedIndicator"),
                }

        return details

    def search_bom(self, bom_id: str):
        hierarchy = self.get_bom_hierarchy(bom_id)
        if hierarchy is None:
            return None

        all_item_ids = [item_id for group in hierarchy["groups"] for item_id in group["item_ids"]]
        details = self.get_component_details_by_ids(all_item_ids)

        result = {
            "bom_id": hierarchy["bom_id"],
            "object_id": hierarchy["object_id"],
            "groups": [],
            "total_components": 0,
        }

        for group in hierarchy["groups"]:
            group_record = {
                "group_id": group["group_id"],
                "group_number": group["group_number"],
                "start_date": group["start_date"],
                "end_date": group["end_date"],
                "components": [],
            }

            for line_index, item_id in enumerate(group["item_ids"]):
                detail = details.get(item_id)
                if not detail:
                    logger.warning(f"No component detail found for item {item_id}")
                    continue
                is_active = not detail["deleted"]
                group_record["components"].append({
                    "line_item": (line_index + 1) * 10,
                    "material_id": detail["material_id"],
                    "quantity": float(detail["quantity"]) if detail["quantity"] else None,
                    "unit_of_measure": detail["unit_of_measure"],
                    "eco_id": detail["eco_id"],
                    "quantity_fixed": bool(detail["quantity_fixed"]),
                    "active": is_active,
                })
                result["total_components"] += 1

            result["groups"].append(group_record)

        return result
