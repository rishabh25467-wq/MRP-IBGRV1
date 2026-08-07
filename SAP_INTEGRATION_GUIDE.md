# SAP Business ByDesign Integration Guide
(Reference doc — everything this app connects to in SAP, so you can wire up another app the same way)

Tenant: `my431827.businessbydesign.cloud.sap`

---

## 1. Connection types used

| # | Service | Protocol | Purpose |
|---|---------|----------|---------|
| 1 | Production BOM Query | SOAP | Full multi-level BOM explosion |
| 2 | Query Materials | SOAP | Material ID → Material UUID lookup (no BOM relationship needed) |
| 3 | Material Valuation Data | OData (custom) | Standard Cost per material |
| 4 | Material LT/MSL (Supply Planning) | OData (custom) | Read + WRITE Safety Stock (MSL) & Procurement Lead Time |
| 5 | On-Hand Inventory (SCMINVV02) | OData Analytics report | Current stock quantity per material |

All 5 are exposed via SAP **Communication Arrangements** (Application and User Management → Communication Arrangements), each tied to a technical/business user.

---

## 2. Credentials / users

Two separate SAP users are used (different services require different user *types* in ByDesign):

| Env var | Value | User type | Used for |
|---|---|---|---|
| `SAP_SOAP_USERNAME` | `_EMERGENTBOM` | Technical (Communication System) user | Services #1, #2 (SOAP) |
| `SAP_SOAP_PASSWORD` | `Admin@1136` | | |
| `SAP_ODATA_USERNAME` | `UNEECOPSTEAM` | Business user (Work Center role) | Services #3, #4, #5 (OData) — technical users can't hold Business Roles in this tenant, so OData custom services needed a real business-user login instead |
| `SAP_ODATA_PASSWORD` | `UAdmin@11334` | | |
| `SAP_INSTANCE_URL` | `https://my431827.businessbydesign.cloud.sap` | — | Base tenant URL (host reference only, not directly called) |
| `SAP_USERNAME`/`SAP_PASSWORD` | `itadmin`/`Admin@1136` | Admin login | SAP UI admin login (not used by app code — for making tenant config changes in the SAP browser UI) |

Both SOAP + OData calls use plain **HTTP Basic Auth** with the respective user above.

---

## 3. Service #1 — Production BOM Query (SOAP)

- **Communication scenario:** "Production Bill of Materials Query" (or similarly named — the one that exposes `QueryProductionBillofMaterialsIn`)
- **Endpoint:**
  `https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/queryproductionbillofmaterials?sap-vhost=my431827.businessbydesign.cloud.sap`
- **Operation:** `QueryProductionBillofMaterialsIn` / `FindByElements`, two selection modes:
  - `SelectionByProductionBillOfMaterialID` — resolve by a known BOM ID (root lookup)
  - `SelectionByOutputProductID` — resolve by a bare Product/Material ID (auto-resolves to its current BOM revision)
