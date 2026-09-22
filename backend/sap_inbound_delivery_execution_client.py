"""SAP Business ByDesign custom OData service `khinbounddeliveryexecution`
(Sep 22 2026) - the piece that finally closes the automated STO Goods
Receipt gap `sap_inbound_delivery_client.py` documents as dead
(PGRBackground/direct GR posting is blocked tenant-wide per SAP KBA
3583076).

Site P1 is a "Task-Supported Warehouse": Releasing an Inbound Delivery
Notification there doesn't post a Goods Receipt directly - SAP spawns a
Warehouse Request -> Warehouse Order (`SiteLogisticsLot`, UI: "Warehouse
Order") -> Operation -> Operation Activity ("Put Away Task") chain
instead, and the actual receipt only lands once that Activity is
confirmed via the `ConfirmAsPlanned` action - live-tested this session
(STO-000111/P8D1-239): real inventory landed in P1-HOLD only after this
call, not just from Release alone.

This service was hand-built via SAP's OData Service Explorer (not a
stock SAP service) - see /app/memory/sap_kh_custom_odata_findings.md
and /app/memory/sap_team_request_inbound_delivery_execution_stage.md
for the full investigation. `ProductID` on `SiteLogisticsLotMaterialOutput`
was added live this session (the service originally didn't expose it) -
required so `inbound_receipt_service.complete_automated_receipt_for_lot`
can match a newly-created Warehouse Order back to the STO awaiting it.

Sep 2026, user's explicit architectural mandate: rejected the Event
Notification webhook + 20-min safety-sweep design entirely (SAP's own
Warehouse Order creation can lag 2-8 minutes, which the user does not
want masked by any async wait/poll/background job). `find_recent_lots`
below is the replacement - a single, immediate, un-retried lookup
called synchronously right after Release inside
inbound_receipt_service.start_automated_receipt. If SAP hasn't created
the Warehouse Order yet at that exact moment, the receive request fails
immediately and the user retries manually - no polling anywhere."""
import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore


class SAPInboundDeliveryExecutionError(Exception):
    pass


class SAPInboundDeliveryExecutionClient:
    def __init__(self, endpoint: str, username: str, password: str, vhost: str):
        self.endpoint = endpoint.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.vhost = vhost

    _EXPAND = "SiteLogisticsLotMaterialOutput,SiteLogisticsLotOperation/SiteLogisticsLotOperationActivity"

    def find_lot_by_object_id(self, lot_object_id: str) -> dict:
        """Returns {object_id, id, life_cycle_status_code, site_id,
        products: [str,...], activities: [{object_id, status_code}]}, or
        None if this ObjectID doesn't exist."""
        url = f"{self.endpoint}/SiteLogisticsLotSiteLogisticLotCollection"
        params = {
            "$filter": f"ObjectID eq '{lot_object_id}'",
            "$expand": self._EXPAND,
            "$format": "json",
            "sap-vhost": self.vhost,
        }
        results = self._query(url, params)
        return self._parse_lot_row(results[0]) if results else None

    def find_recent_lots(self, limit: int = 50) -> list:
        """Sep 2026, user's explicit architectural mandate (zero polling/
        webhook/background sweep) - a SINGLE, immediate, un-retried
        lookup at Warehouse Orders SAP has created, called right after
        Release, so the caller (inbound_receipt_service.
        start_automated_receipt) can try to match the one SAP just
        created for THIS delivery in the same synchronous request. No
        ObjectID filter needed/possible since it isn't known yet -
        caller matches by site_id + product set. No $orderby - this
        custom OData service doesn't expose SystemAdministrativeData
        for sorting (confirmed live: "Property SystemAdministrativeData
        not found in type SiteLogisticsLotSiteLogisticLot"), so this
        relies on SAP's own default result order plus a generous $top.
        Returns [] (never raises past the caller) if SAP hasn't created
        it yet - the caller treats that as an immediate failure, not
        something to wait/retry for."""
        url = f"{self.endpoint}/SiteLogisticsLotSiteLogisticLotCollection"
        params = {
            "$top": str(limit),
            "$expand": self._EXPAND,
            "$format": "json",
            "sap-vhost": self.vhost,
        }
        return [self._parse_lot_row(row) for row in self._query(url, params)]

    def _query(self, url: str, params: dict) -> list:
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryExecutionError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPInboundDeliveryExecutionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("d", {}).get("results", [])

    def _parse_lot_row(self, row: dict) -> dict:
        outputs = self._as_list(row.get("SiteLogisticsLotMaterialOutput"))
        products = sorted({o.get("ProductID") for o in outputs if o.get("ProductID")})
        site_ids = {o.get("SiteID") for o in outputs if o.get("SiteID")}
        site_id = next(iter(site_ids), None)
        activities = []
        for op in self._as_list(row.get("SiteLogisticsLotOperation")):
            for act in self._as_list(op.get("SiteLogisticsLotOperationActivity")):
                activities.append({"object_id": act.get("ObjectID"), "status_code": act.get("ActivityProcessingStatusCode")})
        return {
            "object_id": row.get("ObjectID"), "id": row.get("ID"),
            "life_cycle_status_code": row.get("LifeCycleStatusCode"),
            "site_id": site_id, "products": products, "activities": activities,
        }

    def confirm_activity_as_planned(self, activity_object_id: str) -> None:
        """SiteLogisticsLotOperationActivityConfirmAsPlanned (Function
        Import) - confirms the Put Away task at its full planned
        quantity, which is what actually posts the Goods Receipt (live-
        tested Sep 22 2026: real stock landed in {SITE}-HOLD right after
        this call, matching the Notification's own Planned/Open
        quantity - there's no quantity-override parameter, "as planned"
        means exactly that)."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, "SiteLogisticsLotOperationActivityCollection")
                resp = session.post(
                    f"{self.endpoint}/SiteLogisticsLotOperationActivityConfirmAsPlanned",
                    params={"ObjectID": f"'{activity_object_id}'", "sap-vhost": self.vhost},
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryExecutionError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPInboundDeliveryExecutionError(f"HTTP {resp.status_code}: {resp.text[:500]}")

    def _fetch_csrf_token(self, session: requests.Session, entity_set: str) -> str:
        resp = session.get(
            f"{self.endpoint}/{entity_set}",
            params={"$top": "1", "sap-vhost": self.vhost},
            auth=self.auth,
            headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
            timeout=30,
        )
        token = resp.headers.get("x-csrf-token")
        if not token:
            raise SAPInboundDeliveryExecutionError("SAP did not return a CSRF token - cannot confirm the Put Away task.")
        return token

    @staticmethod
    def _as_list(value) -> list:
        if isinstance(value, dict):
            return value.get("results") or []
        return value or []
