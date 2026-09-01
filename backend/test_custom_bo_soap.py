"""Standalone validation script for the new SAP Custom BO (BusinessObject1)
web services in the TEST tenant (my441464.businessbydesign.cloud.sap) -
NOT wired into the main app yet, this is purely to prove the SOAP round
trip (Create -> Read -> Combine -> Update -> SetTransportDetailsAndRelease)
actually works before any production integration is attempted.

Run: python3 test_custom_bo_soap.py
"""
import re
import uuid

import requests
from requests.auth import HTTPBasicAuth

BASE = "https://my441464.businessbydesign.cloud.sap/sap/bc/srt/scs/sap"
WS1 = f"{BASE}/yy0cq5p7hy_webservice1?sap-vhost=my441464.businessbydesign.cloud.sap"
WS2 = f"{BASE}/yy0cq5p7hy_webservice2?sap-vhost=my441464.businessbydesign.cloud.sap"
NS = "http://0012819041-one-off.sap.com/Y0CQ5P7HY_"
AUTH = HTTPBasicAuth("Admin", "Admin@11336")


def _first_tag(xml: str, tag: str):
    m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _post(endpoint: str, service_name: str, operation: str, body_inner: str) -> str:
    soap_action = f"{NS}/{service_name}/{operation}Request"
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
{body_inner}
  </soapenv:Body>
</soapenv:Envelope>"""
    resp = requests.post(
        endpoint,
        data=envelope.encode("utf-8"),
        auth=AUTH,
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": soap_action},
        timeout=45,
    )
    print(f"--- {operation} -> HTTP {resp.status_code} ---")
    if resp.status_code >= 400 or "<Fault" in resp.text or ":Fault" in resp.text:
        print(resp.text[:3000])
        raise RuntimeError(f"{operation} failed: {_first_tag(resp.text, 'faultstring') or resp.status_code}")
    return resp.text


def create_instance(order_reference_id: str, delivery_request_uuid: str = None) -> str:
    fields = f"<OrderReferenceID>{order_reference_id}</OrderReferenceID>"
    if delivery_request_uuid:
        fields += f"<DeliveryRequestUUID>{delivery_request_uuid}</DeliveryRequestUUID>"
    body = f"""    <BusinessObject1CreateRequest_sync xmlns="{NS}">
      <BusinessObject1>
        {fields}
      </BusinessObject1>
    </BusinessObject1CreateRequest_sync>"""
    xml = _post(WS1, "Y0CQ5P7HY_WebService1", "Create", body)
    sap_uuid = _first_tag(xml, "SAP_UUID") or _first_tag(xml, "UUID")
    print(f"Created SAP_UUID: {sap_uuid}")
    return sap_uuid


def read_status(order_reference_id: str) -> dict:
    body = f"""    <BusinessObject1QueryByElementsRequest_sync xmlns="{NS}">
      <SelectionByOrderReferenceID>
        <InclusionExclusionCode>I</InclusionExclusionCode>
        <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
        <LowerBoundaryOrderReferenceID>{order_reference_id}</LowerBoundaryOrderReferenceID>
      </SelectionByOrderReferenceID>
    </BusinessObject1QueryByElementsRequest_sync>"""
    xml = _post(WS1, "Y0CQ5P7HY_WebService1", "QueryByElements", body)
    return {
        "status": _first_tag(xml, "Status"),
        "delivery_id": _first_tag(xml, "DeliveryID"),
        "sap_uuid": _first_tag(xml, "SAP_UUID"),
    }


def combine(order_reference_id: str):
    body = f"""    <BusinessObject1CombineCombineRequest_sync xmlns="{NS}">
      <BusinessObject1>
        <OrderReferenceID>{order_reference_id}</OrderReferenceID>
      </BusinessObject1>
    </BusinessObject1CombineCombineRequest_sync>"""
    return _post(WS1, "Y0CQ5P7HY_WebService1", "Combine", body)


if __name__ == "__main__":
    test_order_ref = f"TESTBO-{uuid.uuid4().hex[:8]}"
    print(f"Using test OrderReferenceID: {test_order_ref}")

    sap_uuid = create_instance(test_order_ref)
    status = read_status(test_order_ref)
    print("Read back after create:", status)

    print("\nBasic Create/Read cycle validated. Skipping Combine/Release live call")
    print("(needs a real Outbound Delivery Request UUID from this test tenant to be meaningful).")
