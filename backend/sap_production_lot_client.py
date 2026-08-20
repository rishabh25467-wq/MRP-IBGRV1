"""SAP Business ByDesign Production Lot Confirmation - two related SOAP
services on the same tenant:

- QueryProductionLotISIIn / FindByElements: lists production lots (with
  their Confirmation Groups / Production Tasks / Reporting Points) so the
  factory floor can see what's open to confirm.
- ManageProductionLotsIn / MaintainBundle_V1: posts a confirmation
  (Confirmed Quantity, Confirmed Scrap, Deviation Reason, Finished
  indicator) against a single Reporting Point.

Per the user's explicit requirement, this client NEVER sends MaterialInput
(component) quantities - SAP's own backflush auto-consumes BOM components
from the confirmed output quantity. Only ReportingPoint-level fields are
ever written.

Both operations were confirmed live against real WSDLs (uploaded by the
user, endpoints below extracted directly from them) and cross-checked
against SAP's official help.sap.com documentation for exact request/
response shape and the standard Deviation Reason Code list."""
import html
import re

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

QUERY_SOAP_ACTION = "http://sap.com/xi/A1S/Global/QueryProductionLotISIIn/FindByElementsRequest"
MANAGE_SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageProductionLotsIn/MaintainBundle_V1Request"

# Production Lot Life Cycle Status Code (per SAP help.sap.com PSM_ISI_R_II_QUERY_PROD_LOT_IN)
LOT_STATUS_LABELS = {
    "1": "In Preparation", "2": "Released", "3": "Started",
    "4": "Finished", "5": "Closed", "6": "Cancelled",
}
# Statuses considered "open for confirmation" by default (Released/Started)
DEFAULT_OPEN_STATUS_CODES = ["2", "3"]


class SAPProductionLotError(Exception):
    pass


class SAPProductionLotAuthError(SAPProductionLotError):
    """Raised when SAP rejects the call for lacking an authorization role
    for this Communication Scenario - distinct from a transient/data error
    so the caller can surface clear SAP-admin activation steps instead of
    a generic failure."""
    pass


def _tag_re(tag: str):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _first_tag_attr(xml: str, tag: str, attr: str):
    m = re.search(rf"<(?:\w+:)?{tag}\s([^>]*)>", xml)
    if not m:
        return None
    am = re.search(rf'{attr}="([^"]*)"', m.group(1))
    return am.group(1) if am else None


def _first_block(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return m.group(1) if m else None


def _all_blocks(xml: str, tag: str):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


def _to_float(value):
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class SAPProductionLotClient:
    def __init__(self, query_endpoint: str, manage_endpoint: str, username: str, password: str):
        self.query_endpoint = query_endpoint
        self.manage_endpoint = manage_endpoint
        self.auth = HTTPBasicAuth(username, password)

    def _post(self, endpoint: str, body: str, soap_action: str) -> str:
        headers = {"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": soap_action}
        try:
            with sap_semaphore:
                resp = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, auth=self.auth, timeout=(8, 45))
        except requests.exceptions.RequestException as e:
            raise SAPProductionLotError(str(e))
        xml = resp.text
        if resp.status_code == 401:
            raise SAPProductionLotError("SAP SOAP authentication failed. Check communication arrangement credentials.")
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml or "StandardFaultMessage" in xml:
            fault_text = _first_tag(xml, "faultText") or _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            details = [d for d in (_first_tag(b, "text") for b in _all_blocks(xml, "faultDetail")) if d]
            full_text = html.unescape(fault_text + ((" | " + "; ".join(details)) if details else ""))
            if "Authorization role missing" in full_text:
                raise SAPProductionLotAuthError(full_text)
            raise SAPProductionLotError(full_text)
        return xml

    # ------------------------------------------------------------------
    # Query (FindByElements)
    # ------------------------------------------------------------------
    def _query(self, selection_xml: str, max_hits: int) -> str:
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
 <soapenv:Body>
  <n0:ProductionLotByElementsQuery_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
   <ProductionLotSelectionByElements>
    {selection_xml}
   </ProductionLotSelectionByElements>
   <ProcessingConditions>
    <QueryHitsMaximumNumberValue>{max_hits}</QueryHitsMaximumNumberValue>
    <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
   </ProcessingConditions>
  </n0:ProductionLotByElementsQuery_sync>
 </soapenv:Body>
