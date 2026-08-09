"""Standalone validation script for the new 2-tier MRP II cascade
(Sales Plan/AMS -> Production Plan [locked] -> MRP) - run directly with
python3, no HTTP/UI involved, per the user's request to validate the
calculation engine before any page is built.

Usage: cd /app/backend && python3 tests/validate_mrp_cascade.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from pymongo import MongoClient

from oms_client import OMSClient
from open_po_client import OpenPODemandClient
from forecast_demand_client import ForecastDemandClient
from sap_soap_client import SAPSoapBOMClient
import demand_planning_service
import mps_service
import mrp_service

client = MongoClient(os.environ["MONGO_URL"], tz_aware=True)
db = client[os.environ["DB_NAME"]]

oms_client = OMSClient(
    base_url=os.environ["OMS_BASE_URL"], username=os.environ["OMS_USERNAME"], password=os.environ["OMS_PASSWORD"],
)
open_po_client = OpenPODemandClient(os.environ["OPEN_PO_DEMAND_BASE_URL"], os.environ["OPEN_PO_DEMAND_API_KEY"])
forecast_demand_client = ForecastDemandClient(os.environ["FORECAST_DEMAND_BASE_URL"], os.environ["FORECAST_DEMAND_API_KEY"])
sap_soap_client = SAPSoapBOMClient(
    endpoint=os.environ["SAP_SOAP_ENDPOINT"], username=os.environ["SAP_SOAP_USERNAME"], password=os.environ["SAP_SOAP_PASSWORD"],
)

print("=" * 70)
print("STEP 0: Forecast Demand feed signal (new, Session 17 follow-up)")
print("=" * 70)
forecast_signal = demand_planning_service.get_forecast_signal(forecast_demand_client)
print(f"Forecast signal covers {len(forecast_signal)} item(s) right now (feed is brand new, may be empty/sparse).")
for oms_code, data in list(forecast_signal.items())[:5]:
    print(f"  {oms_code:20s} monthly_qty={data['monthly_qty']:>10.2f}  sources={data['sources']}  {data['description']}")

print()
print("=" * 70)
print("STEP 1: AMS (Average Monthly Sales) - Sales Plan layer")
print("=" * 70)
t0 = time.time()
ams_map = demand_planning_service.get_average_monthly_sales(oms_client)
print(f"Computed AMS for {len(ams_map)} finished goods in {time.time() - t0:.1f}s")
top5 = sorted(ams_map.items(), key=lambda kv: -kv[1]["ams"])[:5]
for part_no, data in top5:
    print(f"  {part_no:20s} AMS={data['ams']:>10.2f}/mo   {data['description']}")

print()
print("Combined get_demand_signal() (forecast overrides AMS per-item where available):")
combined = demand_planning_service.get_demand_signal(oms_client, forecast_demand_client)
forecast_sourced = [k for k, v in combined.items() if v.get("source") == "forecast"]
print(f"  {len(combined)} total items, {len(forecast_sourced)} sourced from forecast, {len(combined) - len(forecast_sourced)} from trailing-average AMS")

print()
print("=" * 70)
print("STEP 2: Production Plan draft (Tier 1 - MPS)")
print("=" * 70)
t0 = time.time()
draft = mps_service.build_production_plan(open_po_client, oms_client, db, forecast_demand_client=forecast_demand_client)
print(f"Built draft in {time.time() - t0:.1f}s: {len(draft['fgs'])} FG(s) with a net requirement "
      f"(out of {draft['total_selected_po_lines']} selected PO lines / {draft['total_open_po_lines']} total open PO lines)")
for fg in draft["fgs"][:5]:
    print(f"  {fg['item_code']:20s} AMS={fg['ams']:>8.2f}  on_hand={fg['on_hand_qty']}  "
          f"gross={fg['total_gross_qty']:>8.2f}  NET={fg['total_net_qty']:>8.2f}  lines={len(fg['demand_lines'])}")
    for line in fg["demand_lines"][:2]:
        print(f"      PO {line['internal_pono']} / {line['customer_po']} ({line['customer']}): "
              f"ship={line['target_ship_date']} lead_day={line['lead_day']} "
              f"prod_start={line['production_start_date']} qty_open={line['qty_open']} net={line['net_qty']}")

print()
print("=" * 70)
print("STEP 3: Lock the Production Plan")
print("=" * 70)
locked = mps_service.lock_production_plan(db, draft, locked_by="validation-script")
print(f"Locked plan _id={locked['_id']} at {locked['locked_at'].isoformat()} with {len(locked['fgs'])} FG(s)")

latest = mps_service.get_latest_locked_plan(db)
assert latest and str(latest["_id"]) == str(locked["_id"]), "get_latest_locked_plan did not return the plan we just locked!"
print("Verified get_latest_locked_plan() returns this exact lock.")

print()
print("=" * 70)
print("STEP 4: MRP (Tier 2) - explode the LOCKED plan through BOM")
print("=" * 70)
t0 = time.time()
mrp = mrp_service.build_mrp_plan(sap_soap_client, db)
print(f"Built MRP plan in {time.time() - t0:.1f}s: {len(mrp['components'])} component(s) with a net requirement, "
      f"{len(mrp['unresolved_items'])} unresolved item(s)")
print(f"  locked_plan_id={mrp['locked_plan_id']}  locked_at={mrp['locked_at']}")
for c in mrp["components"][:8]:
    print(f"  {c['product_id']:20s} lead_time={c['lead_time_days']}"
          f"{'(default)' if c['lead_time_is_default'] else '(on file)'}  "
          f"dynamic_msl={c['dynamic_msl']}  on_hand={c['on_hand_qty']}  "
          f"gross={c['total_gross_qty']:>8.2f}  NET={c['total_net_qty']:>8.2f}  lines={len(c['demand_lines'])}")
    for line in c["demand_lines"][:2]:
        print(f"      FG {line['item_code']} PO {line['internal_pono']}: need_by={line['need_by_date']} "
              f"order_by={line['order_by_date']} gross={line['gross_qty']} net={line['net_qty']}")

if mrp["unresolved_items"]:
    print("\n  Unresolved item_codes (couldn't resolve to a SAP BOM):")
    for u in mrp["unresolved_items"][:10]:
        print(f"    {u['item_code']}: {u.get('reason')}")

print()
print("ALL STEPS COMPLETED WITHOUT ERROR.")
