"""One-time script: push Contact Name/Phone/Email updates from
Emergent_Supplier_Master.xlsx to SAP's existing default Supplier
ContactPerson via ManageSupplierIn (MaintainBundle_V1 / CheckMaintainBundle_V1).

Per user's explicit instructions (Sep 2026):
- One-time push, not a recurring job / no UI.
- "Change Old one" -> update the existing default contact, don't add a new one.
- A supplier code not found in SAP's own master -> skip it and log for review.

Usage:
  python update_suppliers.py --check         # dry-run (SAP CheckMaintainBundle_V1, zero side effects)
  python update_suppliers.py --check A2343    # dry-run, single code only
  python update_suppliers.py --run            # LIVE write to production SAP
"""
import os
import re
import sys
import json
import html
import time
import argparse
from io import BytesIO

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

EXCEL_URL = "https://customer-assets-m6fa6gv7.emergentagent.net/job_sap-data-sync/artifacts/dgzzm1b6_Emergent_Supplier_Master.xlsx"

READ_ENDPOINT = os.environ["SAP_SOAP_SUPPLIER_ENDPOINT"]
WRITE_ENDPOINT = os.environ["SAP_SOAP_SUPPLIER_MANAGE_ENDPOINT"]
SAP_USER = os.environ["SAP_SOAP_USERNAME"]
SAP_PASSWORD = os.environ["SAP_PASSWORD"]
AUTH = HTTPBasicAuth(SAP_USER, SAP_PASSWORD)

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "supplier_update_results.json")


def _tag_re(tag):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml, tag):
    m = _tag_re(tag).search(xml)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else None


def _all_blocks(xml, tag):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


def load_excel_rows():
    """Returns list of {code, contact_name, phone, email} for every row
    that has a non-blank Code."""
    import pandas as pd

    resp = requests.get(EXCEL_URL, timeout=60)
    resp.raise_for_status()
    df = pd.read_excel(BytesIO(resp.content))
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("Code") or "").strip()
        if not code or code.lower() == "nan" or code == "0":
            continue
        contact_name = str(r.get("contact_p") or "").strip()
        phone = str(r.get("ContactNo") or "").strip()
        email = str(r.get("EMAIL") or "").strip()
        contact_name = "" if contact_name.lower() == "nan" else contact_name
        phone = "" if phone.lower() == "nan" else phone
        email = "" if email.lower() == "nan" else email
        if not contact_name and not phone and not email:
            continue
        rows.append({"code": code, "contact_name": contact_name, "phone": phone, "email": email})
    return rows


def fetch_sap_supplier_map():
    """One bulk SupplierByElementsQuery_sync call -> {CODE: {contact_uuid,
    contact_internal_id, has_default_contact}}."""
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
    <Supplier contactPersonTransmissionRequestCode="1"/>
   </RequestedElements>
  </glob:SupplierByElementsQuery_sync>
 </soapenv:Body></soapenv:Envelope>"""
    resp = requests.post(
        READ_ENDPOINT,
        data=query_xml.encode("utf-8"),
        auth=AUTH,
        headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
        timeout=180,
    )
    resp.raise_for_status()
    xml = resp.text
    result = {}
    for block in _all_blocks(xml, "Supplier"):
        internal_id = _first_tag(block, "InternalID")
        if not internal_id:
            continue
        contact_blocks = _all_blocks(block, "ContactPerson")
        contact_uuid = None
        contact_internal_id = None
        if contact_blocks:
            default = next(
                (c for c in contact_blocks if (_first_tag(c, "DefaultContactPersonIndicator") or "").lower() == "true"),
                contact_blocks[0],
            )
            contact_uuid = _first_tag(default, "BusinessPartnerContactUUID")
            contact_internal_id = _first_tag(default, "BusinessPartnerContactInternalID")
        result[internal_id.upper()] = {
            "contact_uuid": contact_uuid,
            "contact_internal_id": contact_internal_id,
        }
    return result


def _split_phones(raw_phone):
    """Excel sometimes has 2 numbers separated by / or , - SAP allows max
    2 WorkplaceTelephone entries (one mobile=true, one mobile=false)."""
    parts = [p.strip() for p in re.split(r"[/,]", raw_phone) if p.strip()]
    return parts[:2]


def _first_email(raw_email):
    """Excel sometimes has multiple emails separated by comma/space - SAP's
    WorkplaceEMailURI is a single value and rejects multi-address strings."""
    if not raw_email:
        return ""
    parts = re.split(r"[,\s]+", raw_email.strip())
    return parts[0] if parts else ""


def build_update_xml(code, contact_uuid, contact_internal_id, contact_name, phone, email):
    code = code.upper()
    contact_id_xml = (
        f"<BusinessPartnerContactUUID>{html.escape(contact_uuid)}</BusinessPartnerContactUUID>"
        if contact_uuid
        else f"<BusinessPartnerContactInternalID>{html.escape(contact_internal_id)}</BusinessPartnerContactInternalID>"
    )
    name_xml = f"<FamilyName>{html.escape(contact_name)}</FamilyName>" if contact_name else ""
    email_xml = f"<WorkplaceEMailURI>{html.escape(_first_email(email))}</WorkplaceEMailURI>" if _first_email(email) else ""
    phones = _split_phones(phone) if phone else []
    telephone_xml = ""
    if phones:
        entries = []
        for i, number in enumerate(phones):
            mobile = "true" if i == 0 else "false"
            entries.append(
                f"<WorkplaceTelephone><FormattedNumberDescription>{html.escape(number)}"
                f"</FormattedNumberDescription><MobilePhoneNumberIndicator>{mobile}"
                f"</MobilePhoneNumberIndicator></WorkplaceTelephone>"
            )
        telephone_xml = "".join(entries)
    telephone_complete_attr = ' workplaceTelephoneListCompleteTransmissionIndicator="true"' if phones else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <n0:SupplierBundleMaintainRequest_sync_V1>
   <BasicMessageHeader><ID>SUPUPD-{html.escape(code)}</ID></BasicMessageHeader>
   <Supplier actionCode="02">
    <InternalID>{html.escape(code)}</InternalID>
    <ContactPerson actionCode="02"{telephone_complete_attr}>
     {contact_id_xml}
     {name_xml}
     {email_xml}
     {telephone_xml}
    </ContactPerson>
   </Supplier>
  </n0:SupplierBundleMaintainRequest_sync_V1>
 </soapenv:Body></soapenv:Envelope>"""


