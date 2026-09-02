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

            # Aug 25 2026 fix (real incident, lot 70539): the ReportingPoint's
            # own ConfirmationFinishedIndicator is FROZEN once set true - SAP
            # docs confirm it "cannot be unchecked once set", even after a
            # successful Restart Task action. ProcessingStatusCode on the
            # TASK itself is the field that actually reflects a restart
            # (3=truly Finished, 2=back In Process) - this is what the
            # Confirm button must gate on, not the frozen RP flag.
            task_status_block = _first_block(first_task, "Status") if first_task else None
            task_processing_status_code = _first_tag(task_status_block, "ProcessingStatusCode") if task_status_block else None

            # Aug 25 2026, user's explicit ask: show the real operation
            # name (e.g. "Blanking"/"Bending"/"Forming") instead of a bare
            # Reporting Point code like "RP10" - each ConfirmationGroup's
            # Activity block carries this description already.
            activity_blocks = _all_blocks(group_block, "Activity")
            operation_description = _first_tag(activity_blocks[0], "ActivityDescription") if activity_blocks else None

            # Output Products grid (main output + any by-products already
            # planned on this lot, e.g. IRON-SCR) - each carries its own
            # MaterialOutputUUID, needed to confirm ITS quantity separately
            # via confirm_material_output (ActionCode 02, cannot be combined
            # with the ReportingPoint call in the same request).
            material_outputs = [{
                "product_id": _first_tag(mo_block, "ProductID"),
                "material_output_uuid": _first_tag(mo_block, "MaterialOutputUUID"),
                "unit_code": _first_tag_attr(mo_block, "PlannedQuantity", "unitCode") or _first_tag_attr(mo_block, "OpenQuantity", "unitCode"),
                "planned_quantity": _to_float(_first_tag(mo_block, "PlannedQuantity")),
                "total_confirmed_quantity": _to_float(_first_tag(mo_block, "TotalConfirmedQuantity")),
                # Aug 25 2026 fix: same frozen-field issue as the
                # ReportingPoint's own OpenQuantity - recompute from
                # planned minus confirmed rather than trusting SAP's field.
                "open_quantity": max(
                    (_to_float(_first_tag(mo_block, "PlannedQuantity")) or 0)
                    - (_to_float(_first_tag(mo_block, "TotalConfirmedQuantity")) or 0),
                    0,
                ),
                # Needed to CREATE a brand-new by-product line (ActionCode
                # 01) when one wasn't planned at all - reuse the main
                # output's own target area as a sensible default, since a
                # by-product almost always shares the main output's site.
                "target_logistics_area_id": _first_tag(mo_block, "TargetLogisticsAreaID"),
            } for mo_block in _all_blocks(group_block, "MaterialOutput")]

            # Exact planned components for THIS lot's own ConfirmationGroup
            # (Aug 2026 fix - see production_confirmation_service.
            # check_component_availability_from_material_inputs): unlike
            # guessing a BOM by ProductID alone (ambiguous when SAP has
            # multiple active Production Models for the same product,
            # e.g. BK-0021 has both FLAT-BK21 and SH4.5HR-based models
            # released for different sites), SAP's own MaterialInput block
            # here already carries the EXACT components this specific lot
            # was planned against - no guessing needed at all.
            material_inputs_raw = [{
                "product_id": _first_tag(mi_block, "ProductID"),
                "unit_code": _first_tag_attr(mi_block, "PlannedQuantity", "unitCode"),
                "planned_quantity": _to_float(_first_tag(mi_block, "PlannedQuantity")),
                "total_confirmed_quantity": _to_float(_first_tag(mi_block, "TotalConfirmedQuantity")),
                "source_logistics_area_id": _first_tag(mi_block, "SourceLogisticsAreaID"),
            } for mi_block in _all_blocks(group_block, "MaterialInput")]

            for rp_block in _all_blocks(group_block, "ReportingPoint"):
                unit_code = (
                    _first_tag_attr(rp_block, "PlannedQuantity", "unitCode")
                    or _first_tag_attr(rp_block, "OpenQuantity", "unitCode")
                    or _first_tag_attr(rp_block, "TotalConfirmedQuantity", "unitCode")
                )
                finished_raw = _first_tag(rp_block, "ConfirmationFinishedIndicator")
                rp_planned_quantity = _to_float(_first_tag(rp_block, "PlannedQuantity"))
                # Scales each component's total planned quantity (planned
                # against the WHOLE lot's output) down to a per-output-unit
                # ratio, the same shape the old BOM-based check already
                # used (item["quantity"] * confirmed_quantity) - so this
                # Reporting Point's own PlannedQuantity is the correct
                # denominator, not the confirmed_quantity being entered now.
                material_inputs = [{
                    **mi,
                    "qty_per_unit": round(mi["planned_quantity"] / rp_planned_quantity, 6)
                    if mi.get("planned_quantity") is not None and rp_planned_quantity else None,
                } for mi in material_inputs_raw]
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
                    "operation_description": operation_description,
                    "task_finished": task_processing_status_code == "3",
                    "unit_code": unit_code,
                    "planned_quantity": _to_float(_first_tag(rp_block, "PlannedQuantity")),
                    "total_confirmed_quantity": _to_float(_first_tag(rp_block, "TotalConfirmedQuantity")),
                    "total_confirmed_scrap": _to_float(_first_tag(rp_block, "TotalConfirmedScrap")),
                    # Aug 25 2026 fix: SAP's own OpenQuantity field freezes at
                    # 0 once ConfirmationFinishedIndicator was ever set true,
                    # even after the task is restarted (lot 70539 incident) -
                    # planned minus confirmed is always the true remaining
                    # amount, restarted or not, so compute it directly
                    # instead of trusting SAP's stale field.
                    "open_quantity": max(
                        (_to_float(_first_tag(rp_block, "PlannedQuantity")) or 0)
                        - (_to_float(_first_tag(rp_block, "TotalConfirmedQuantity")) or 0),
                        0,
                    ),
                    "confirmation_finished": finished_raw == "true",
                    "material_outputs": material_outputs,
                    "material_inputs": material_inputs,
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

        logs = self._parse_confirm_logs(xml)
        success = not any(l["severity"] == "E" for l in logs)

        # Per SAP docs, marking a Reporting Point's ConfirmationFinishedIndicator only
        # closes that checkpoint - it does NOT transition the underlying Production
        # Task's own life cycle status (shown as In Process/Finished in SAP's Task
        # Control UI). That requires a SEPARATE, standalone "Finish Task" request
        # (ProductionTask node only - no ReportingPoint/Material nodes allowed in it).
        # Sep 2 2026 BUG FOUND + FIXED (live incident, Lot 71425 -
        # "output item posted but Task status still open in SAP and
        # Emergent"): this standalone finish_task() call used to be
        # allowed to raise straight out of confirm_reporting_point() on
        # any SAP fault - discarding the fact that the ReportingPoint
        # update just above had ALREADY succeeded (a real, irreversible
        # SAP state change - ConfirmationFinishedIndicator can never be
        # unset). The caller then reported total failure and never
        # logged anything (see _confirm_production_inner's exception
        # handlers), so the user retried the WHOLE confirmation -
        # which SAP now rejects outright ("Changes in task or lot not
        # permitted; confirmation complete indicator set") since the
        # ReportingPoint fields can't be resent. Now caught here so the
        # partial success (RP done, Task still open) is preserved and
        # reported accurately instead of silently lost.
        if confirmation_finished and success and (production_task_id or production_task_uuid):
            try:
                task_result = self.finish_task(
                    production_lot_id=production_lot_id, production_lot_uuid=production_lot_uuid,
                    confirmation_group_uuid=confirmation_group_uuid,
                    production_task_id=production_task_id, production_task_uuid=production_task_uuid,
                )
                logs.extend(task_result["logs"])
                success = success and task_result["success"]
            except SAPProductionLotError as e:
                logs.append({"node_name": "PRODUCTION_LOT->PRODUCTION_TASK_ROOT", "severity": "E", "note": f"Output was posted to SAP successfully, but closing the Task itself failed: {e}. Do NOT re-enter the quantity - retry with 'Finish Task' only."})
                success = False

        return {"success": success, "logs": logs}

    def finish_task(
        self, production_lot_id: str, production_lot_uuid: str, confirmation_group_uuid: str,
        production_task_id: str = None, production_task_uuid: str = None, processor_employee_id: str = None,
    ) -> dict:
        """Standalone 'Finish Task' action - transitions the Production Task's own
        life cycle status (SAP Task Control: In Process -> Finished). Per SAP docs
        this request must contain ONLY the ProductionTask node (no ReportingPoint,
        MaterialInput/Output, or Activity/Resource nodes)."""
        from datetime import datetime, timezone
        execution_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        processor_xml = f"<ProcessorEmployeeID>{processor_employee_id}</ProcessorEmployeeID>" if processor_employee_id else ""
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
     <ProductionTask>
      <ProductionTaskID>{production_task_id or ''}</ProductionTaskID>
      <ProducionTaskUUID>{production_task_uuid or ''}</ProducionTaskUUID>
      {processor_xml}
      <ExecutionDateTime>{execution_dt}</ExecutionDateTime>
      <ConfirmationCompletedRequiredIndicator>true</ConfirmationCompletedRequiredIndicator>
     </ProductionTask>
    </ConfirmationGroup>
   </ProductionLot>
  </n0:ProductionLotsBundleMaintainRequest_sync_V1>
 </soapenv:Body>
