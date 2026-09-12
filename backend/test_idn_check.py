"""Standalone, risk-free test of CheckMaintainBundle against test PO
29482 (per user's explicit request) - validates payload shape with SAP
WITHOUT committing anything. Run manually: python3 test_idn_check.py"""
import os
import sys
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

from sap_inbound_delivery_notification_client import SAPInboundDeliveryNotificationClient  # noqa: E402

client = SAPInboundDeliveryNotificationClient(
    endpoint=os.environ["SAP_SOAP_INBOUND_DELIVERY_NOTIFICATION_ENDPOINT"],
    username=os.environ["SAP_SOAP_USERNAME"],
    password=os.environ["SAP_SOAP_PASSWORD"],
)

result = client.check_maintain_bundle(
    notification_id="TESTIDN29482",
    po_number="29482",
    vendor_id="RAD-P2-S",
    delivery_date="2026-09-13",
    items=[
        {"item_number": "1", "unit_of_measure": "EA", "quantity": 100, "product_id": "WAS8PZ"},
        {"item_number": "2", "unit_of_measure": "EA", "quantity": 200, "product_id": "WAS8PZ"},
        {"item_number": "3", "unit_of_measure": "EA", "quantity": 300, "product_id": "WAS8PZ"},
        {"item_number": "4", "unit_of_measure": "EA", "quantity": 400, "product_id": "WAS8PZ"},
        {"item_number": "5", "unit_of_measure": "EA", "quantity": 500, "product_id": "WAS8PZ"},
    ],
)

print("DELIVERY NOTIFICATION ID (echoed):", result["delivery_notification_id"])
print("UUID:", result["uuid"])
print("SEVERITIES:", result["severities"])
print("NOTES:", result["notes"])
print("HAS ERROR:", result["has_error"])
print("\n--- RAW XML (first 5000 chars) ---")
print(result["raw_xml"])
