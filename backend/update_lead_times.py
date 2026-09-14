"""One-time script: bulk push Lead Time (Days) from Item Lead Time.xlsx to
SAP's Procurement Lead Time field (every Supply Planning Area row) + local
component_master (Sep 2026, user's explicit ask).

Per user's explicit instructions:
- Process every row that has BOTH Product ID and Lead Time (Days) present;
  skip only if either is blank. The sheet's own "Has SAP Link" column is
  NOT trusted as the filter - SAP UUID resolution is done live per row
  instead (rows the sheet marks "No" still get attempted; they just
  naturally fail/skip if SAP truly has no such material).
- Always overwrite SAP's existing Lead Time with the Excel value.
- Also update local component_master.lead_time_days to match, so the
  Admin > Component Master page stays consistent going forward.

Usage:
  python update_lead_times.py --check [N]   # dry-run first N rows (resolve
                                             # UUID + read SAP planning data
                                             # only, ZERO writes)
  python update_lead_times.py --run         # LIVE write to SAP + local DB,
                                             # resumable via results file
"""
import os
import sys
import json
import argparse
import threading
from io import BytesIO
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import time

import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from pymongo import MongoClient  # noqa: E402
from sap_material_client import SAPMaterialClient, SAPMaterialError, SAPMaterialAuthError  # noqa: E402
from sap_planning_client import SAPPlanningClient, SAPPlanningError  # noqa: E402

EXCEL_URL = "https://customer-assets-m6fa6gv7.emergentagent.net/job_sap-data-sync/artifacts/qv5g6wu5_Item%20Lead%20Time.xlsx"
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "lead_time_update_results.json")

material_client = SAPMaterialClient(
    endpoint=os.environ["SAP_SOAP_MATERIAL_ENDPOINT"],
    username=os.environ["SAP_SOAP_USERNAME"],
    password=os.environ["SAP_SOAP_PASSWORD"],
)
planning_client = SAPPlanningClient(
    base_url=os.environ["SAP_PLANNING_ODATA_BASE_URL"],
    username=os.environ["SAP_ODATA_USERNAME"],
    password=os.environ["SAP_ODATA_PASSWORD"],
)
mongo = MongoClient(os.environ["MONGO_URL"])
db = mongo[os.environ["DB_NAME"]]


def load_rows():
    resp = requests.get(EXCEL_URL, timeout=60)
    resp.raise_for_status()
    df = pd.read_excel(BytesIO(resp.content))
    df = df[df["Product ID"].notna() & df["Lead Time (Days)"].notna()]
    return [
        {
            "product_id": str(r["Product ID"]).strip(),
            "description": r.get("Description"),
            "lead_time_days": float(r["Lead Time (Days)"]),
        }
        for _, r in df.iterrows()
    ]


RETRYABLE_ATTEMPTS = 3


def process_one(row):
    """Any transient network failure (SAP connect timeout/read timeout/
    DNS blip) is retried a few times before giving up on this one row -
    a single flaky call must never crash the whole 2271-row batch (real
    incident: an uncaught requests.exceptions.ConnectTimeout killed the
    first run at row 64/2271)."""
    product_id = row["product_id"]
    last_network_error = None
    for attempt in range(1, RETRYABLE_ATTEMPTS + 1):
        try:
            uuid = material_client.resolve_uuid(product_id)
            last_network_error = None
            break
        except SAPMaterialAuthError as e:
            return {"product_id": product_id, "success": False, "reason": f"Auth error: {e}"}
        except SAPMaterialError as e:
            return {"product_id": product_id, "success": False, "reason": f"SAP material lookup failed: {e}"}
        except requests.exceptions.RequestException as e:
            last_network_error = str(e)
            time.sleep(2)
    if last_network_error:
        return {"product_id": product_id, "success": False, "reason": f"Network error resolving UUID: {last_network_error}"}
    if not uuid:
        return {"product_id": product_id, "success": False, "reason": "No material with this ID found in SAP"}

    for attempt in range(1, RETRYABLE_ATTEMPTS + 1):
        try:
            planning = planning_client.get_planning_data([uuid])
            data = planning.get(uuid.upper())
            if not data:
                return {"product_id": product_id, "success": False, "reason": "SAP has no Supply Planning record for this material"}
            updated = planning_client.push_planning_data(uuid, data["rows"], None, row["lead_time_days"])
            return {
                "product_id": product_id, "success": True, "uuid": uuid,
                "planning_areas_updated": updated, "lead_time_days": row["lead_time_days"],
            }
        except SAPPlanningError as e:
            return {"product_id": product_id, "success": False, "reason": str(e)}
        except requests.exceptions.RequestException as e:
            last_network_error = str(e)
            time.sleep(2)
    return {"product_id": product_id, "success": False, "reason": f"Network error pushing planning data: {last_network_error}"}


