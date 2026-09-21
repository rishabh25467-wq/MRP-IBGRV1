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
from urllib.parse import quote

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
    # Sep 2026, user's explicit ask: the admin-only "test" variant of
    # Production Confirmation (multi-Reporting-Point production models)
    # is now its own independently-grantable right instead of being
    # hardcoded to super_admin/admin only (see App.js's ProtectedRoute
    # and NavTabs.jsx's "Operation" dropdown). Reuses the SAME underlying
    # /api/production-confirmation/ endpoints as the main page - see
    # PAGE_ROUTE_RULES below, this key is added alongside
    # "production_confirmation" wherever that page's API calls are
    # gated, so a user with ONLY this test right isn't 403'd.
    {"key": "production_confirmation_test", "label": "Production Confirmation (Test)"},
    {"key": "inventory", "label": "Stock Overview"},
    {"key": "supplier_master", "label": "Supplier Master"},
    {"key": "quota_allocation", "label": "Quota Allocation"},
    {"key": "admin", "label": "Admin"},
    {"key": "admin_sap_write", "label": "Admin - SAP Write"},
    {"key": "admin_create_material", "label": "Admin - Create Material"},
    {"key": "admin_activate_material_site", "label": "Admin - Activate Material Site"},
    # Aug 2026 - Store Approval moved from unauthenticated/public to
    # requiring Entra ID login (user's explicit ask), so it now needs its
    # own grantable page permission like every other page.
    {"key": "store_approval", "label": "Store Goods Issue"},
    # Aug 27 2026, user's explicit ask: split out of "inventory" (Stock
    # Overview) into its own grantable right - previously anyone with
    # Stock Overview automatically also got Inter Plant Stock Transfer,
    # with no way to grant one without the other.
    {"key": "stock_transfer", "label": "Inter Plant Stock Transfer"},
    # Sep 3 2026, user's explicit ask: Inbound STO Receipt used to be
    # open to ANY logged-in user (no page key at all, see
    # PAGE_ROUTE_RULES/ProtectedRoute's `anyUser` - Aug 28 2026 decision,
    # "to start with") - now its own grantable right, separate from
    # Inter Plant Stock Transfer above.
    {"key": "inbound_stock_transfer", "label": "Inbound STO Receipt"},
    # Aug 2026: internal staff review/approval of external vendor
    # signups + (later phases) GRN approval for the new Supplier Portal.
    # Distinct from the Supplier Portal itself, which is NOT an Entra ID
    # page at all - see EXTERNAL_PORTAL_PATH_PREFIXES below.
    {"key": "supplier_portal_admin", "label": "Supplier Portal Approvals"},
    # Sep 9 2026, user's explicit ask: view-only access to the Supplier
    # Portal Approvals page (supplier list + GST/PAN/MSME/Bank documents)
    # WITHOUT the Approve/Reject actions, which stay gated behind
    # supplier_portal_admin specifically (see the explicit role checks in
    # post_admin_supplier_portal_approve/reject in server.py - the page
    # route rule below intentionally grants EITHER permission read access
    # to the whole prefix since it can't distinguish HTTP methods).
    {"key": "supplier_portal_documents", "label": "Supplier Additional Documents"},
    # Aug 2026: Purchase Order Creation automation - writes real POs into
    # SAP ByDesign via ManagePurchaseOrderIn. Kept separate from
    # purchasing_plan/supplier_master (planning/master-data pages, no
    # SAP write capability) since this one directly commits live data.
    {"key": "purchase_order", "label": "Purchase Order Creation"},
    # Sep 5 2026, user's explicit ask: split out of the single
    # "supplier_portal_admin"/"purchase_order" catch-alls above into their
    # own independently-grantable rights, same pattern as the earlier
    # stock_transfer/inbound_stock_transfer split. "Create Purchase Order"
    # (the write-to-SAP form) intentionally stays under "purchase_order"
    # above - user's explicit choice, since it's the more sensitive one.
    {"key": "supplier_portal_invite", "label": "Invite Supplier"},
    {"key": "vendor_goods_receipt", "label": "Vendor Goods Receipt"},
    {"key": "supplier_dashboard", "label": "Supplier Dashboard"},
    {"key": "created_purchase_orders", "label": "Created POs"},
    {"key": "open_purchase_orders", "label": "Open Purchase Orders"},
    # Sep 18 2026, user's explicit ask: lets an internal staff member
    # create a shipment on behalf of a supplier (and reset a supplier's
    # password) directly from the internal app - deliberately its OWN
    # grantable right, separate from "supplier_dashboard" above (which
    # is just a nav shortcut into the external JWT-authenticated portal
    # and still requires the supplier's own credentials to log in).
    {"key": "act_as_supplier", "label": "Act as Supplier"},
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
    # Aug 28 2026: the connection-status widget renders on EVERY page
    # (by design, see SapConnectionStatus.jsx) but was gated behind the
    # bom_explorer permission via the general /api/bom/ rule below - any
    # user without bom_explorer (e.g. GRN staff) got a false "SAP
    # Disconnected" 403. No specific page needed, just a valid session.
    ("/api/bom/connection-status", set()),
    ("/api/bom/", {"bom_explorer"}),
    ("/api/sap/cost-estimate-run", {"bom_explorer"}),
    ("/api/purchasing-plan/", {"purchasing_plan"}),
    ("/api/sales-plan", {"purchasing_plan"}),
    ("/api/part-suppliers/", {"purchasing_plan"}),
    ("/api/production-plan/", {"production_plan"}),
    ("/api/production-confirmation/", {"production_confirmation", "production_confirmation_test"}),
    ("/api/inventory", {"inventory"}),
    # Aug 27 2026, user's explicit ask: Inter Plant Stock Transfer is now
    # its own grantable right, separate from Stock Overview above.
    ("/api/stock-transfer", {"stock_transfer"}),
    # Sep 3 2026, user's explicit ask: see inbound_stock_transfer in
    # PAGE_CATALOG above.
    ("/api/inbound-receipts", {"inbound_stock_transfer"}),
    ("/api/suppliers/bulk-push-erp-to-sap", {"admin_sap_write"}),
    ("/api/suppliers/sap-price-specs", {"quota_allocation", "admin_sap_write"}),
    ("/api/admin/material-valuation", {"admin_sap_write"}),
    ("/api/suppliers/erp-prices/", {"quota_allocation"}),
    ("/api/suppliers/sap-purchase-history/", {"quota_allocation"}),
    ("/api/suppliers/sap-receipt-dates/", {"quota_allocation"}),
    ("/api/suppliers/sync-from-sap", {"supplier_master"}),
    ("/api/suppliers", {"supplier_master", "quota_allocation", "supplier_portal_admin", "supplier_portal_invite"}),
    ("/api/products/search", {"quota_allocation", "production_confirmation", "production_confirmation_test", "inventory", "stock_transfer"}),
    ("/api/quota-arrangements/", {"quota_allocation"}),
    ("/api/admin/components", {"admin"}),
    ("/api/admin/categories", {"admin"}),
    ("/api/admin/create-material", {"admin_create_material"}),
    ("/api/admin/delete-material", {"admin_create_material"}),
    # Bug fix (Aug 2026): /journal also backs MyStockRequestsTab, a
    # requester-side view embedded INSIDE the Production Confirmation page
    # (a production planner tracking their own submissions, not a store
    # person) - checked BEFORE the general /api/store-requests rule below
    # so a plain "user" with production_confirmation access (but no
    # store_approval access) isn't 403'd out of their own requests. Every
    # other /api/store-requests/* endpoint (queue, decision, issue, etc.)
    # still requires store_approval specifically.
    ("/api/store-requests/journal", {"store_approval", "production_confirmation", "production_confirmation_test"}),
    # Aug 27 2026: Inter Plant Stock Transfer's "Refresh Site Stock"
    # dropdown reuses this same site list - harmless read-only endpoint,
    # same pattern as the /journal override right above.
    # Sep 11 2026 bug fix, real incident (sap.p9@rampgroup.co.in - Site
    # Binding correctly saved "P9", but the Manual Return Site/Plant
    # dropdown stayed empty with zero error shown): this rule was never
    # updated when the Sep 9 2026 Return to Store workflow was added -
    # a "user" with ONLY production_confirmation (no store_approval) can
    # already reach the Return to Store tab (see /api/store-returns rule
    # below) and is correctly Site-Bound, but got a silent 403 on this
    # one site-list call specifically, since it didn't recognize
    # production_confirmation as a valid page for it yet.
    ("/api/store-requests/known-sites", {"store_approval", "stock_transfer", "production_confirmation", "production_confirmation_test"}),
    ("/api/store-requests", {"store_approval"}),
    # Sep 9 2026, user's explicit ask: Return to Store workflow - reuses
    # the SAME two existing page permissions rather than new grantable
    # rights (Production side creates/views its own returns under
    # production_confirmation, Store side works the pending queue under
    # store_approval - both need read access to shared endpoints like
    # /store-returns/{id}, so this single broad rule covers everything).
    ("/api/store-returns", {"production_confirmation", "store_approval"}),
    # Aug 2026: internal approval side of the new Supplier Portal
    # (Entra ID-authenticated staff, distinct from the JWT-authenticated
    # external supplier routes below). Both the vendor-onboarding
    # approvals AND the Phase 4 GRN approval screen share this same page
    # permission - one internal team, one page.
    ("/api/admin/supplier-portal/invites", {"supplier_portal_invite"}),
    ("/api/admin/supplier-portal", {"supplier_portal_admin", "supplier_portal_documents"}),
    ("/api/admin/grn", {"vendor_goods_receipt"}),
    # Aug 2026: Purchase Order Creation automation page.
    # Sep 5 2026, user's explicit ask: "Created POs" (history/list) and
    # "Open Purchase Orders" (vendor open-PO viewer) split out of
    # "purchase_order" into their own rights - checked BEFORE the general
    # /api/purchase-orders catch-all below so they don't also require the
    # (more sensitive) Create Purchase Order permission. suppliers/search
    # is shared by both the Create form and the Open PO viewer.
    ("/api/purchase-orders/history", {"created_purchase_orders"}),
    ("/api/purchase-orders/open", {"open_purchase_orders"}),
    ("/api/purchase-orders/suppliers/search", {"purchase_order", "open_purchase_orders"}),
    ("/api/purchase-orders", {"purchase_order"}),
    # Sep 14 2026, user's explicit ask: standalone "Service Purchase
    # Order" form - a full copy of the Create Purchase Order flow
    # (separate backend endpoints/collection, see server.py), but
    # deliberately reuses the SAME "purchase_order" permission per
    # user's choice rather than a new grantable right.
    ("/api/service-purchase-orders", {"purchase_order"}),
    # Sep 18 2026, user's explicit ask: internal "Act as Supplier" page.
    ("/api/admin/act-as-supplier", {"act_as_supplier"}),
]

