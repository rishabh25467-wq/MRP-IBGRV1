"""One-time script: cleans up Supplier Master contact data per user's
Sep 2026 rules (SAP is the source of truth, our local "suppliers" Mongo
cache just mirrors it):
1. Mobile number has the "91" country code duplicated (e.g.
   "+91 918506008249") and is longer than 12 digits -> drop the extra "91".
2. Contact Person name ends with the honorific "Ji"/"ji" -> strip it.
3. Contact Person has no title at all (Mr./Mrs./Ms./Miss/Dr.) -> prepend
   "Mr. " (existing titles of any kind are left untouched).

Pushes corrected values to SAP's own Supplier ContactPerson via the same
SupplierBundleMaintainRequest_sync_V1 mechanism as update_suppliers.py,
then mirrors the same values into the local "suppliers" Mongo collection
so both stay consistent without waiting for the next "Sync from SAP".

Usage:
  python fix_supplier_contacts.py --check   # dry-run, prints every planned change, zero writes
  python fix_supplier_contacts.py --run     # LIVE write to SAP + Mongo
"""
import os
import re
import sys
import json
import html
import time
import argparse

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

READ_ENDPOINT = os.environ["SAP_SOAP_SUPPLIER_ENDPOINT"]
WRITE_ENDPOINT = os.environ["SAP_SOAP_SUPPLIER_MANAGE_ENDPOINT"]
AUTH = HTTPBasicAuth(os.environ["SAP_SOAP_USERNAME"], os.environ["SAP_SOAP_PASSWORD"])

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "supplier_contact_cleanup_results.json")

TITLE_RE = re.compile(r"^(mr|mrs|ms|miss|dr)(?:\.|\s|$)", re.I)
TRAILING_JI_RE = re.compile(r"\s+ji\.?$", re.I)


def _tag_re(tag):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml, tag):
    m = _tag_re(tag).search(xml)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else None


def _all_blocks(xml, tag):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


