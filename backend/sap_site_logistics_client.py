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
      <SiteLogisticsTaskSelectionByElements>
        <SelectionBySiteID>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
          <LowerBoundarySiteID>{escape(site_id)}</LowerBoundarySiteID>
          <UpperBoundarySiteID>{escape(site_id)}</UpperBoundarySiteID>
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
                "referenced_object_uuid": _first_tag(block, "ReferencedObjectUUID"),
                "operation_activity_uuid": _first_tag(block, "SiteLogisticsLotOperationActivityUUID"),
            })
        return results


class SAPSiteLogisticsManageClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def confirm_tasks_bundle(self, tasks: list) -> str:
        """MaintainBundle_V1 - `tasks`: list of dicts, each:
          {task_uuid, referenced_object_uuid, operation_activity_uuid,
           product_id, quantity, unit_code, source_warehouse_id,
           target_warehouse_id}
        ALL tasks are sent in ONE request - the whole point (see module
        docstring): this is the API equivalent of multi-selecting several
        rows in the SAP UI and confirming them together in one shot.
        Returns the raw response XML (caller checks for per-task logs)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        task_blocks = []
        for t in tasks:
            task_blocks.append(f"""
      <SiteLogisticsTask>
        <SiteLogisticTaskUUID>{escape(t["task_uuid"])}</SiteLogisticTaskUUID>
        <ActualExecutionOn>{now}</ActualExecutionOn>
        <ReferenceObject>
          <ReferenceObjectUUID>{escape(t["referenced_object_uuid"])}</ReferenceObjectUUID>
          <OperationActivity>
            <OperationActivityUUID>{escape(t["operation_activity_uuid"])}</OperationActivityUUID>
            <MaterialOutput>
              <ProductID>{escape(t["product_id"])}</ProductID>
              <SourceLogisticsAreaIDPostSplit>{escape(t.get("source_warehouse_id") or "")}</SourceLogisticsAreaIDPostSplit>
              <TargetLogisticsAreaID>{escape(t.get("target_warehouse_id") or "")}</TargetLogisticsAreaID>
              <ActualQuantity unitCode="{escape(t.get("unit_code") or "EA")}">{_format_qty(t["quantity"])}</ActualQuantity>
            </MaterialOutput>
          </OperationActivity>
        </ReferenceObject>
      </SiteLogisticsTask>""")
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:SiteLogisticsTaskBundleMaintainRequest_sync_V1 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">{"".join(task_blocks)}
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
        severities = re.findall(r"<SiteLogisticsTaskSeverityCode>(.*?)</SiteLogisticsTaskSeverityCode>", xml, re.S)
        if any(s.strip() in ("2", "3") for s in severities):  # 2=Error, 3=Cancellation typical SAP severity codes
            notes = [n.strip() for n in re.findall(r"<SiteLogisticsTaskNote>(.*?)</SiteLogisticsTaskNote>", xml, re.S) if n.strip()]
            raise SAPSiteLogisticsError("; ".join(notes) or "SAP reported an error confirming one or more tasks.")
        return xml
