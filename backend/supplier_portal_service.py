"""External Supplier Portal - vendor onboarding + JWT auth (Aug 2026).

Runs ALONGSIDE (not integrated with) the internal Microsoft Entra ID SSO
in auth_service.py - suppliers are external vendors with no Azure AD
identity, so they get their own email+password signup, an admin-approval
gate (a new SAP-side supplier account only becomes usable once an
internal staff member approves it - user's explicit ask), and a JWT
session cookie completely separate from `vms_session`.

Phase 1 (SAP PO fetch, see sap_po_client.py) + Phase 2 (this file -
onboarding/auth) land in this session. Phase 3 (shipment 2-way match),
4 (GRN -> SAP Goods Receipt RESTRICTED) and 5 (QMS feed) land in a later
session once the SAP endpoints are confirmed by the user.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

import object_storage_service

ACCOUNTS_COLLECTION = "supplier_portal_accounts"
LOGIN_ATTEMPTS_COLLECTION = "supplier_portal_login_attempts"
INVITES_COLLECTION = "supplier_portal_invites"

SESSION_COOKIE_NAME = "supplier_token"
SESSION_TTL = timedelta(hours=24)
JWT_ALGORITHM = "HS256"

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

APP_NAME = "sap-mrp-portal"


class SupplierPortalError(Exception):
    pass


class SupplierPortalValidationError(SupplierPortalError):
    pass


class SupplierPortalAuthError(SupplierPortalError):
    pass


class SupplierPortalNotFoundError(SupplierPortalError):
    pass


def ensure_indexes(db) -> None:
    db[ACCOUNTS_COLLECTION].create_index("email", unique=True)


def _jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def signup(db, vendor_code: str, company_name: str, email: str, password: str,
           gst_number: str, pan_number: str,
           gst_bytes: bytes, gst_filename: str, gst_content_type: str,
           pan_bytes: bytes, pan_filename: str, pan_content_type: str) -> dict:
    email = (email or "").strip().lower()
    vendor_code = (vendor_code or "").strip()
    company_name = (company_name or "").strip()
    gst_number = (gst_number or "").strip()
    pan_number = (pan_number or "").strip()
    if not (vendor_code and company_name and email and password and gst_number and pan_number):
        raise SupplierPortalValidationError("All fields are required")
    if len(password) < 8:
        raise SupplierPortalValidationError("Password must be at least 8 characters")
    if not (gst_bytes and pan_bytes):
        raise SupplierPortalValidationError("GST certificate and PAN card documents are both required")
    existing = db[ACCOUNTS_COLLECTION].find_one({"email": email})
    if existing:
        if existing.get("status") == "rejected":
            # Sep 9 2026 fix: a rejected applicant must be able to re-apply
            # with the same email (e.g. corrected vendor code) instead of
            # being permanently blocked by their old rejected record.
            db[ACCOUNTS_COLLECTION].delete_one({"_id": existing["_id"]})
        else:
            raise SupplierPortalValidationError("An account with this email already exists")

    account_id = str(uuid.uuid4())
    gst_ext = gst_filename.rsplit(".", 1)[-1] if gst_filename and "." in gst_filename else "bin"
    pan_ext = pan_filename.rsplit(".", 1)[-1] if pan_filename and "." in pan_filename else "bin"
    gst_path = f"{APP_NAME}/supplier-docs/{account_id}/gst.{gst_ext}"
    pan_path = f"{APP_NAME}/supplier-docs/{account_id}/pan.{pan_ext}"
    object_storage_service.put_object(gst_path, gst_bytes, gst_content_type or "application/octet-stream")
    object_storage_service.put_object(pan_path, pan_bytes, pan_content_type or "application/octet-stream")

    now = datetime.now(timezone.utc)
    doc = {
        "_id": account_id,
        "vendor_code": vendor_code,
        "company_name": company_name,
        "email": email,
        "password_hash": _hash_password(password),
        "gst_number": gst_number,
        "pan_number": pan_number,
        "gst_doc_path": gst_path,
        "gst_doc_filename": gst_filename,
        "gst_doc_content_type": gst_content_type,
        "pan_doc_path": pan_path,
        "pan_doc_filename": pan_filename,
        "pan_doc_content_type": pan_content_type,
        "status": "pending",
        "rejection_reason": None,
        "approved_by": None,
        "approved_at": None,
        "created_at": now,
    }
    try:
        db[ACCOUNTS_COLLECTION].insert_one(doc)
    except Exception as e:
        if "duplicate key" in str(e).lower():
            raise SupplierPortalValidationError("An account with this email already exists")
        raise
    return doc


def authenticate(db, email: str, password: str) -> dict:
    # Keyed on email ONLY (not request IP) - behind this app's k8s
    # ingress, request.client.host is a rotating ingress pod IP, not the
    # real client, so an IP-based key silently split the failed-attempt
    # counter across docs and lockout never triggered (found by
    # testing_agent, iteration_120).
    email = (email or "").strip().lower()
    now = datetime.now(timezone.utc)
    attempt = db[LOGIN_ATTEMPTS_COLLECTION].find_one({"_id": email})
    if attempt and attempt.get("locked_until") and attempt["locked_until"] > now:
        raise SupplierPortalAuthError("Too many failed attempts. Please try again in a few minutes.")

    account = db[ACCOUNTS_COLLECTION].find_one({"email": email})
    if not account or not _verify_password(password, account["password_hash"]):
        count = (attempt.get("count", 0) if attempt else 0) + 1
        locked_until = now + LOCKOUT_DURATION if count >= MAX_FAILED_ATTEMPTS else None
        db[LOGIN_ATTEMPTS_COLLECTION].update_one(
            {"_id": email}, {"$set": {"count": count, "locked_until": locked_until}}, upsert=True,
        )
        raise SupplierPortalAuthError("Invalid email or password")

    db[LOGIN_ATTEMPTS_COLLECTION].delete_one({"_id": email})
    # Rejected accounts ARE allowed to authenticate (a session/JWT is
    # still issued) - the frontend's Pending/Rejected screen is what
    # surfaces `rejection_reason`, and every data-bearing route
    # (/purchase-orders etc.) independently gates on status=="approved",
    # so this can never leak access. Blocking login entirely here (the
    # previous behavior) made rejection_reason unreachable dead code -
    # found by testing_agent, iteration_120.
    return account


def create_access_token(account_id: str, email: str) -> str:
    payload = {"sub": account_id, "email": email, "exp": datetime.now(timezone.utc) + SESSION_TTL}
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def get_current_account(request, db):
    """Returns None (never raises) on any missing/invalid/expired token -
    callers decide whether that means 401 or just "not logged in yet"."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return db[ACCOUNTS_COLLECTION].find_one({"_id": payload["sub"]})


