"""Microsoft Entra ID (Azure AD) SSO authentication + page-level access
control (Feb 2026).

Session model: an opaque random session_id (HttpOnly cookie) maps to a
session doc in Mongo (`auth_sessions`) holding just the signed-in user's
identity key. Every request re-reads that user's CURRENT role/allowed_pages
from `auth_users` (never cached in the cookie itself), so a permission
change made on the IT Access Management page takes effect on the user's
very next request - no forced re-login needed.

Login flow (MSAL authorization code flow, confidential client - per the
Entra ID SSO integration playbook):
  GET  /api/auth/login    -> redirect to Microsoft's sign-in page
  GET  /api/auth/callback -> exchange the code, upsert auth_users, create
                              an auth_sessions entry, set the session
                              cookie, redirect back into the app
  GET  /api/auth/me       -> current session's identity + role + allowed
                              pages (returns {"authenticated": False} if
                              not signed in - never a 401, so the frontend
                              can always safely check login state)
  POST /api/auth/logout   -> delete the session, clear the cookie

Page-level enforcement lives in `auth_middleware` (registered once in
server.py) using PAGE_ROUTE_RULES below, rather than a Depends() on every
individual route - a route added later can't accidentally end up
unprotected by omission.

New users (first successful Microsoft sign-in) are inserted with
allowed_pages=[] and role="user" (unless their email matches
SUPER_ADMIN_EMAILS) - the frontend shows a "Pending Access" screen for
that state until a Super Admin grants specific pages.
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import asyncio

import msal
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)

AZURE_AD_TENANT_ID = os.environ["AZURE_AD_TENANT_ID"]
AZURE_AD_CLIENT_ID = os.environ["AZURE_AD_CLIENT_ID"]
AZURE_AD_CLIENT_SECRET = os.environ["AZURE_AD_CLIENT_SECRET"]
AUTHORITY = f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}"
SCOPES = ["User.Read"]
SUPER_ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("SUPER_ADMIN_EMAILS", "").split(",") if e.strip()}

SESSION_COOKIE_NAME = "vms_session"
SESSION_TTL = timedelta(days=14)
OAUTH_STATE_TTL = timedelta(minutes=10)

USERS_COLLECTION = "auth_users"
SESSIONS_COLLECTION = "auth_sessions"
OAUTH_STATES_COLLECTION = "auth_oauth_states"

# Every page in the app's nav that can be independently granted/revoked on
# the IT Access Management page. Keys are checked against PAGE_ROUTE_RULES
# below - keep both in sync when adding a new page.
PAGE_CATALOG = [
    {"key": "bom_explorer", "label": "BOM Explorer"},
    {"key": "purchasing_plan", "label": "Purchasing Plan"},
    {"key": "production_plan", "label": "Production Plan"},
    {"key": "production_confirmation", "label": "Production Confirmation"},
    {"key": "inventory", "label": "Inventory"},
    {"key": "supplier_master", "label": "Supplier Master"},
    {"key": "quota_allocation", "label": "Quota Allocation"},
    {"key": "admin", "label": "Admin"},
    {"key": "admin_sap_write", "label": "Admin - SAP Write"},
]
PAGE_KEYS = {p["key"] for p in PAGE_CATALOG}

# (path_prefix, {page_keys that grant access}) - checked in order, FIRST
# match wins. Built directly from each page's actual API calls (grepped
# from the frontend, not guessed) so overlapping endpoints shared by two
# pages (e.g. /suppliers is used by both Supplier Master and Quota
# Allocation) correctly grant access if the user has EITHER page. A path
# matching none of these still requires a valid session (see
# require_login below) but no specific page permission - used for small
# shared/utility endpoints not tied to one visible page.
PAGE_ROUTE_RULES = [
    ("/api/bom/", {"bom_explorer"}),
    ("/api/sap/cost-estimate-run", {"bom_explorer"}),
    ("/api/purchasing-plan/", {"purchasing_plan"}),
    ("/api/sales-plan", {"purchasing_plan"}),
    ("/api/part-suppliers/", {"purchasing_plan"}),
    ("/api/production-plan/", {"production_plan"}),
    ("/api/production-confirmation/", {"production_confirmation"}),
    ("/api/inventory", {"inventory"}),
    ("/api/suppliers/bulk-push-erp-to-sap", {"admin_sap_write"}),
    ("/api/suppliers/sap-price-specs", {"quota_allocation", "admin_sap_write"}),
    ("/api/suppliers/erp-prices/", {"quota_allocation"}),
    ("/api/suppliers/sap-purchase-history/", {"quota_allocation"}),
    ("/api/suppliers/sap-receipt-dates/", {"quota_allocation"}),
    ("/api/suppliers/sync-from-sap", {"supplier_master"}),
    ("/api/suppliers", {"supplier_master", "quota_allocation"}),
    ("/api/products/search", {"quota_allocation"}),
    ("/api/quota-arrangements/", {"quota_allocation"}),
    ("/api/admin/components", {"admin"}),
    ("/api/admin/categories", {"admin"}),
]

# Paths the auth middleware never gates - login must stay reachable while
# logged out, and /auth/me must never itself 401 (the frontend uses it to
# find out WHETHER it's logged in).
PUBLIC_PATHS = {"/api/", "/api/auth/login", "/api/auth/callback", "/api/auth/me"}


def ensure_indexes(db) -> None:
    """TTL indexes so expired sessions/OAuth flow state get cleaned up
    automatically instead of growing the collections forever - same
    pattern as job_store.ensure_indexes."""
    db[SESSIONS_COLLECTION].create_index("expires_at", expireAfterSeconds=0)
    db[OAUTH_STATES_COLLECTION].create_index("expires_at", expireAfterSeconds=0)


def resolve_required_pages(path: str):
    for prefix, pages in PAGE_ROUTE_RULES:
        if path.startswith(prefix):
            return pages
    return None


def _user_key(tid: str, oid: str) -> str:
    return f"{tid}:{oid}"


def _build_msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        AZURE_AD_CLIENT_ID, authority=AUTHORITY, client_credential=AZURE_AD_CLIENT_SECRET,
    )


def _redirect_uri_for(request: Request) -> str:
    """Derives the OAuth redirect_uri from the ACTUAL incoming request's
    host rather than a hardcoded env var - this app is reachable at
    multiple domains (preview + production), both already registered as
    valid Web redirect URIs on the Azure AD app registration, so login
    works correctly in either environment without per-environment .env
    reconfiguration."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host"))
    return f"{proto}://{host}/api/auth/callback"