</soapenv:Envelope>"""
        return self._post(self.query_endpoint, body, QUERY_SOAP_ACTION)

    @staticmethod
    def _parse_lot_block(lot_block: str) -> list:
        """Returns a list of flattened, UI-ready 'confirmable reporting
        point' rows for ONE <ProductionLot> block - each row already
        carries every ID/UUID needed to submit a confirmation directly,
        so the frontend never has to understand the nested SAP structure."""
        production_lot_id = _first_tag(lot_block, "ProductionLotID")
        production_lot_uuid = _first_tag(lot_block, "ProductionLotUUID")
        production_order_id = _first_tag(lot_block, "ProductionOrderID")
        main_output_product = _first_tag(lot_block, "MainOutputProduct")
        site_id = _first_tag(lot_block, "MainOutputProductSiteID")
        status_block = _first_block(lot_block, "ProductionLotStatus")
        life_cycle_status_code = _first_tag(status_block, "Life_Cycle_Status_Code") if status_block else None

        rows = []
        for group_block in _all_blocks(lot_block, "ConfirmationGroup"):
            group_uuid = _first_tag(group_block, "ConfirmationGroupUUID")
            task_blocks = _all_blocks(group_block, "ProductionTask")
            first_task = task_blocks[0] if task_blocks else None
            task_id = _first_tag(first_task, "ProductionTaskID") if first_task else None
            task_uuid = _first_tag(first_task, "ProducionTaskUUID") if first_task else None

            for rp_block in _all_blocks(group_block, "ReportingPoint"):
                unit_code = (
                    _first_tag_attr(rp_block, "PlannedQuantity", "unitCode")
                    or _first_tag_attr(rp_block, "OpenQuantity", "unitCode")
                    or _first_tag_attr(rp_block, "TotalConfirmedQuantity", "unitCode")
                )
                finished_raw = _first_tag(rp_block, "ConfirmationFinishedIndicator")
                rows.append({
                    "production_lot_id": production_lot_id,
                    "production_lot_uuid": production_lot_uuid,
                    "production_order_id": production_order_id,
                    "main_output_product": main_output_product,
                    "site_id": site_id,
                    "life_cycle_status_code": life_cycle_status_code,
                    "life_cycle_status_label": LOT_STATUS_LABELS.get(life_cycle_status_code, life_cycle_status_code),
                    "confirmation_group_uuid": group_uuid,
                    "production_task_id": task_id,
                    "production_task_uuid": task_uuid,
                    "reporting_point_id": _first_tag(rp_block, "ReportingPointID"),
                    "reporting_point_uuid": _first_tag(rp_block, "ReportingPointUUID"),
                    "unit_code": unit_code,
                    "planned_quantity": _to_float(_first_tag(rp_block, "PlannedQuantity")),
                    "total_confirmed_quantity": _to_float(_first_tag(rp_block, "TotalConfirmedQuantity")),
                    "total_confirmed_scrap": _to_float(_first_tag(rp_block, "TotalConfirmedScrap")),
                    "open_quantity": _to_float(_first_tag(rp_block, "OpenQuantity")),
                    "confirmation_finished": finished_raw == "true",
                })
        return rows

    def find_open_lots(self, status_codes=None, site_id: str = None, limit: int = 100) -> list:
        """Lists open production lots (default: Released + Started) with
        every confirmable Reporting Point flattened into one row each."""
        status_codes = status_codes or DEFAULT_OPEN_STATUS_CODES
        selection_parts = []
        for code in status_codes:
            selection_parts.append(f"""<SelectionByProductionLotStatusCode>
              <InclusionExclusionCode>I</InclusionExclusionCode>
              <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
              <LowerBoundaryLifeCycleStatusCode>{code}</LowerBoundaryLifeCycleStatusCode>
            </SelectionByProductionLotStatusCode>""")
        if site_id:
            selection_parts.append(f"""<SelectionBySiteID>
              <InclusionExclusionCode>I</InclusionExclusionCode>
              <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
              <LowerBoundarySiteID>{site_id}</LowerBoundarySiteID>
            </SelectionBySiteID>""")
        xml = self._query("\n".join(selection_parts), max_hits=limit)
        rows = []
        for lot_block in _all_blocks(xml, "ProductionLot"):
            rows.extend(self._parse_lot_block(lot_block))
        return rows

    def find_lot_by_id(self, production_lot_id: str) -> list:
        """Manual-entry lookup - exact Production Lot ID match, regardless
        of status (so a user can pull up an already-Finished lot too)."""
        selection = f"""<SelectionByProductionLotID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundaryProductionLotID>{production_lot_id}</LowerBoundaryProductionLotID>
        </SelectionByProductionLotID>"""
        xml = self._query(selection, max_hits=5)
        rows = []
        for lot_block in _all_blocks(xml, "ProductionLot"):
            rows.extend(self._parse_lot_block(lot_block))
        return rows

    # ------------------------------------------------------------------
    # Confirm (MaintainBundle_V1)
    # ------------------------------------------------------------------
    def confirm_reporting_point(
        self, production_lot_id: str, production_lot_uuid: str, confirmation_group_uuid: str,
        reporting_point_uuid: str, unit_code: str,
        production_task_id: str = None, production_task_uuid: str = None,
        confirmed_quantity: float = None, confirmed_scrap: float = None,
        deviation_reason_code: str = None, confirmation_finished: bool = None,
    ) -> dict:
        """Posts a confirmation against ONE Reporting Point. Deliberately
        NEVER includes a MaterialInput node - SAP's backflush auto-consumes
        BOM components from the confirmed output quantity, per the user's
        explicit requirement."""
        task_xml = ""
        if production_task_id or production_task_uuid:
            task_xml = f"""<ProductionTask>
        <ProductionTaskID>{production_task_id or ''}</ProductionTaskID>
        <ProducionTaskUUID>{production_task_uuid or ''}</ProducionTaskUUID>
       </ProductionTask>"""

        rp_fields = []
        if confirmed_quantity is not None:
            rp_fields.append(f'<ConfirmedQuantity unitCode="{unit_code or ""}">{confirmed_quantity}</ConfirmedQuantity>')
        if confirmed_scrap is not None:
            rp_fields.append(f'<ConfirmedScrap unitCode="{unit_code or ""}">{confirmed_scrap}</ConfirmedScrap>')
        if deviation_reason_code:
            rp_fields.append(f"<DeviationReason>{deviation_reason_code}</DeviationReason>")
        if confirmation_finished is not None:
            rp_fields.append(f"<ConfirmationFinishedIndicator>{'true' if confirmation_finished else 'false'}</ConfirmationFinishedIndicator>")

        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
 <soapenv:Body>
  <n0:ProductionLotsBundleMaintainRequest_sync_V1 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
   <BasicMessageHeader/>
   <ProductionLot>
    <ProductionLotID>{production_lot_id}</ProductionLotID>
    <ProductionLotUUID>{production_lot_uuid}</ProductionLotUUID>
    <ConfirmationGroup>
     <ConfirmationGroupUUID>{confirmation_group_uuid}</ConfirmationGroupUUID>
     {task_xml}
     <ReportingPoint>
      <ReportingPointUUID>{reporting_point_uuid}</ReportingPointUUID>
      {''.join(rp_fields)}
     </ReportingPoint>
    </ConfirmationGroup>
   </ProductionLot>
  </n0:ProductionLotsBundleMaintainRequest_sync_V1>
 </soapenv:Body>
</soapenv:Envelope>"""
        xml = self._post(self.manage_endpoint, body, MANAGE_SOAP_ACTION)

        logs = []
        lot_response = _first_block(xml, "ProductionLotResponse")
        if lot_response:
            for log_block in _all_blocks(lot_response, "ProductionLotLog"):
                logs.append({
                    "node_name": _first_tag(log_block, "NodeName"),
                    "severity": _first_tag(log_block, "SeverityCode"),
                    "note": _first_tag(log_block, "Note"),
                })
        success = not any(l["severity"] == "E" for l in logs)
        return {"success": success, "logs": logs}
