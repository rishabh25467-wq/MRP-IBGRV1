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

SAP's own "Event Notification" framework (subscribed to Business Object
"Site Logistics Lot", not "SiteLogisticsTask" - confirmed live that
object never gets created for STO Put Away on this tenant) pushes a
`sap.byd.SiteLogisticsLot.Root.Created.v1` CloudEvents payload to
server.py's `/api/webhooks/sap-put-away` the moment SAP creates one of
these - carrying the exact `entity-id` (ObjectID) this client reads via
`find_lot_by_object_id`. No polling anywhere in this chain."""
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

    def find_lot_by_object_id(self, lot_object_id: str) -> dict:
        """Returns {object_id, id, life_cycle_status_code, site_id,
        products: [str,...], activities: [{object_id, status_code}]}, or
        None if this ObjectID doesn't exist (a malformed/unrelated
        webhook payload)."""
        url = f"{self.endpoint}/SiteLogisticsLotSiteLogisticLotCollection"
        params = {
            "$filter": f"ObjectID eq '{lot_object_id}'",
            "$expand": "SiteLogisticsLotMaterialOutput,SiteLogisticsLotOperation/SiteLogisticsLotOperationActivity",
            "$format": "json",
            "sap-vhost": self.vhost,
        }
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryExecutionError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPInboundDeliveryExecutionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("d", {}).get("results", [])
        if not results:
            return None
        row = results[0]
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