def clean_mobile(raw):
    """Returns the corrected number, or None if no fix is needed."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("9191") and len(digits) > 12:
        fixed = digits[2:][-10:]
        return f"+91 {fixed}"
    return None


def clean_name(raw):
    """Returns the corrected name, or None if no fix is needed."""
    if not raw:
        return None
    original = raw.strip()
    new = TRAILING_JI_RE.sub("", original).strip()
    if not TITLE_RE.match(new):
        new = f"Mr. {new}"
    return new if new != original else None


def fetch_all_supplier_contacts():
    """One bulk SupplierByElementsQuery_sync call -> {CODE: {contact_uuid,
    family_name, telephones: [(number, is_mobile)]}} for the default
    contact of every active supplier."""
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
        if not contact_blocks:
            continue
        default = next(
            (c for c in contact_blocks if (_first_tag(c, "DefaultContactPersonIndicator") or "").lower() == "true"),
            contact_blocks[0],
        )
        contact_uuid = _first_tag(default, "BusinessPartnerContactUUID")
        family_name = _first_tag(default, "FamilyName")
        telephones = []
        for t in _all_blocks(default, "WorkplaceTelephone"):
            number = _first_tag(t, "FormattedNumberDescription")
            if not number:
                continue
            is_mobile = (_first_tag(t, "MobilePhoneNumberIndicator") or "").lower() == "true"
            telephones.append((number, is_mobile))
        result[internal_id.upper()] = {
            "contact_uuid": contact_uuid,
            "family_name": family_name,
            "telephones": telephones,
        }
    return result


def build_update_xml(code, contact_uuid, new_family_name, new_telephones):
    """new_telephones: full corrected list of (number, is_mobile) tuples,
    or None to leave SAP's existing telephone list untouched entirely."""
    code = code.upper()
    name_xml = f"<FamilyName>{html.escape(new_family_name)}</FamilyName>" if new_family_name else ""
    telephone_xml = ""
    telephone_complete_attr = ""
    if new_telephones is not None:
        entries = []
        for number, is_mobile in new_telephones:
            entries.append(
                f"<WorkplaceTelephone><FormattedNumberDescription>{html.escape(number)}"
                f"</FormattedNumberDescription><MobilePhoneNumberIndicator>{'true' if is_mobile else 'false'}"
                f"</MobilePhoneNumberIndicator></WorkplaceTelephone>"
            )
        telephone_xml = "".join(entries)
        telephone_complete_attr = ' workplaceTelephoneListCompleteTransmissionIndicator="true"'

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <n0:SupplierBundleMaintainRequest_sync_V1>
   <BasicMessageHeader><ID>SUPCLEAN-{html.escape(code)}</ID></BasicMessageHeader>
   <Supplier actionCode="02">
    <InternalID>{html.escape(code)}</InternalID>
    <ContactPerson actionCode="02"{telephone_complete_attr}>
     <BusinessPartnerContactUUID>{html.escape(contact_uuid)}</BusinessPartnerContactUUID>
     {name_xml}
     {telephone_xml}
    </ContactPerson>
   </Supplier>
  </n0:SupplierBundleMaintainRequest_sync_V1>
 </soapenv:Body></soapenv:Envelope>"""


def build_check_xml(code, contact_uuid, new_family_name, new_telephones):
    live = build_update_xml(code, contact_uuid, new_family_name, new_telephones)
    return live.replace("SupplierBundleMaintainRequest_sync_V1", "SupplierBundleMaintenanceCheckRequest_sync_V1")


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
        return {"success": False, "faultstring": f"Connection error after {retries} retries: {last_exc}"}
    text = resp.text
    is_fault = resp.status_code >= 400 or "<Fault" in text or ":Fault" in text
    severities = _all_blocks(text, "SeverityCode")
    has_error_severity = any(s.strip() in ("1", "3", "4") for s in severities)
    faultstring = _first_tag(text, "faultstring")
    success = not is_fault and not has_error_severity
    return {"success": success, "faultstring": faultstring, "raw_response": text[:4000]}


def plan_changes(sap_map):
    """Returns list of {code, contact_uuid, old_name, new_name, old_phone,
    new_phone, new_telephones} for every supplier that needs at least one
    fix."""
    plans = []
    for code, entry in sap_map.items():
        if not entry["contact_uuid"]:
            continue
        new_name = clean_name(entry["family_name"])
        new_telephones = None
        old_mobile = None
        new_mobile = None
        mobile_idx = next((i for i, (_, is_m) in enumerate(entry["telephones"]) if is_m), None)
        if mobile_idx is not None:
            old_mobile = entry["telephones"][mobile_idx][0]
            new_mobile = clean_mobile(old_mobile)
            if new_mobile:
                new_telephones = list(entry["telephones"])
                new_telephones[mobile_idx] = (new_mobile, True)
        if new_name or new_telephones:
            plans.append({
                "code": code,
                "contact_uuid": entry["contact_uuid"],
                "old_name": entry["family_name"],
                "new_name": new_name,
                "old_phone": old_mobile,
                "new_phone": new_mobile,
                "new_telephones": new_telephones,
            })
    return plans


def run(mode):
    print("Fetching all SAP supplier contacts (bulk read, one call)...")
    sap_map = fetch_all_supplier_contacts()
    print(f"SAP returned {len(sap_map)} suppliers with a default contact.")

    plans = plan_changes(sap_map)
    print(f"{len(plans)} supplier(s) need at least one fix.")

    if mode == "check":
        for p in plans:
            changes = []
            if p["new_name"]:
                changes.append(f"name '{p['old_name']}' -> '{p['new_name']}'")
            if p["new_phone"]:
                changes.append(f"mobile '{p['old_phone']}' -> '{p['new_phone']}'")
            print(f"  {p['code']}: " + "; ".join(changes))
        with open(RESULTS_PATH, "w") as f:
            json.dump({"checked_plans": plans}, f, indent=2, default=str)
        print(f"\nDry-run only, no writes made. Full plan saved to {RESULTS_PATH}")
        return

    client = MongoClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    results = {"updated": [], "failed": []}
    for p in plans:
        xml_body = build_update_xml(p["code"], p["contact_uuid"], p["new_name"], p["new_telephones"])
        outcome = post_supplier_update(xml_body)
        entry = {"code": p["code"], "new_name": p["new_name"], "new_phone": p["new_phone"], **outcome}
        if outcome["success"]:
            results["updated"].append(entry)
            mongo_updates = {}
            if p["new_name"]:
                mongo_updates["contact_person"] = p["new_name"]
            if p["new_phone"]:
                mongo_updates["phone"] = p["new_phone"]
            if mongo_updates:
                db["suppliers"].update_many({"sap_internal_id": p["code"]}, {"$set": mongo_updates})
            print(f"  UPDATED {p['code']}")
        else:
            results["failed"].append(entry)
            print(f"  FAILED {p['code']}: {outcome.get('faultstring')}")
        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=2, default=str)
        time.sleep(0.5)

    print(f"\nDone. Updated: {len(results['updated'])}  Failed: {len(results['failed'])}")
    print(f"Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.check:
        run("check")
    elif args.run:
        run("run")
    else:
        parser.print_help()
        sys.exit(1)