def account_public_view(account: dict) -> dict:
    return {
        "authenticated": True,
        "account_id": account["_id"],
        "vendor_code": account["vendor_code"],
        "company_name": account["company_name"],
        "email": account["email"],
        "status": account["status"],
        "rejection_reason": account.get("rejection_reason"),
    }


def list_accounts(db, status: str = None) -> list:
    query = {"status": status} if status else {}
    docs = list(db[ACCOUNTS_COLLECTION].find(query).sort("created_at", -1))
    for d in docs:
        d.pop("password_hash", None)
    return docs


def _get_account_or_404(db, account_id: str) -> dict:
    account = db[ACCOUNTS_COLLECTION].find_one({"_id": account_id})
    if not account:
        raise SupplierPortalNotFoundError("Supplier account not found")
    return account


def approve_account(db, account_id: str, approved_by: str) -> None:
    _get_account_or_404(db, account_id)
    db[ACCOUNTS_COLLECTION].update_one(
        {"_id": account_id},
        {"$set": {
            "status": "approved", "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc), "rejection_reason": None,
        }},
    )


def reject_account(db, account_id: str, rejected_by: str, reason: str) -> None:
    _get_account_or_404(db, account_id)
    db[ACCOUNTS_COLLECTION].update_one(
        {"_id": account_id},
        {"$set": {
            "status": "rejected", "approved_by": rejected_by,
            "approved_at": datetime.now(timezone.utc), "rejection_reason": (reason or "").strip() or None,
        }},
    )