def start_login(request: Request, db) -> RedirectResponse:
    msal_app = _build_msal_app()
    flow = msal_app.initiate_auth_code_flow(SCOPES, redirect_uri=_redirect_uri_for(request))
    db[OAUTH_STATES_COLLECTION].insert_one({
        "_id": flow["state"], "flow": flow,
        "expires_at": datetime.now(timezone.utc) + OAUTH_STATE_TTL,
    })
    return RedirectResponse(flow["auth_uri"])


def handle_callback(request: Request, db):
    params = dict(request.query_params)
    state = params.get("state")
    saved = state and db[OAUTH_STATES_COLLECTION].find_one({"_id": state})
    if not saved:
        return RedirectResponse("/?auth_error=state_expired")
    db[OAUTH_STATES_COLLECTION].delete_one({"_id": state})

    result = _build_msal_app().acquire_token_by_auth_code_flow(saved["flow"], params)
    if "error" in result:
        logger.warning(f"Azure AD login failed: {result.get('error')}: {result.get('error_description')}")
        return RedirectResponse("/?auth_error=login_failed")

    claims = result.get("id_token_claims", {})
    tid, oid = claims.get("tid"), claims.get("oid")
    email = (claims.get("preferred_username") or claims.get("email") or "").strip().lower()
    name = claims.get("name") or email
    user_id = _user_key(tid, oid)

    now = datetime.now(timezone.utc)
    is_super_admin = email in SUPER_ADMIN_EMAILS
    existing = db[USERS_COLLECTION].find_one({"_id": user_id})
    role = "super_admin" if is_super_admin else (existing.get("role", "user") if existing else "user")
    allowed_pages = existing.get("allowed_pages", []) if existing else []
    db[USERS_COLLECTION].update_one(
        {"_id": user_id},
        {
            "$set": {"tid": tid, "oid": oid, "email": email, "name": name, "role": role,
                      "allowed_pages": allowed_pages, "last_login_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    session_id = secrets.token_urlsafe(32)
    db[SESSIONS_COLLECTION].insert_one({"_id": session_id, "user_id": user_id, "expires_at": now + SESSION_TTL})

    response = RedirectResponse("/")
    response.set_cookie(
        SESSION_COOKIE_NAME, session_id, httponly=True, secure=True, samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()), path="/",
    )
    return response


def get_current_user(request: Request, db) -> dict | None:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    session = db[SESSIONS_COLLECTION].find_one({"_id": session_id})
    if not session:
        return None
    return db[USERS_COLLECTION].find_one({"_id": session["user_id"]})


def logout(request: Request, db) -> JSONResponse:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        db[SESSIONS_COLLECTION].delete_one({"_id": session_id})
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


def user_public_view(user: dict) -> dict:
    role = user.get("role", "user")
    return {
        "authenticated": True,
        "email": user.get("email"),
        "name": user.get("name"),
        "role": role,
        "allowed_pages": sorted(PAGE_KEYS) if role == "super_admin" else user.get("allowed_pages", []),
    }


def create_auth_middleware(db):
    """Factory (not a bare module-level function) so the middleware closes
    over `db` without a circular import back to server.py, where `db` is
    actually constructed."""

    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith("/api/") or path in PUBLIC_PATHS:
            return await call_next(request)

        user = await asyncio.to_thread(get_current_user, request, db)
        if not user:
            return JSONResponse({"detail": "Login required"}, status_code=401)

        required_pages = resolve_required_pages(path)
        if required_pages and user.get("role") != "super_admin":
            if not (set(user.get("allowed_pages", [])) & required_pages):
                return JSONResponse({"detail": "Access denied"}, status_code=403)

        request.state.user = user
        return await call_next(request)

    return auth_middleware
