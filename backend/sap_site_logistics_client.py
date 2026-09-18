"""SAP Business ByDesign "Site Logistics Task" SOAP clients (Aug 27 2026,
attempt #8 - see sap_outbound_delivery_client.py module docstring for
attempts #1-7, all confirmed dead ends purely at the OData layer).

The user's SAP Basis team set up two NEW Communication Arrangements
specifically to solve the multi-line combined-delivery problem:
  - QuerySiteLogisticsTaskIn.FindByElements (read)
  - ManageSiteLogisticsTaskIn.MaintainBundle_V1 (confirm)

The idea: SAP creates one "Site Logistics Task" per Outbound Delivery
Request line once it's due for shipping - normally confirmed one-by-one
via the SAP UI, OR all-at-once by multi-selecting several rows and
hitting one "Confirm" button (which is what actually produces ONE
combined Delivery, per the user's own screenshots). MaintainBundle_V1 is
the API equivalent of that multi-select: it accepts MULTIPLE
<SiteLogisticsTask> entries in ONE request - the goal is that
confirming every line of one STO in a SINGLE bundle call reproduces the
same one-Delivery result the UI gets.

Same raw-XML + SOAPAction-header pattern as every other SAP SOAP client
in this codebase (see sap_sto_client.py) - both WSDLs were uploaded live
by the user, schema is fixed/documented, no zeep/runtime WSDL parsing
needed.

Sep 19 2026 - REVIVED for a DIFFERENT scenario than the one declared
dead below (that finding was scoped ONLY to the outbound STO/pick-task
case at site P2/P8 - never tested for inbound). Real user-reported bug:
the EM1 "Test Full Automated GRN" flow (sap_inbound_delivery_notification_
client.py's MaintainBundle release=True) creates+releases the Inbound
Delivery Notification fine (Planned Quantity is always correct - confirmed
live via user's own SAP screenshot, PO 29685, Warehouse Request 85628),
but that release ONLY creates the downstream Warehouse Request/Order/
Delivery/"Put Away" Warehouse Task chain - it never CONFIRMS the actual
Put Away task, so "Product Fulfilled Quantity" stays 0 forever (user's
exact words: "fulfilled qty didn't post correctly, should be = planned").
Live-queried site P8 (Sep 19 2026) and found this Put Away task IS a real
SiteLogisticsTask (OperationTypeCode=11, e.g. task 69040 for PO 29685) -
same object this module already knows how to query. `find_tasks_for_site`
below now parses the full MaterialInput/MaterialOutput line-item detail
(UUIDs + PlanQuantity) needed to confirm it, and `confirm_tasks_bundle`'s
payload was corrected against SAP's own official schema (help.sap.com
PSM_ISI_R_II_MANAGE_SLT_IN) - the previous version was never live-tested
(this whole file was "dormant" per the dead-end finding) and was missing
MaterialInput entirely plus using a made-up `SourceLogisticsAreaIDPostSplit`
field that doesn't exist in the real schema. Wired into
sap_playwright_supplier_pgr_service.create_and_release_inbound_delivery_
notifications: right after a successful release, it looks up this PO's
Put Away task and confirms every line's ActualQuantity = PlanQuantity.

CONFIRMED DEAD END (Sep 18 2026, live-verified, read-only - no
MaintainBundle_V1 write ever attempted, so no live confirm risk taken):
`find_tasks_for_site` genuinely works (fixed the query schema - dropping
`UpperBoundarySiteID` and adding the `ProcessingConditions` node, both
required or SAP throws a generic unhelpful "An exception was raised"
SY530 fault instead of a clean result) and returns REAL data - but for
site P2, EVERY one of the 18 Site Logistics Tasks that exist (checked
with no other filter, i.e. every task SAP has ever created at that site)
is `OperationTypeCode=30` (a production material-issue/receipt task,
e.g. product "Wall Support"/"Arc Moving-42" with `MaterialInput`+
`MaterialOutput` nodes, dated 2023-08 through 2025-12) - NONE reference
any Outbound Delivery Request/Stock Transfer Order at all. Also queried
directly `SelectionByReferenceDocumentID` for 4 real STO SAP order IDs
(30215, 30129, 32139, 32140 - including 2 that DID successfully complete
Goods Issue) - zero hits on all 4. Conclusion: this tenant's Site
Logistics Task object is used ONLY for a specific production logistics
scenario, never for Stock Transfer/Outbound Delivery - same class of
finding sap_playwright_pgr_service.py already reached independently for
the INBOUND side (0 hits for ProcessTypeCode=1). This whole SOAP pair
stays dormant/unused for the outbound STO flow; kept only as a reference
in case a future SAP Basis configuration change ever starts routing
Stock Transfer through task-based execution.
"""
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

_QUERY_SOAP_ACTION = "http://sap.com/xi/A1S/Global/QuerySiteLogisticsTaskIn/FindByElementsRequest"
_MAINTAIN_SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageSiteLogisticsTaskIn/MaintainBundle_V1Request"


class SAPSiteLogisticsError(Exception):
    pass


def _first_tag(xml: str, tag: str):
    m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _all_blocks(xml: str, tag: str):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


