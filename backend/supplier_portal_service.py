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
    if db[ACCOUNTS_COLLECTION].find_one({"email": email}):
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
        "created_at": datetime.now(timezone.utc),
    }
    db[INVITES_COLLECTION].insert_one(doc)
    return doc


def list_invites(db) -> list:
    return list(db[INVITES_COLLECTION].find().sort("created_at", -1))