def build_check_xml(code, contact_uuid, contact_internal_id, contact_name, phone, email):
    live = build_update_xml(code, contact_uuid, contact_internal_id, contact_name, phone, email)
    return live.replace(
        "SupplierBundleMaintainRequest_sync_V1", "SupplierBundleMaintenanceCheckRequest_sync_V1"
    )


def post_supplier_update(xml_body, retries=3):
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                WRITE_ENDPOINT,
                data=xml_body.encode("utf-8"),
                auth=AUTH,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                timeout=90,
            )
            break
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(5 * (attempt + 1))
    else:
        return {
            "success": False,
            "http_status": None,
            "faultstring": f"Connection error after {retries} retries: {last_exc}",
            "notes": [],
            "raw_response": "",
        }
    text = resp.text
    is_fault = resp.status_code >= 400 or "<Fault" in text or ":Fault" in text
    error_notes = [n for n in _all_blocks(text, "Note")]
    severities = _all_blocks(text, "SeverityCode")
    has_error_severity = any(s.strip() in ("1", "3", "4") for s in severities)  # 1=Error in BOD Log
    faultstring = _first_tag(text, "faultstring")
    success = not is_fault and not has_error_severity
    return {
        "success": success,
        "http_status": resp.status_code,
        "faultstring": faultstring,
        "notes": error_notes,
        "raw_response": text[:4000],
    }


def run(mode, only_code=None):
    print("Fetching SAP supplier map (bulk read, one call)...")
    sap_map = fetch_sap_supplier_map()
    print(f"SAP returned {len(sap_map)} suppliers.")

    print("Reading Excel...")
    rows = load_excel_rows()
    print(f"Excel has {len(rows)} rows with contact data.")

    if only_code:
        rows = [r for r in rows if r["code"].upper() == only_code.upper()]
        print(f"Filtered to code {only_code}: {len(rows)} row(s).")

    results = {"updated": [], "skipped_not_found": [], "failed": [], "checked": []}
    processed_codes = set()
    if mode == "run" and only_code is None and os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH) as f:
            prior = json.load(f)
        if "updated" in prior:
            results = prior
            for bucket in ("updated", "skipped_not_found", "failed"):
                processed_codes.update(e["code"] for e in results.get(bucket, []))
            print(f"Resuming: {len(processed_codes)} codes already processed in a prior run.")

    for row in rows:
        code = row["code"]
        if code in processed_codes:
            continue
        sap_entry = sap_map.get(code.upper())
        if not sap_entry or not (sap_entry["contact_uuid"] or sap_entry["contact_internal_id"]):
            results["skipped_not_found"].append({"code": code, "reason": "Supplier or default contact not found in SAP"})
            print(f"  SKIP {code}: not found in SAP")
            with open(RESULTS_PATH, "w") as f:
                json.dump(results, f, indent=2)
            continue

        xml_builder = build_check_xml if mode == "check" else build_update_xml
        xml_body = xml_builder(
            code, sap_entry["contact_uuid"], sap_entry["contact_internal_id"],
            row["contact_name"], row["phone"], row["email"],
        )
        outcome = post_supplier_update(xml_body)
        entry = {"code": code, **row, **outcome}
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
