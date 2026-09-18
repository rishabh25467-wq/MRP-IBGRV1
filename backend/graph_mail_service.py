"""Microsoft Graph email sending (Sep 2 2026) - Supplier Portal invite
emails. Reuses the SAME Azure AD app registration already used for Entra
ID SSO login (auth_service.py) - only needs the additional Application
-level "Mail.Send" permission + admin consent granted in the Azure
Portal (user is doing this themselves), no new secret/env var needed.

Sends AS the inviting staff member's own mailbox (user's explicit ask:
"Staff members", not one fixed shared mailbox) via app-only
POST /users/{upn}/sendMail - Application Mail.Send can send as any real
mailbox in the tenant, so the currently signed-in admin's own Entra ID
email (already known from their session) is used as the sender."""
import logging
import os

import httpx
import msal

logger = logging.getLogger(__name__)

AZURE_AD_TENANT_ID = os.environ["AZURE_AD_TENANT_ID"]
AZURE_AD_CLIENT_ID = os.environ["AZURE_AD_CLIENT_ID"]
AZURE_AD_CLIENT_SECRET = os.environ["AZURE_AD_CLIENT_SECRET"]
AUTHORITY = f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}"

_msal_app = msal.ConfidentialClientApplication(
    AZURE_AD_CLIENT_ID, authority=AUTHORITY, client_credential=AZURE_AD_CLIENT_SECRET,
)


def _graph_token() -> str:
    result = _msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Could not acquire Microsoft Graph token: {result.get('error_description') or result.get('error')}")
    return result["access_token"]


async def send_supplier_invite(sender_upn: str, to_email: str, company_name: str, vendor_code: str, signup_url: str) -> None:
    token = _graph_token()
    payload = {
        "message": {
            "subject": "You're invited to the Materials Hub Supplier Portal",
            "body": {
                "contentType": "HTML",
                "content": f"""
                    <p>Hello,</p>
                    <p><b>{company_name}</b> has been invited to join the Materials Hub Supplier Portal, where you can view your Open Purchase Orders and submit shipments directly.</p>
                    <p><b>Your Vendor Code:</b> {vendor_code}</p>
                    <p><a href="{signup_url}">Click here to sign up</a> - enter this Vendor Code along with your company details, GST and PAN documents.</p>
                    <p>Once your account is reviewed and approved, you'll be able to log in and get started.</p>
                """,
            },
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": True,
    }
    url = f"https://graph.microsoft.com/v1.0/users/{sender_upn}/sendMail"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
    if resp.status_code != 202:
        logger.error(f"Graph sendMail failed (HTTP {resp.status_code}) for invite to {to_email}: {resp.text[:500]}")
        raise RuntimeError(f"Graph sendMail failed with HTTP {resp.status_code}: {resp.text[:300]}")


async def send_supplier_password_reset(sender_upn: str, to_email: str, company_name: str, new_password: str) -> None:
    """Sep 18 2026, Act as Supplier feature - notifies a supplier of a
    password an internal staff member just set for their account."""
    token = _graph_token()
    payload = {
        "message": {
            "subject": "Your Materials Hub Supplier Portal password has been reset",
            "body": {
                "contentType": "HTML",
                "content": f"""
                    <p>Hello,</p>
                    <p>Your password for <b>{company_name}</b>'s Materials Hub Supplier Portal account has been reset by our team.</p>
                    <p><b>Your new password:</b> {new_password}</p>
                    <p>Please log in and change this password if you'd like to choose your own.</p>
                """,
            },
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": True,
    }
    url = f"https://graph.microsoft.com/v1.0/users/{sender_upn}/sendMail"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
    if resp.status_code != 202:
        logger.error(f"Graph sendMail failed (HTTP {resp.status_code}) for password reset to {to_email}: {resp.text[:500]}")
        raise RuntimeError(f"Graph sendMail failed with HTTP {resp.status_code}: {resp.text[:300]}")