</soapenv:Envelope>"""
        xml = self._post(self.manage_endpoint, body, MANAGE_SOAP_ACTION)
        logs = self._parse_confirm_logs(xml)
        success = not any(l["severity"] == "E" for l in logs)
        return {"success": success, "logs": logs}

    def restart_task(
        self, production_lot_id: str, production_lot_uuid: str, confirmation_group_uuid: str,
        production_task_id: str = None, production_task_uuid: str = None, processor_employee_id: str = None,
    ) -> dict:
        """Standalone 'Restart Task' action - the ONLY way to reopen a task
        that was already marked Finished (SAP explicitly rejects re-sending
        ReportingPoint's ConfirmationFinishedIndicator=false - "cannot be
        unchecked once set" per SAP's own docs). Recovery path for the Aug
        25 2026 incident: a task closed at a partial quantity (e.g. 9 of
        18) because the confirming request wrongly included
        ConfirmationFinishedIndicator=true. Per SAP docs, this request must
        contain ONLY the ProductionTask node."""
        from datetime import datetime, timezone
        execution_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        processor_xml = f"<ProcessorEmployeeID>{processor_employee_id}</ProcessorEmployeeID>" if processor_employee_id else ""
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
     <ProductionTask>
      <ProductionTaskID>{production_task_id or ''}</ProductionTaskID>
      <ProducionTaskUUID>{production_task_uuid or ''}</ProducionTaskUUID>
      {processor_xml}
      <ExecutionDateTime>{execution_dt}</ExecutionDateTime>
      <RestartOfTaskIndicator>true</RestartOfTaskIndicator>
     </ProductionTask>
    </ConfirmationGroup>
   </ProductionLot>
  </n0:ProductionLotsBundleMaintainRequest_sync_V1>
 </soapenv:Body>
</soapenv:Envelope>"""
        xml = self._post(self.manage_endpoint, body, MANAGE_SOAP_ACTION)
        logs = self._parse_confirm_logs(xml)
        success = not any(l["severity"] == "E" for l in logs)
        return {"success": success, "logs": logs}

    def confirm_material_output(
        self, production_lot_id: str, production_lot_uuid: str, confirmation_group_uuid: str,
        material_output_uuid: str, confirmed_quantity: float, unit_code: str,
    ) -> dict:
        """Confirms the quantity of an EXISTING, already-planned Output
        Products grid line (e.g. a by-product like IRON-SCR that's already
        on the lot from its Production Model) - ActionCode="02" (Change),
        referencing the existing MaterialOutputUUID. Per SAP docs this must
        be its own isolated request: a MaterialOutput node cannot be
        combined with a ReportingPoint node in the same call (same
        constraint as finish_task's ProductionTask-only isolation above)."""
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
     <MaterialOutput ActionCode="02">
      <MaterialOutputUUID>{material_output_uuid}</MaterialOutputUUID>
      <ConfirmedQuantity unitCode="{unit_code or ''}">{confirmed_quantity}</ConfirmedQuantity>
     </MaterialOutput>
    </ConfirmationGroup>
   </ProductionLot>
  </n0:ProductionLotsBundleMaintainRequest_sync_V1>
 </soapenv:Body>
</soapenv:Envelope>"""
        xml = self._post(self.manage_endpoint, body, MANAGE_SOAP_ACTION)
        logs = self._parse_confirm_logs(xml)
        success = not any(l["severity"] == "E" for l in logs)
        return {"success": success, "logs": logs}

    def create_material_output(
        self, production_lot_id: str, production_lot_uuid: str, confirmation_group_uuid: str,
        product_id: str, target_logistics_area_id: str, confirmed_quantity: float, unit_code: str,
    ) -> dict:
        """Adds a brand-new by-product output line that was NEVER planned
        on this lot at all (its Production Model has no such output row) -
        ActionCode="01" (Create), per SAP's own documented example ("Add
        new by-product under MaterialOutput"): just ProductID +
        TargetLogisticsAreaID + ConfirmedQuantity, no MaterialOutputUUID
        needed since SAP generates one. Same isolation constraint as
        confirm_material_output - must run BEFORE finish_task, and cannot
        be combined with a ReportingPoint node in the same request."""
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
     <MaterialOutput ActionCode="01">
      <ProductID>{product_id}</ProductID>
      <TargetLogisticsAreaID>{target_logistics_area_id}</TargetLogisticsAreaID>
      <ConfirmedQuantity unitCode="{unit_code or ''}">{confirmed_quantity}</ConfirmedQuantity>
     </MaterialOutput>
    </ConfirmationGroup>
   </ProductionLot>
  </n0:ProductionLotsBundleMaintainRequest_sync_V1>
 </soapenv:Body>
</soapenv:Envelope>"""
        xml = self._post(self.manage_endpoint, body, MANAGE_SOAP_ACTION)
        logs = self._parse_confirm_logs(xml)
        success = not any(l["severity"] == "E" for l in logs)
        return {"success": success, "logs": logs}

    @staticmethod
    def _parse_confirm_logs(xml: str) -> list:
        logs = []
        lot_response = _first_block(xml, "ProductionLotResponse")
        if lot_response:
            for log_block in _all_blocks(lot_response, "ProductionLotLog"):
                logs.append({
                    "node_name": _first_tag(log_block, "NodeName"),
                    "severity": _first_tag(log_block, "SeverityCode"),
                    "note": _first_tag(log_block, "Note"),
                })
        return logs