def _material_nodes(block: str, tag: str, uuid_tag: str) -> list:
    """Parses each <MaterialInput>/<MaterialOutput> child of one
    SiteLogisticsTask's OperationActivity - `uuid_tag` is the QUERY
    response's own element name for that line's UUID
    (SiteLogisticsLotMaterialInputUUID/...OutputUUID), which is a
    DIFFERENT tag name than the WRITE (MaintainBundle_V1) request uses
    for the same UUID value (MaterialInputUUID/MaterialOutputUUID,
    confirmed against SAP's own official schema, help.sap.com
    PSM_ISI_R_II_MANAGE_SLT_IN) - `confirm_tasks_bundle` below re-emits
    it under the write schema's name."""
    nodes = []
    for m in _all_blocks(block, tag):
        qty_match = re.search(r'<PlanQuantity unitCode="([^"]*)">([^<]*)</PlanQuantity>', m)
        nodes.append({
            "uuid": _first_tag(m, uuid_tag),
            "product_id": _first_tag(m, "ProductID"),
            "line_item_id": _first_tag(m, "LineItemID"),
            "plan_quantity": float(qty_match.group(2)) if qty_match else None,
            "unit_code": qty_match.group(1) if qty_match else "EA",
            "target_area": _first_tag(m, "TargetLogisticsAreaID"),
        })
    return nodes


def _format_qty(qty: float) -> str:
    return format(qty, "f").rstrip("0").rstrip(".") or "0"


def _fault_message(xml: str, status_code: int) -> str:
    faultstring = _first_tag(xml, "faultText") or _first_tag(xml, "faultstring") or f"HTTP {status_code}"
    notes = [n.strip() for n in re.findall(r"<MessageNote>(.*?)</MessageNote>", xml, re.S) if n.strip()]
    return faultstring + ((" | " + "; ".join(notes)) if notes else "")


class SAPSiteLogisticsQueryClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def find_tasks_for_site(self, site_id: str) -> list:
        """FindByElements filtered by SelectionBySiteID - returns every
        Site Logistics Task at this site (open or already confirmed).
        The caller matches results by `referenced_object_uuid` against
        its own known Outbound Delivery Request Item UUIDs (see
        stock_transfer_service.py's try_post_goods_issue). Confirmed live
        (Aug 27 2026): this schema's local elements are UNqualified
        (elementFormDefault absent/"unqualified" on the A1S/Global
        schema) - only the ROOT element gets the n0: namespace prefix,
        every nested element below it must stay bare/unprefixed, same
        convention as sap_sto_client.py. Prefixing everything (the first
        attempt here) got a generic, undiagnosable SOAP 500."""
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:SiteLogisticsTaskByElementsQuery_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <ProcessingConditions>
        <QueryHitsMaximumNumberValue>500</QueryHitsMaximumNumberValue>
        <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
      </ProcessingConditions>
      <SiteLogisticsTaskSelectionByElements>
        <SelectionBySiteID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundarySiteID>{escape(site_id)}</LowerBoundarySiteID>
        </SelectionBySiteID>
      </SiteLogisticsTaskSelectionByElements>
    </n0:SiteLogisticsTaskByElementsQuery_sync>
  </soapenv:Body>
