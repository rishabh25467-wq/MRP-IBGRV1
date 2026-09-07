"""One-time script: push SAP Payment Terms (CashDiscountTermsCode) from
Payment Terms Supplier.xlsx for suppliers whose credit_day = 0.

Per user's explicit instructions (Sep 2026):
- Only suppliers where credit_day == 0 (the other ~670 with real day counts
  are out of scope for this batch).
- Code "0001" = "Due net payable immediately" (confirmed by user via SAP
  Business Configuration > Terms of Payment fine-tuning activity).
- If the supplier already has a payment term set, overwrite it with 0001
  anyway; if blank, just set it. Either way it's the same single write.

Usage:
  python update_payment_terms.py --check [CODE]   # dry-run, zero side effects
  python update_payment_terms.py --run [CODE]      # LIVE write to production SAP
"""
import os
import sys
import json
import html
import time
import argparse
from io import BytesIO

import requests
from dotenv import load_dotenv

load_dotenv()

from update_suppliers import (  # noqa: E402
    READ_ENDPOINT, WRITE_ENDPOINT, AUTH, _all_blocks, _first_tag, post_supplier_update,
)

EXCEL_URL = "https://customer-assets-m6fa6gv7.emergentagent.net/job_sap-data-sync/artifacts/l81i8sgl_Payment%20Terms%20Supplier.xlsx"
TERM_CODE = "0001"  # Due net payable immediately

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "payment_terms_update_results.json")


def load_zero_day_rows():
    resp = requests.get(EXCEL_URL, timeout=60)
    resp.raise_for_status()
    import pandas as pd
    df = pd.read_excel(BytesIO(resp.content))
    df["Pcode"] = df["Pcode"].astype(str).str.strip()
    df["credit_day"] = df["credit_day"].astype(str).str.strip()
    zero_rows = df[df["credit_day"] == "0"]
    return [{"code": c, "party": p} for c, p in zip(zero_rows["Pcode"], zero_rows["party"])]


def fetch_sap_supplier_info():
    """Bulk read -> {InternalID: has_purchasing_data} for all active suppliers."""
    query_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <glob:SupplierByElementsQuery_sync>
   <SupplierSelectionByElements>
    <SelectionByLifeCycleStatusCode>
     <InclusionExclusionCode>I</InclusionExclusionCode>
     <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
     <LowerBoundaryLifeCycleStatusCode>2</LowerBoundaryLifeCycleStatusCode>
    </SelectionByLifeCycleStatusCode>
   </SupplierSelectionByElements>
   <ProcessingConditions>
    <QueryHitsMaximumNumberValue>6000</QueryHitsMaximumNumberValue>
    <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
   </ProcessingConditions>
   <RequestedElements supplierTransmissionRequestCode="2">
    <Supplier purchasingDataTransmissionRequestCode="1"/>
   </RequestedElements>
  </glob:SupplierByElementsQuery_sync>
 </soapenv:Body></soapenv:Envelope>"""
    resp = requests.post(
        READ_ENDPOINT, data=query_xml.encode("utf-8"), auth=AUTH,
        headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
        timeout=180,
    )
    resp.raise_for_status()
    xml = resp.text
    info = {}
    for block in _all_blocks(xml, "Supplier"):
        iid = _first_tag(block, "InternalID")
        if iid:
            info[iid.upper()] = bool(_all_blocks(block, "PurchasingData"))
    return info


def build_payment_term_xml(code, create_new=False):
    code = code.upper()
    action = "01" if create_new else "02"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <n0:SupplierBundleMaintainRequest_sync_V1>
   <BasicMessageHeader><ID>PAYTERM-{html.escape(code)}</ID></BasicMessageHeader>
   <Supplier actionCode="02">
    <InternalID>{html.escape(code)}</InternalID>
    <PurchasingData actionCode="{action}">
     <CashDiscountTermsCode>{TERM_CODE}</CashDiscountTermsCode>
    </PurchasingData>
   </Supplier>
  </n0:SupplierBundleMaintainRequest_sync_V1>
 </soapenv:Body></soapenv:Envelope>"""


def build_check_xml(code, create_new=False):
    return build_payment_term_xml(code, create_new).replace(
        "SupplierBundleMaintainRequest_sync_V1", "SupplierBundleMaintenanceCheckRequest_sync_V1"
    )


def run(mode, only_code=None):
    print("Fetching SAP supplier info (bulk read, one call)...")
    sap_info = fetch_sap_supplier_info()
    print(f"SAP has {len(sap_info)} active suppliers.")

    print("Reading Excel, filtering credit_day == 0...")
    rows = load_zero_day_rows()
    print(f"Excel has {len(rows)} rows with credit_day = 0.")

    if only_code:
        rows = [r for r in rows if r["code"].upper() == only_code.upper()]
        print(f"Filtered to code {only_code}: {len(rows)} row(s).")

    results = {"updated": [], "failed": [], "skipped_not_found": [], "checked": []}
    processed_codes = set()
    if os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH) as f:
            results = json.load(f)
        if mode == "run" and only_code is None:
            for bucket in ("updated", "skipped_not_found"):
                processed_codes.update(e["code"] for e in results.get(bucket, []))
            print(f"Resuming: {len(processed_codes)} codes already processed in a prior run.")

    for row in rows:
        code = row["code"]
        if code in processed_codes:
            continue
        if code.upper() not in sap_info:
            results["skipped_not_found"].append({"code": code, "party": row["party"], "reason": "Supplier code not found in SAP"})
            print(f"  SKIP {code}: not found in SAP")
            with open(RESULTS_PATH, "w") as f:
                json.dump(results, f, indent=2)
            continue

        create_new = not sap_info[code.upper()]
        xml_body = build_check_xml(code, create_new) if mode == "check" else build_payment_term_xml(code, create_new)
        outcome = post_supplier_update(xml_body)
        entry = {"code": code, "party": row["party"], **outcome}
        if mode == "check":
            results["checked"].append(entry)
            print(f"  CHECK {code}: {'OK' if outcome['success'] else 'FAIL - ' + str(outcome.get('faultstring'))}")
        elif outcome["success"]:
            results["updated"].append(entry)
            print(f"  UPDATED {code}")
        else:
            results["failed"].append(entry)
            print(f"  FAILED {code}: {outcome.get('faultstring')}")

        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=2)
        time.sleep(0.5)

    print(f"\nDone. Results written to {RESULTS_PATH}")
    print(f"Updated: {len(results['updated'])}  Checked: {len(results['checked'])}  "
          f"Failed: {len(results['failed'])}  Skipped (not found): {len(results['skipped_not_found'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", nargs="?", const="ALL", metavar="CODE")
    parser.add_argument("--run", nargs="?", const="ALL", metavar="CODE")
    args = parser.parse_args()

    if args.check:
        run("check", only_code=None if args.check == "ALL" else args.check)
    elif args.run:
        run("run", only_code=None if args.run == "ALL" else args.run)
    else:
        parser.print_help()
        sys.exit(1)