def check_mode(n):
    rows = load_rows()[:n]
    print(f"Dry-run: resolving + reading SAP planning data for {len(rows)} sample rows (NO write)...\n")
    for row in rows:
        product_id = row["product_id"]
        try:
            uuid = material_client.resolve_uuid(product_id)
        except (SAPMaterialAuthError, SAPMaterialError) as e:
            print(f"  {product_id}: FAIL resolving UUID - {e}")
            continue
        if not uuid:
            print(f"  {product_id}: NOT FOUND in SAP")
            continue
        try:
            planning = planning_client.get_planning_data([uuid])
            data = planning.get(uuid.upper())
            if not data:
                print(f"  {product_id}: UUID={uuid} but NO planning record in SAP")
                continue
            print(
                f"  {product_id}: UUID={uuid}, current SAP lead_time={data['lead_time_days']}, "
                f"will set -> {row['lead_time_days']}, planning areas={data['planning_area_count']}"
            )
        except SAPPlanningError as e:
            print(f"  {product_id}: UUID={uuid} but planning read failed - {e}")


WORKERS = 3  # matches sap_rate_limiter.SAP_MAX_CONCURRENT_REQUESTS - this
             # script is a separate process from the live backend, so it
             # can't share that semaphore, but staying at the same cap
             # keeps combined load within the tenant's known concurrent
             # web-service session limit.

_write_lock = threading.Lock()


def run_mode():
    rows = load_rows()
    print(f"Excel has {len(rows)} rows with Product ID + Lead Time both present.")

    results = {"updated": [], "failed": []}
    if os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH) as f:
            results = json.load(f)
        processed_ids = {e["product_id"] for e in results.get("updated", [])} | {e["product_id"] for e in results.get("failed", [])}
        print(f"Resuming: {len(processed_ids)} already processed in a prior run.")
    else:
        processed_ids = set()

    remaining = [r for r in rows if r["product_id"] not in processed_ids]
    total = len(remaining)
    print(f"{total} remaining to process, {WORKERS} concurrent workers.")
    done = 0

    def handle(row):
        nonlocal done
        try:
            outcome = process_one(row)
        except Exception as e:
            outcome = {"product_id": row["product_id"], "success": False, "reason": f"Unexpected error: {e}"}
        with _write_lock:
            done += 1
            if outcome["success"]:
                results["updated"].append(outcome)
                db["component_master"].update_one(
                    {"_id": outcome["product_id"]},
                    {"$set": {
                        "lead_time_days": outcome["lead_time_days"],
                        "product_uuid": outcome["uuid"],
                        "sap_pushed_at": datetime.now(timezone.utc),
                    }},
                    upsert=True,
                )
                print(f"  [{done}/{total}] UPDATED {outcome['product_id']} -> {outcome['lead_time_days']}d ({outcome['planning_areas_updated']} planning areas)")
            else:
                results["failed"].append(outcome)
                print(f"  [{done}/{total}] FAILED {outcome['product_id']}: {outcome['reason']}")
            with open(RESULTS_PATH, "w") as f:
                json.dump(results, f, indent=2, default=str)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(handle, row) for row in remaining]
        for f in as_completed(futures):
            f.result()

    print(f"\nDone. Updated: {len(results['updated'])}  Failed: {len(results['failed'])}")
    print(f"Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", nargs="?", const=15, type=int, metavar="N")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    if args.check is not None:
        check_mode(args.check)
    elif args.run:
        run_mode()
    else:
        parser.print_help()
        sys.exit(1)
