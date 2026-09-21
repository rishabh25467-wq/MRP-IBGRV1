# SAP Event Notification (push) rollout plan - for SAP Admin/Basis + whoever picks this up

Status as of Sep 22 2026: **groundwork built on our side, dormant. Nothing activated on SAP's
side yet - the Event Notification subscriber was deliberately left Inactive/unsaved.**

## Why this exists

Several pages in this app currently poll SAP for state changes instead of being told the instant
something happens:
1. GRN "Post GRN in SAP" - waits (poll, up to ~170s) for Put Away Task confirmation, Inbound
   Delivery # assignment, and the resulting Goods Movement.
2. STO "awaiting manual Goods Issue" - polls waiting for someone to post GI manually in SAP UI.
3. Supplier/Act-as-Supplier PO pages' Open Qty - background loop polls every 5 min, PLUS a live
   fetch on every page load (this live fetch was the main cause of a 49s page-load bug, since
   fixed on the DB side - see CHANGELOG.md).

SAP's own "Event Notification" framework (Application and User Management work center) can call
a custom HTTP endpoint the instant a subscribed Business Object changes, instead of us guessing
on a timer.

## What's already built on our side (dormant, safe, zero live traffic today)

- `POST /api/webhooks/sap-put-away` (`server.py`) - receiver endpoint. Validates HTTP Basic Auth
  (credentials in `backend/.env`: `SAP_WEBHOOK_USERNAME` / `SAP_WEBHOOK_PASSWORD`), logs the raw
  payload + headers into a new `sap_webhook_events` Mongo collection, acks with `{"status":
  "received"}` / HTTP 200. Does NOT trigger any action yet - purely for inspecting the real
  payload shape once SAP actually sends one (SAP's own docs don't spell this out precisely).
- `auth_service.py`: `EXTERNAL_WEBHOOK_PATH_PREFIXES = ("/api/webhooks/",)` added to the session-
  auth middleware's bypass list, so SAP's calls (which use their own Basic Auth, not our
  `vms_session` cookie) reach the route at all.

## What SAP Admin/Basis needs to do (when ready to actually activate this)

1. In **Application and User Management** work center, go to **Event Notification**.
2. Create/edit the subscriber (user already started one named `INB-EMERGENT`).
3. **Business Object**: search for and select `SiteLogisticsTask` (namespace
   `http://sap.com/xi/AP/LogisticsExecution/Global`) - confirmed via SAP's own documentation to
   be the correct object backing Put Away Task confirmation (same object our app already writes
   to via the `ManageSiteLogisticsTaskIn` SOAP service).
4. **Business Object Node**: pick the node under it, subscribe to the **"Updated"** event (not
   Created/Deleted) - this is what fires when a task's status changes to confirmed.
5. **Endpoint**: the public webhook URL, e.g. `https://<your-production-domain>/api/webhooks/
   sap-put-away` (use the REAL production URL when this goes live, not a preview URL - preview
   URLs are not guaranteed stable).
6. **Authentication Method**: choose **Basic Authentication**, enter the credentials from
   `backend/.env` (`SAP_WEBHOOK_USERNAME` / `SAP_WEBHOOK_PASSWORD` - rotate these before sharing
   with SAP Admin if they've been exposed anywhere insecure).
7. Activate the subscriber.

## What we (app side) still need to do once step 7 happens

1. Have SAP Admin trigger one real Put Away confirmation, then inspect the logged row in
   `sap_webhook_events` (`db.sap_webhook_events.find().sort("received_at", -1).limit(1)`) to see
   the REAL payload shape - do NOT guess this blind.
2. Once the shape is known, extend `sap_put_away_webhook` to actually parse it (identify which
   PO/shipment the event refers to) and short-circuit `_auto_finish_full_auto_grn`'s poll loop
   for that shipment (e.g. set an event/flag the loop checks, or directly run one check-now cycle
   immediately) instead of waiting for the next scheduled tick.
3. Decide whether to keep the poll loop running as a slower safety net (recommended - webhooks
   can occasionally fail to fire or get lost) or remove it once the webhook is proven reliable.

## Bigger picture - NOT started, future scope if this proves valuable

The same framework could later cover:
- STO manual Goods Issue wait (subscribe to the relevant Outbound Delivery/GI Business Object).
- Open PO Quantity (subscribe to `PurchaseOrder`/Goods Receipt-related objects) - would let us
  remove BOTH the 5-min background refresh loop and the live per-page-load fetch entirely.
- Inventory cache (30-min staleness today) and PO structural/PO-number caches (5-10 min loops) -
  lower urgency, but would cut background SAP call volume, which also reduces contention on the
  shared `SAP_MAX_CONCURRENT_REQUESTS=3` tenant-wide limiter.

None of this is started. Each would need its own Business Object identified (same research
process as `SiteLogisticsTask` above) before building.