- **Recursion:** the response gives you one level (groups → items). To get a full multi-level tree, recursively call `SelectionByOutputProductID` for every item that itself has a sub-BOM (BFS, our code caps at `MAX_DEPTH=6` with cycle detection).
- **Empirical gotchas (all found the hard way against this exact tenant — will very likely apply to any ByDesign tenant):**
  1. `SelectionByOutputProductID` can return **multiple BOM revisions** for the same product (old superseded + current). Always pick by:
     - Prefer `ConsistencyStatus = 3` ("Consistent"/released) over merely the highest numeric revision suffix (a "Check Pending" revision can otherwise get wrongly picked).
     - Among same-consistency candidates, pick the highest numeric revision suffix (handles both `_2` integer and `_4.4` decimal suffix formats — parse as float).
  2. A single line item (`ItemGroupItem`) can carry **multiple `ProductionBillOfMaterialItemGroupChangeState` entries** (its own mini revision history) — always take the fields from the highest-revision ChangeState, not the first one in the XML.
  3. To get the item's real Material ID (not just the ObjectID), there's an empirically-verified offset: `change_state_object_id = item_object_id_int + 0x4000` — used to batch-lookup item details via chunked OData `$filter` queries against the sibling read service, matched back by ObjectID (not by ECO name/position — that approach silently drops items when a sub-item references a different ECO than its parent).
  4. Every BOM response also carries the root product's own `<ProductUUID>` (useful even for products that are themselves a BOM root, not just leaf items).
  5. Genuine **alternate BOMs** (same output, different real recipe — e.g. different raw material grade) show up as multiple *Consistent* revisions with genuinely different input product sets. Detect by fingerprinting the set of input product IDs per revision; revisions sharing a fingerprint = normal revision chain (collapse to latest); different fingerprint = a real alternate (surface both, don't silently pick one).
  6. Transient SOAP timeouts are common under concurrent load — always retry (3 attempts w/ backoff) before treating a sub-BOM as missing.

---

## 4. Service #2 — Query Materials (SOAP)

- **Communication scenario:** "Query Materials" (**separate** Communication Arrangement from #1, even though same technical user/system can be reused — in this tenant it needed its own arrangement, not just an added authorization on the existing one).
- **Endpoint:**
  `https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/querymaterialin?sap-vhost=my431827.businessbydesign.cloud.sap`
- **Operation:** `MaterialByElementsQuery_sync` (`QueryMaterialIn` / `FindByElements`), namespace `http://sap.com/xi/SAPGlobal20/Global`
- **Purpose:** direct Material ID → Material UUID resolution, with **no BOM relationship required** — resolves pure raw materials that never appear as a BOM leaf/root anywhere. This was the key to closing the last gap in Standard-Cost coverage.
- **Gotcha:** if you see fault string `"Authorization role missing for service 'QueryMaterialIn', operation 'FindByElements'"` — this is a Communication Arrangement/authorization issue on the SAP side, not a code bug. Fix: Communication Arrangements → activate/create the "Query Materials" scenario → link to your technical user → ensure its Business Role includes the `QueryMaterialIn`/`FindByElements` authorization.

---

## 5. Service #3 — Material Valuation Data (OData, Standard Cost)

- **Custom OData service**, Business Object `MaterialValuationData`.
- **Base URL:** `https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/materialvaluationdata`
- **Chain (3 calls):**
  1. `Product UUID` → query `ValuationLevel` entity filtered by `MaterialUUID eq '<uuid>'` → get its `ObjectID`
  2. Convert that `ObjectID` to dashed-UUID format → this is the `ValuationLevelUUID`
  3. Query `ValuationPrice` entity filtered by `ValuationLevelUUID eq '<uuid>'` → pick the record whose date range currently covers "now" → that's the live Standard Cost (`amount` + `currency`)
- Batch multiple product UUIDs per call for efficiency; treat a single-batch failure as "no cost for this batch" (best-effort — don't let one bad batch kill the whole valuation pull).

---

## 6. Service #4 — Material LT/MSL / Supply Planning (OData, read + WRITE)

- **Custom OData service**, entity `MaterialSupplyPlanningProcessInformation`, Work Center View `PMM_MATERIALS`.
- **Base URL:** `https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/materialltmsl`
- **Fields:** `SafetyStockQuantity` (= MSL) and `PlannedDeliveryDuration` (= Procurement Lead Time, ISO-8601 `"PnD"` string format, e.g. `"P7D"` = 7 days).
- **Read:** simple GET filtered by Material UUID — returns one row **per Supply Planning Area** (a material can have several, e.g. P1/P2/P3/P4/P6/P7/P9 in this tenant). We push the same value to ALL of a material's planning-area rows (no single "canonical" site in this tenant).
- **Write (PATCH) — this is the tricky one:**
  1. `GET` with header `x-csrf-token: fetch` → capture the returned CSRF token AND the session cookie from the response.
  2. `PATCH` the specific row's URL, sending back the same CSRF token header + session cookie, body = only the field(s) changing.
  3. SAP will occasionally return **"Locking object not possible"** if the parent Material is momentarily locked by another write — this is transient, not a real error. Retry with backoff (we use 4 attempts).
  4. If pushing to many materials in bulk, keep concurrency modest (we use 5 parallel workers) — safe across *different* materials, since the lock only contends on the *same* material.

---

## 7. Service #5 — On-Hand Inventory (OData Analytics report, SCMINVV02)

- **Custom Analytics OData report** built on the standard `SCMINVV02` ("On-Hand Inventory") data source.
- **URL:** `https://my431827.businessbydesign.cloud.sap/sap/byd/odata/ana_businessanalytics_analytics.svc/RPZ76B8273872353FC8677FCFQueryResults`
  (this exact path is tenant/report-specific — you'll get a different report ID if you build your own copy of this report in your tenant)
- **CRITICAL gotcha (silently corrupts data if missed):** this report's `$select` parameter changes which characteristics the SAP OLAP engine aggregates by. If you request only a subset of fields (e.g. `$select=CMATERIAL_UUID,KCON_HAND_STOCK`), `CMATERIAL_UUID` comes back as an **internal numeric surrogate key** (e.g. `'430'`) instead of the real Material ID. You must fetch the **full, un-`$select`'d row shape** (all ~35 default characteristics) to get the correct business Material ID (e.g. `'SI-0038C-2'`) back in that same field. Despite the field being named `CMATERIAL_UUID`, it is actually the **Material ID** (`product_id`), NOT a GUID.
- **Pagination:** `$top=5000` + `$skip=` loop (this tenant has ~5,746 rows / ~3,165 distinct materials).
- Sum `KCON_HAND_STOCK` per material across every other implicit dimension (site/logistics area/stock status) to get one company-wide on-hand total.

---

## 8. Suggested build order for a new app

1. Get SAP admin to activate the same (or equivalent) Communication Arrangements above, reusing/mirroring the technical + business users if you have access to the same tenant, or creating fresh ones scoped to just what the new app needs.
2. Start with whichever service the new app actually needs first — they're fully independent of each other.
3. For BOM explosion (#1): budget for the revision-picking and ChangeState gotchas above up front — they are NOT edge cases, they show up on real production data immediately.
4. For inventory (#5): NEVER use `$select` on this report — most common integration mistake, and it fails silently (wrong-looking-right values), not with an error.
5. For any write-capable OData service (#4-style): always do the CSRF fetch + cookie dance, always retry on "Locking object not possible".