</soapenv:Envelope>"""
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    data=envelope.encode("utf-8"),
                    auth=self.auth,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": _QUERY_SOAP_ACTION},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPSiteLogisticsError(f"Could not reach SAP: {e}")
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            raise SAPSiteLogisticsError(_fault_message(xml, resp.status_code))
        results = []
        for block in _all_blocks(xml, "SiteLogisticsTask"):
            task_uuid = _first_tag(block, "SiteLogisticsTaskUUID")
            if not task_uuid:
                continue
            results.append({
                "task_id": _first_tag(block, "SiteLogisticsTaskID"),
                "task_uuid": task_uuid,
                "operation_type_code": _first_tag(block, "OperationTypeCode"),
                # Sep 19 2026: the field the inbound Put Away confirm fix
                # (module docstring) matches a task to its originating PO
                # by - confirmed live this holds the plain PO number
                # (e.g. "29685") for a Put Away task (OperationTypeCode=11).
                "po_number": _first_tag(block, "BusinessTransactionDocumentReferenceID"),
                "referenced_object_uuid": _first_tag(block, "ReferencedObjectUUID"),
                "operation_activity_uuid": _first_tag(block, "SiteLogisticsLotOperationActivityUUID"),
                "material_inputs": _material_nodes(block, "MaterialInput", "SiteLogisticsLotMaterialInputUUID"),
                "material_outputs": _material_nodes(block, "MaterialOutput", "SiteLogisticsLotMaterialOutputUUID"),
            })
        return results


class SAPSiteLogisticsManageClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def confirm_tasks_bundle(self, tasks: list) -> str:
        """MaintainBundle_V1 - `tasks`: list of dicts, each:
          {task_id, task_uuid, referenced_object_uuid, operation_activity_uuid,
           material_inputs: [{uuid, product_id, actual_quantity, unit_code}],
           material_outputs: [{uuid, product_id, actual_quantity, unit_code, target_area}]}
        `task_id` (SiteLogisticTaskID, e.g. "69040") is REQUIRED alongside
        `task_uuid` - confirmed live (Sep 19 2026): SAP rejected the whole
        request with a generic "Web service processing error" (no
        detail) when only the UUID was sent; help.sap.com's own official
        example always sends both SiteLogisticTaskID and
        SiteLogisticTaskUUID together.
        ALL tasks are sent in ONE request - the whole point (see module
        docstring): this is the API equivalent of multi-selecting several
        rows in the SAP UI and confirming them together in one shot.

        Sep 19 2026 rewrite (inbound Put Away confirm fix, see module
        docstring) - corrected against SAP's own official schema
        (help.sap.com PSM_ISI_R_II_MANAGE_SLT_IN) AND live-verified
        (real Put Away task 69040, PO 29685, site P8 - SAP returned
        SeverityCode "S"/"Saved Successfully" and the task then
        disappeared from find_tasks_for_site's open-task results,
        confirming it completed): now emits a <MaterialInput> block per
        line (was missing entirely before - every real Put Away task
        has BOTH an input and output side, one pair per PO line item),
        and MaterialOutput no longer sends the non-existent
        `SourceLogisticsAreaIDPostSplit` field (real schema has no
        source area on MaterialOutput at all, only ProductID/
        TargetLogisticsAreaID/ActualQuantity). Two more corrections found
        only by live-testing (SAP's generic "Web service processing
        error" gives zero detail on schema mismatches): (1) an empty
        `<SourceLogisticsAreaID></SourceLogisticsAreaID>` tag on
        MaterialInput must be OMITTED entirely, not sent blank - SAP
        rejects the whole bundle if present; (2) `<BasicMessageHeader/>`
        must be present (empty is fine) even though help.sap.com marks
        it optional.
        Returns the raw response XML (caller checks for per-task logs)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        task_blocks = []
        for t in tasks:
            material_input_blocks = "".join(f"""
            <MaterialInput>
              <MaterialInputUUID>{escape(mi["uuid"])}</MaterialInputUUID>
              <ProductID>{escape(mi["product_id"])}</ProductID>
              <ActualQuantity unitCode="{escape(mi.get("unit_code") or "EA")}">{_format_qty(mi["actual_quantity"])}</ActualQuantity>
            </MaterialInput>""" for mi in t.get("material_inputs", []))
            material_output_blocks = "".join(f"""
            <MaterialOutput>
              <MaterialOutputUUID>{escape(mo["uuid"])}</MaterialOutputUUID>
              <ProductID>{escape(mo["product_id"])}</ProductID>
              <TargetLogisticsAreaID>{escape(mo.get("target_area") or "")}</TargetLogisticsAreaID>
              <ActualQuantity unitCode="{escape(mo.get("unit_code") or "EA")}">{_format_qty(mo["actual_quantity"])}</ActualQuantity>
            </MaterialOutput>""" for mo in t.get("material_outputs", []))
            task_blocks.append(f"""
      <SiteLogisticsTask>
        <SiteLogisticTaskID>{escape(str(t["task_id"]))}</SiteLogisticTaskID>
        <SiteLogisticTaskUUID>{escape(t["task_uuid"])}</SiteLogisticTaskUUID>
        <ActualExecutionOn>{now}</ActualExecutionOn>
        <ReferenceObject>
          <ReferenceObjectUUID>{escape(t["referenced_object_uuid"])}</ReferenceObjectUUID>
          <OperationActivity>
            <OperationActivityUUID>{escape(t["operation_activity_uuid"])}</OperationActivityUUID>{material_input_blocks}{material_output_blocks}
          </OperationActivity>
        </ReferenceObject>
      </SiteLogisticsTask>""")
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:SiteLogisticsTaskBundleMaintainRequest_sync_V1 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <BasicMessageHeader/>{"".join(task_blocks)}
    </n0:SiteLogisticsTaskBundleMaintainRequest_sync_V1>
  </soapenv:Body>
</soapenv:Envelope>"""
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    data=envelope.encode("utf-8"),
                    auth=self.auth,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": _MAINTAIN_SOAP_ACTION},
                    timeout=60,
                )
        except requests.exceptions.RequestException as e:
            raise SAPSiteLogisticsError(f"Could not reach SAP: {e}")
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            raise SAPSiteLogisticsError(_fault_message(xml, resp.status_code))
        # Sep 19 2026 fix: live response uses single-letter severity
        # codes (confirmed: "S" = Success on the real 69040 confirm),
        # not the numeric "2"/"3" this used to check for (never actually
        # observed live before this fix - that guess was wrong).
        severities = re.findall(r"<SiteLogisticsTaskSeverityCode>(.*?)</SiteLogisticsTaskSeverityCode>", xml, re.S)
        if any(s.strip().upper() == "E" for s in severities):
            notes = [n.strip() for n in re.findall(r"<SiteLogisticsTaskNote>(.*?)</SiteLogisticsTaskNote>", xml, re.S) if n.strip()]
            raise SAPSiteLogisticsError("; ".join(notes) or "SAP reported an error confirming one or more tasks.")
        return xml
