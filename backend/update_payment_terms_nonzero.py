"""One-time script: push SAP Payment Terms for the non-zero credit_day
suppliers in Payment Terms Supplier.xlsx (the credit_day == 0 batch was
already handled by update_payment_terms.py).

Day-count -> SAP Terms of Payment code mapping (confirmed by user from the
SAP Business Configuration > Terms of Payment fine-tuning table, all on an
"Invoice Date" basis - Z008/Z009/Z010 added by user's SAP Admin for this):
  1->Z008  2->Z003  3->Z004  4->Z005  5->Z006  7->0002  8->Z009
  10->Z001 14->0003 15->Z002 21->Z010 30->0010 45->0008 60->0012 75->0014 90->Z007

Usage:
  python update_payment_terms_nonzero.py --check [CODE]
  python update_payment_terms_nonzero.py --run [CODE]
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

from update_suppliers import READ_ENDPOINT, WRITE_ENDPOINT, AUTH, _all_blocks, _first_tag, post_supplier_update  # noqa: E402
from update_payment_terms import fetch_sap_supplier_info  # noqa: E402

EXCEL_URL = "https://customer-assets-m6fa6gv7.emergentagent.net/job_sap-data-sync/artifacts/l81i8sgl_Payment%20Terms%20Supplier.xlsx"

DAY_TO_CODE = {
    "1": "Z008", "2": "Z003", "3": "Z004", "4": "Z005", "5": "Z006",
    "7": "0002", "8": "Z009", "10": "Z001", "14": "0003", "15": "Z002",
    "21": "Z010", "30": "0010", "45": "0008", "60": "0012", "75": "0014", "90": "Z007",
}

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "payment_terms_nonzero_results.json")


def load_nonzero_rows():
    resp = requests.get(EXCEL_URL, timeout=60)
    resp.raise_for_status()
    import pandas as pd
    df = pd.read_excel(BytesIO(resp.content))
    df["Pcode"] = df["Pcode"].astype(str).str.strip()
    df["credit_day"] = df["credit_day"].astype(str).str.strip()
    rows = []
    for _, r in df.iterrows():
        days = r["credit_day"]
        if days == "0":
            continue  # handled by update_payment_terms.py already
        rows.append({"code": r["Pcode"], "party": r["party"], "days": days})
    return rows


def build_payment_term_xml(code, term_code, create_new=False):
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
     <CashDiscountTermsCode>{html.escape(term_code)}</CashDiscountTermsCode>
    </PurchasingData>
   </Supplier>
  </n0:SupplierBundleMaintainRequest_sync_V1>
 </soapenv:Body></soapenv:Envelope>"""


def build_check_xml(code, term_code, create_new=False):
    return build_payment_term_xml(code, term_code, create_new).replace(
        "SupplierBundleMaintainRequest_sync_V1", "SupplierBundleMaintenanceCheckRequest_sync_V1"
    )


def run(mode, only_code=None):
    print("Fetching SAP supplier info (bulk read, one call)...")
    sap_info = fetch_sap_supplier_info()
    print(f"SAP has {len(sap_info)} active suppliers.")

    print("Reading Excel, filtering credit_day != 0...")
    rows = load_nonzero_rows()
    print(f"Excel has {len(rows)} non-zero-day rows.")

    if only_code:
        rows = [r for r in rows if r["code"].upper() == only_code.upper()]
        print(f"Filtered to code {only_code}: {len(rows)} row(s).")

    results = {"updated": [], "failed": [], "skipped_not_found": [], "skipped_no_term_mapping": [], "checked": []}
    processed_codes = set()
    if os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH) as f:
            results = json.load(f)
        if mode == "run" and only_code is None:
            for bucket in ("updated", "skipped_not_found", "skipped_no_term_mapping"):
                processed_codes.update(e["code"] for e in results.get(bucket, []))
            print(f"Resuming: {len(processed_codes)} codes already processed in a prior run.")

    for row in rows:
        code = row["code"]
        if code in processed_codes:
            continue

        term_code = DAY_TO_CODE.get(row["days"])
        if not term_code:
            results["skipped_no_term_mapping"].append({"code": code, "party": row["party"], "days": row["days"]})
            print(f"  SKIP {code}: no SAP term mapping for {row['days']} days")
            with open(RESULTS_PATH, "w") as f:
                json.dump(results, f, indent=2)
            continue

        if code.upper() not in sap_info:
            results["skipped_not_found"].append({"code": code, "party": row["party"], "reason": "Supplier code not found in SAP"})
            print(f"  SKIP {code}: not found in SAP")
            with open(RESULTS_PATH, "w") as f:
                json.dump(results, f, indent=2)
            continue

        create_new = not sap_info[code.upper()]
        xml_body = (build_check_xml if mode == "check" else build_payment_term_xml)(code, term_code, create_new)
        outcome = post_supplier_update(xml_body)
        entry = {"code": code, "party": row["party"], "days": row["days"], "term_code": term_code, **outcome}
        if mode == "check":
            results["checked"].append(entry)
            print(f"  CHECK {code} ({row['days']}d -> {term_code}): {'OK' if outcome['success'] else 'FAIL - ' + str(outcome.get('faultstring'))}")
        elif outcome["success"]:
            results["updated"].append(entry)
            print(f"  UPDATED {code} ({row['days']}d -> {term_code})")
        else:
            results["failed"].append(entry)
            print(f"  FAILED {code}: {outcome.get('faultstring')}")

        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=2)
        time.sleep(0.5)

    print(f"\nDone. Results written to {RESULTS_PATH}")
    print(f"Updated: {len(results['updated'])}  Checked: {len(results['checked'])}  Failed: {len(results['failed'])}  "
          f"Skipped (not found): {len(results['skipped_not_found'])}  Skipped (no term mapping): {len(results['skipped_no_term_mapping'])}")


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