# Paths the auth middleware never gates - login must stay reachable while
# logged out, and /auth/me must never itself 401 (the frontend uses it to
# find out WHETHER it's logged in).
PUBLIC_PATHS = {"/api/", "/api/auth/login", "/api/auth/callback", "/api/auth/me", "/api/version"}

# Aug 2026: the external Supplier Portal is a completely separate user
# base (vendors, not Azure AD identities) with its OWN JWT-based
# auth/session (see supplier_portal_service.py) - every route under this
# prefix enforces its own auth internally and must never be gated by the
# Entra ID middleware below (would 401 every request since suppliers
# have no `vms_session` cookie at all).
EXTERNAL_PORTAL_PATH_PREFIXES = ("/api/supplier-portal/",)

# Sep 18 2026, user's explicit ask: a public, no-login SAP integration
# reference page (see sap_integration_docs.py) - genuinely read-only
# static reference info, no PII/business data, meant to be shareable
# with SAP consultants who have no app account at all.
PUBLIC_API_PATH_PREFIXES = ("/api/public/",)


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

    if params.get("error"):
        # Entra can redirect back with an error/error_description instead of a code
        # (e.g. consent declined, conditional access block) - surface it directly in
        # the redirect (safe: no secrets/tokens in these fields) so this is diagnosable
        # without needing raw backend logs.
        reason = params.get("error_description") or params.get("error")
        logger.warning(f"Azure AD login: Entra returned an error before code exchange: {reason}")
        return RedirectResponse(f"/?auth_error=login_failed&auth_reason={quote(reason[:2000])}")

    try:
        result = _build_msal_app().acquire_token_by_auth_code_flow(saved["flow"], params)
    except Exception as e:
        # MSAL normally returns an {"error": ...} dict on failure (handled below) rather
        # than raising - but state/scope/transport problems can raise ValueError/
        # AssertionError/connection errors instead, which would otherwise surface as a
        # raw, unhelpful 500 to the user. Log the full traceback server-side and also
        # surface a safe summary via the redirect for diagnosis without log access.
        logger.exception(f"Azure AD login: acquire_token_by_auth_code_flow raised (state={state})")
        return RedirectResponse(f"/?auth_error=login_failed&auth_reason={quote(str(e)[:2000])}")
    if "error" in result:
        logger.warning(f"Azure AD login failed: {result.get('error')}: {result.get('error_description')}")
        reason = result.get("error_description") or result.get("error")
        return RedirectResponse(f"/?auth_error=login_failed&auth_reason={quote(reason[:2000])}")
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
    bound_sites = existing.get("bound_sites", []) if existing else []
    db[USERS_COLLECTION].update_one(
        {"_id": user_id},
        {
            "$set": {"tid": tid, "oid": oid, "email": email, "name": name, "role": role,
                      "allowed_pages": allowed_pages, "bound_sites": bound_sites, "last_login_at": now},
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
    # Aug 2026: "admin" is a new middle tier - full page access like
    # super_admin, but (enforced server-side in the /admin/users/{id}/
    # access endpoint, not here) can only ever assign the "user" role to
    # others, never "admin"/"super_admin".
    return {
        "authenticated": True,
        "email": user.get("email"),
        "name": user.get("name"),
        "role": role,
        "allowed_pages": sorted(PAGE_KEYS) if role in ("super_admin", "admin") else user.get("allowed_pages", []),
        "bound_sites": user.get("bound_sites", []),
        # Sep 17 2026, Manual GRN (No-Playwright) feature - a user-level
        # preference (self-service toggle on the GRN Approval page, see
        # PUT /api/admin/grn/my-preference) for whether their own approvals
        # go through the SAP UI automation (Playwright) or stop after the
        # SOAP Notification create and wait for staff to post the Goods
        # Receipt manually in SAP. `grn_blocked_shipment`/`grn_blocked_reason`
        # enforce the strict qty-match rule: a user who approved a manual GRN
        # that SAP later disagrees with (quantity mismatch) is blocked from
        # approving ANY new GRN until it's resolved or an admin overrides it.
        "manual_grn_preference": bool(user.get("manual_grn_preference", False)),
        "grn_blocked_shipment": user.get("grn_blocked_shipment"),
        "grn_blocked_reason": user.get("grn_blocked_reason"),
    }


def create_auth_middleware(db):
    """Factory (not a bare module-level function) so the middleware closes
    over `db` without a circular import back to server.py, where `db` is
    actually constructed."""

    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or not path.startswith("/api/")
            or path in PUBLIC_PATHS
            or path.startswith(EXTERNAL_PORTAL_PATH_PREFIXES)
            or path.startswith(PUBLIC_API_PATH_PREFIXES)
        ):
            return await call_next(request)

        user = await asyncio.to_thread(get_current_user, request, db)
        if not user:
            return JSONResponse({"detail": "Login required"}, status_code=401)

        required_pages = resolve_required_pages(path)
        if required_pages and user.get("role") not in ("super_admin", "admin"):
            if not (set(user.get("allowed_pages", [])) & required_pages):
                return JSONResponse({"detail": "Access denied"}, status_code=403)

        request.state.user = user
        return await call_next(request)

    return auth_middleware