def get_document(db, account_id: str, doc_type: str) -> tuple:
    account = _get_account_or_404(db, account_id)
    path = account.get(f"{doc_type}_doc_path")
    if not path:
        raise SupplierPortalNotFoundError("Document not found")
    filename = account.get(f"{doc_type}_doc_filename") or f"{doc_type}.bin"
    data, fallback_content_type = object_storage_service.get_object(path)
    return data, account.get(f"{doc_type}_doc_content_type") or fallback_content_type, filename


# ---- Additional Documents (Sep 9 2026, user's explicit ask): MSME
# Certificate + Bank Details/Cancelled Cheque, uploadable by the vendor
# any time AFTER approval (unlike GST/PAN, required upfront at signup).
# Each upload is a new REVISION, not a replacement - user's explicit ask
# ("if he upload two it should be saved as revision/latest he can't
# remove it") - so every version stays in `{doc_type}_documents` forever,
# object storage path included, and only the LATEST is treated as
# "current" by default. ----

ADDITIONAL_DOC_TYPES = ("msme", "bank")


MAX_ADDITIONAL_DOC_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_ADDITIONAL_DOC_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}


def upload_additional_document(db, account_id: str, doc_type: str, file_bytes: bytes, filename: str, content_type: str) -> dict:
    if doc_type not in ADDITIONAL_DOC_TYPES:
        raise SupplierPortalValidationError("Invalid document type")
    if not file_bytes:
        raise SupplierPortalValidationError("A file is required")
    if len(file_bytes) > MAX_ADDITIONAL_DOC_SIZE_BYTES:
        raise SupplierPortalValidationError("File is too large - maximum size is 10 MB")
    if content_type not in ALLOWED_ADDITIONAL_DOC_CONTENT_TYPES:
        raise SupplierPortalValidationError("Only PDF, JPG or PNG files are allowed")
    account = _get_account_or_404(db, account_id)
    if account.get("status") != "approved":
        raise SupplierPortalValidationError("Additional documents can only be uploaded after your account is approved")
    existing = account.get(f"{doc_type}_documents") or []
    version = len(existing) + 1
    ext = filename.rsplit(".", 1)[-1] if filename and "." in filename else "bin"
    path = f"{APP_NAME}/supplier-docs/{account_id}/{doc_type}/v{version}.{ext}"
    object_storage_service.put_object(path, file_bytes, content_type or "application/octet-stream")
    revision = {
        "version": version, "path": path, "filename": filename,
        "content_type": content_type, "uploaded_at": datetime.now(timezone.utc),
    }
    db[ACCOUNTS_COLLECTION].update_one({"_id": account_id}, {"$push": {f"{doc_type}_documents": revision}})
    return revision


def list_additional_documents(db, account_id: str) -> dict:
    account = _get_account_or_404(db, account_id)
    result = {}
    for doc_type in ADDITIONAL_DOC_TYPES:
        revisions = account.get(f"{doc_type}_documents") or []
        result[doc_type] = {"latest": revisions[-1] if revisions else None, "history": revisions}
    return result


def get_additional_document(db, account_id: str, doc_type: str, version: int = None) -> tuple:
    if doc_type not in ADDITIONAL_DOC_TYPES:
        raise SupplierPortalValidationError("Invalid document type")
    account = _get_account_or_404(db, account_id)
    revisions = account.get(f"{doc_type}_documents") or []
    revision = revisions[-1] if version is None else next((r for r in revisions if r["version"] == version), None)
    if not revision:
        raise SupplierPortalNotFoundError("Document not found")
    data, fallback_content_type = object_storage_service.get_object(revision["path"])
    return data, revision.get("content_type") or fallback_content_type, revision["filename"]


# ---- Invite Supplier (Sep 2 2026) - manual, one-at-a-time outreach.
# User's explicit ask: "No, I'll reach out manually" to bulk-invite, just
# a form near Supplier Portal Approvals to send one supplier at a time an
# invite email (signup link + their Vendor Code) via Microsoft Graph, sent
# AS the inviting staff member's own mailbox. Logged here purely for the
# admin's own audit trail of who's already been invited - does NOT create
# or pre-approve the actual signup itself, the vendor still fills out the
# real /supplier-portal/signup form (Vendor Code, GST, PAN docs) themselves. ----

def create_invite(db, company_name: str, vendor_code: str, email: str, invited_by: str) -> dict:
    doc = {
        "_id": str(uuid.uuid4()),
        "company_name": company_name.strip(),
        "vendor_code": vendor_code.strip().upper(),
        "email": email.strip().lower(),
        "invited_by": invited_by,
        "status": "sent",
        "resend_count": 0,
        "last_sent_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }
    db[INVITES_COLLECTION].insert_one(doc)
    return doc


def get_invite(db, invite_id: str) -> dict:
    invite = db[INVITES_COLLECTION].find_one({"_id": invite_id})
    if not invite:
        raise SupplierPortalNotFoundError("Invite not found")
    return invite


def record_resend(db, invite_id: str, resent_by: str) -> dict:
    db[INVITES_COLLECTION].update_one(
        {"_id": invite_id},
        {"$set": {"last_sent_at": datetime.now(timezone.utc), "last_resent_by": resent_by}, "$inc": {"resend_count": 1}},
    )
    return get_invite(db, invite_id)


# ---- Corrective Action Plan (CAP) submissions (Sep 2026, user's explicit
# ask): vendor-submitted CAPs against the (currently mocked/demo, Phase 5
# QMS feed pending) Q-Notifications and Audit NCs shown on the Audits & QC
# page. Persisted so a vendor's submission history survives a page
# revisit, even though the reference records themselves are still demo
# data - each submission just needs a stable reference_id to key off. ----

CAP_SUBMISSIONS_COLLECTION = "supplier_cap_submissions"


def create_cap_submission(db, account: dict, payload: dict, evidence_bytes: bytes = None,
                           evidence_filename: str = None, evidence_content_type: str = None) -> dict:
    required = ("reference_type", "reference_id", "root_cause", "containment_action", "corrective_action", "target_completion_date")
    if not all((payload.get(k) or "").strip() for k in required):
        raise SupplierPortalValidationError("Root cause, containment action, corrective action, and target completion date are required")

    cap_id = str(uuid.uuid4())
    evidence = None
    if evidence_bytes:
        ext = evidence_filename.rsplit(".", 1)[-1] if evidence_filename and "." in evidence_filename else "bin"
        path = f"{APP_NAME}/supplier-docs/{account['_id']}/cap/{cap_id}.{ext}"
        object_storage_service.put_object(path, evidence_bytes, evidence_content_type or "application/octet-stream")
        evidence = {"path": path, "filename": evidence_filename, "content_type": evidence_content_type}

    doc = {
        "_id": cap_id,
        "account_id": account["_id"],
        "vendor_code": account["vendor_code"],
        "reference_type": payload["reference_type"],
        "reference_id": payload["reference_id"],
        "root_cause": payload["root_cause"].strip(),
        "containment_action": payload["containment_action"].strip(),
        "corrective_action": payload["corrective_action"].strip(),
        "target_completion_date": payload["target_completion_date"],
        "evidence": evidence,
        "status": "submitted",
        "created_at": datetime.now(timezone.utc),
    }
    db[CAP_SUBMISSIONS_COLLECTION].insert_one(doc)
    return doc


def list_cap_submissions(db, vendor_code: str) -> list:
    return list(db[CAP_SUBMISSIONS_COLLECTION].find({"vendor_code": vendor_code}).sort("created_at", -1))


def list_invites(db) -> list:
    """Each invite is enriched with `signup_status` - whether that
    vendor code has actually signed up yet (user's explicit ask), by
    cross-referencing the real ACCOUNTS_COLLECTION. "not_signed_up" means
    no signup attempt yet; otherwise mirrors the account's own
    pending/approved/rejected status."""
    invites = list(db[INVITES_COLLECTION].find().sort("created_at", -1))
    status_by_code = {}
    for inv in invites:
        code = inv["vendor_code"]
        if code not in status_by_code:
            account = db[ACCOUNTS_COLLECTION].find_one({"vendor_code": code}, {"status": 1})
            status_by_code[code] = account["status"] if account else "not_signed_up"
        inv["signup_status"] = status_by_code[code]
    return invites
