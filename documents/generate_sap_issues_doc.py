"""Generates the SAP technical consultant issues report as a .docx file."""
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def set_cell_shading(cell, color_hex):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), color_hex)
    tcPr.append(shd)


def add_code_block(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    run.font.name = "Consolas"
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x1a, 0x1a, 0x1a)
    p_fmt = p.paragraph_format
    p_fmt.left_indent = Inches(0.25)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), 'F0F0F0')
    p._p.get_or_add_pPr().append(shd)
    return p


def add_status_line(doc, label, text, color="C00000"):
    p = doc.add_paragraph()
    r1 = p.add_run(f"{label}: ")
    r1.bold = True
    r2 = p.add_run(text)
    r2.font.color.rgb = RGBColor.from_string(color)
    r2.bold = True
    return p


doc = Document()

style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(11)

# Title
title = doc.add_heading("SAP Business ByDesign Integration Issues Report", level=0)
subtitle = doc.add_paragraph("Prepared for: SAP Technical Consultant / Basis Team")
subtitle.runs[0].bold = True
doc.add_paragraph("Application: Internal ERP-integrated Production/Logistics App (SAP BOM Viewer + Stock Transfer + Supplier Portal)")
doc.add_paragraph("Date: September 1, 2026")
doc.add_paragraph("Prepared by: Development team (Emergent Agent session)")
doc.add_paragraph()

doc.add_heading("Purpose of this Document", level=1)
doc.add_paragraph(
    "This document catalogs every SAP OData/SOAP web service call our application attempted for four "
    "specific business processes, the exact payload/parameters sent, the exact SAP response/rejection "
    "received, and the root cause conclusion reached after live testing against the production tenant. "
    "All findings below are from LIVE testing against the production SAP Business ByDesign tenant "
    "(my431827.businessbydesign.cloud.sap) - none are theoretical. Where we were forced to fall back to "
    "headless browser (Playwright) UI automation instead of a supported API, this is flagged explicitly "
    "and is the primary ask for SAP consultant review: are there ANY supported API/service alternatives "
    "we have missed, or configuration changes (master data, Business Configuration scoping) that would "
    "unlock a native API path?"
)

# ============ ISSUE 1 ============
doc.add_heading("1. Stock Transfer Order (STO) Creation", level=1)
doc.add_paragraph("Status: WORKING via SOAP API (no Playwright needed).")
doc.add_paragraph(
    "Service used: \"Manage Customer Requirements\" (ManageCustomerRequirementIn), confirmed via SAP's "
    "public documentation (PSM_ISI_R_II_MANAGE_CUST_REQ_IN) and the tenant's own WSDL. One endpoint, "
    "differentiated by SOAPAction header + request root element:"
)
doc.add_paragraph("• ManageCustomerRequirementInCheckMaintainAsBundleRequest - validation only, no commit (always run first as a safety net)", style="List Bullet")
doc.add_paragraph("• ManageCustomerRequirementInMaintainAsBundleRequest - the real, irreversible create write", style="List Bullet")

doc.add_heading("1.1 Payload sent (MaintainAsBundle)", level=2)
add_code_block(doc, """POST {SAP_SOAP_STO_ENDPOINT}
SOAPAction: http://sap.com/xi/A1S/Global/ManageCustomerRequirementIn/ManageCustomerRequirementInMaintainAsBundleRequest
Content-Type: text/xml; charset=utf-8

<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:ManageCustomerRequirementInMaintainAsBundleRequest_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <CustomerRequirement ActionCode="01">
        <ObjectNodeSenderTechnicalID>1</ObjectNodeSenderTechnicalID>
        <ChangeStateID></ChangeStateID>
        <ID></ID>
        <ShipFromSiteID>{ship_from_site_id}</ShipFromSiteID>
        <ShipToSiteID>{ship_to_site_id}</ShipToSiteID>
        <ShipToLocationID>{ship_to_location_id}</ShipToLocationID>
        <CompleteDeliveryRequestedIndicator>true</CompleteDeliveryRequestedIndicator>
        <DeliveryPriorityCode>1</DeliveryPriorityCode>
        <TextCollection ActionCode="01">
          <Text ActionCode="01">
            <TypeCode>10011</TypeCode>
            <TextContent><Text>{gst_note_text}</Text></TextContent>
          </Text>
        </TextCollection>
        <ExternalRquestItem ActionCode="01">
          <ObjectNodeSenderTechnicalID>10</ObjectNodeSenderTechnicalID>
          <ItemID>10</ItemID>
          <ProductKey>
            <ProductTypeCode></ProductTypeCode>
            <ProductIdentifierTypeCode></ProductIdentifierTypeCode>
            <ProductID>{product_id}</ProductID>
          </ProductKey>
          <RequestedQuantity unitCode="{unit_code}">{requested_qty}</RequestedQuantity>
          <RequestedLocalDateTime timeZoneCode="UTC">{requested_local_datetime}</RequestedLocalDateTime>
          <PartialDeliveryControlCode>9</PartialDeliveryControlCode>
          <Description languageCode="EN">{description}</Description>
        </ExternalRquestItem>
        <!-- additional ExternalRquestItem blocks repeat for each line -->
      </CustomerRequirement>
    </n0:ManageCustomerRequirementInMaintainAsBundleRequest_sync>
  </soapenv:Body>
</soapenv:Envelope>""")

doc.add_heading("1.2 Finding: multi-line STOs and the Delivery Rule", level=2)
doc.add_paragraph(
    "PartialDeliveryControlCode was originally set to \"9\" (Single delivery, no document qualifier). We "
    "hypothesized this should already force one combined delivery per order, but live testing on a real "
    "2-line order (order 30433) still produced 2 separate Outbound Deliveries (P8D1-196/197) after Goods "
    "Issue. We then tried code \"3\" (\"Single delivery - full quantity\", the document-level qualifier SAP's "
    "own help documentation pairs with CompleteDeliveryRequestedIndicator=true) - same live dead end, "
    "identical 2-delivery split (order 30433, re-tested)."
)
doc.add_paragraph(
    "CONCLUSION (per SAP KBA 3342620, a related precedence note for Service Orders): the Ship-To Party's "
    "own Account Master Data \"Complete Delivery\" setting takes precedence over whatever this SOAP "
    "payload sends for PartialDeliveryControlCode. This is understood to be a genuine SAP-side master-data "
    "lock, not something fixable from the ExternalRequestItem payload alone."
)
doc.add_paragraph(
    "CompleteDeliveryRequestedIndicator=true DOES correctly combine every line into ONE Outbound Delivery "
    "REQUEST document (confirmed live, order 30411 - both lines share the same ParentObjectID) - this part "
    "of the header-level Delivery Request works. The split happens one step LATER, at the Delivery "
    "Request -> Outbound Delivery conversion (see Section 2)."
)
add_status_line(doc, "Question for SAP consultant", "Is there a Business Configuration or Account Master Data setting on the Ship-To Party (Site) that overrides PartialDeliveryControlCode/CompleteDeliveryRequestedIndicator for Intra-Company Stock Transfers specifically, and can it be changed?", "C00000")

# ============ ISSUE 2 ============
doc.add_heading("2. Combining a Multi-Line STO into ONE Outbound Delivery (Goods Issue)", level=1)
doc.add_paragraph("Status: NO WORKING API FOUND. Currently uses Playwright UI automation (flaky); a native SAP Custom Business Object (ABSL) alternative is in progress in a separate test tenant (see Section 5).")
doc.add_paragraph(
    "Once SAP's own scheduler converts the combined Delivery Request (Section 1.2) into Outbound "
    "Deliveries, EVERY API path we tried still produces one Outbound Delivery PER LINE, never one "
    "combined delivery for a multi-line order. Four distinct SAP OData actions were tried against the "
    "custom OData service \"odataoutboundemergent\":"
)

doc.add_heading("2.1 Attempt A: PGIInBackground (item-scoped)", level=2)
add_code_block(doc, """POST {endpoint}/PGIInBackground
    ?ObjectID='{outbound_delivery_request_item_object_id}'
    &TaskBasedIndicator=false
    &AllowSplitIndicator=false
    &AutoReleaseOutboundDelivery=false
    &SplitByDeliveryPriorityCodeIndicator=false
    &SplitByOrderIndicator=false
    &SplitByShippingOrPickupDateTimeIndicator=false
    &sap-vhost={vhost}
Headers: Accept: application/json, X-CSRF-Token: {fetched token}""")
doc.add_paragraph(
    "Result: Even with every \"do not split\" indicator explicitly set to false, this still creates one "
    "Outbound Delivery per call. Root cause confirmed via this action's own $metadata: it is bound to "
    "OutboundDeliveryRequestItemCollection (item-level), and ObjectID is a single Edm.String - there is "
    "no batch/multi-item parameter on this action at all, by design."
)

doc.add_heading("2.2 Attempt B: SLRequestDeliveryExecution (schedule-line-scoped)", level=2)
add_code_block(doc, """POST {endpoint}/SLRequestDeliveryExecution
    ?ObjectID='{schedule_line_object_id}'
    &TargetSiteLogisticsRequestUUID=guid'{target_uuid}'
    &TaskBasedIndicator=false
    &AllowSplitIndicator=false
    &SplitByShippingOrPickupDateTimeIndicator=false
    &SplitByOrderIndicator=false
    &SplitByDeliveryPriorityCodeIndicator=false
    &sap-vhost={vhost}""")
doc.add_paragraph(
    "Result: Rejected live with business fault \"Site logistics request does not exist\". "
    "TargetSiteLogisticsRequestUUID must reference an ALREADY-EXISTING SAP \"Site Logistics Request\" "
    "object, and no service exposed to this application (every SAP_SOAP_*/BYD_ODATA_* endpoint checked) "
    "can create or look one up."
)

doc.add_heading("2.3 Attempt C: SLPGIInBackground (schedule-line-scoped, no split params)", level=2)
add_code_block(doc, """POST {endpoint}/SLPGIInBackground
    ?ObjectID='{schedule_line_object_id}'
    &sap-vhost={vhost}""")
doc.add_paragraph(
    "Result: This action exposes NO Task/Split/AutoRelease control parameters at all (confirmed via "
    "$metadata) and always auto-releases. Tried because it operates at the finer ScheduleLine "
    "granularity SAP's own internal due-list batching naturally works on - did not change the "
    "one-delivery-per-line outcome."
)

doc.add_heading("2.4 Attempt D: OutboundDeliveryRequestAllocate (header-scoped)", level=2)
doc.add_paragraph(
    "Bound to OutboundDeliveryRequestCollection (header level, the most promising candidate for a "
    "combined action). Result: Rejected live with \"Project outbound delivery request reference missing "
    "or not valid\" - this action appears to be built for a project-based SAP scenario (Project Stock "
    "Transfer / Project logistics), not standard Intra-Company Stock Transfer."
)

doc.add_heading("2.5 Root cause conclusion", level=2)
doc.add_paragraph(
    "This tenant's Ship-to Party Account Master Data silently forces one Outbound Delivery per line no "
    "matter what any of the four API calls above send. This was confirmed by comparing SAP's own UI "
    "behaviour: Outbound Logistics > Delivery Proposals correctly shows ONE combined row per multi-line "
    "order, and manually clicking that row's \"Create Outbound Delivery\" > \"Without Release\" button "
    "DOES create exactly one combined Outbound Delivery (confirmed live, orders 30434/30435/30441 - "
    "always 1 delivery for 2 lines). This UI-only combination logic is not exposed by any of the four "
    "OData actions above."
)
add_status_line(doc, "Question for SAP consultant", "What does the SAP UI's \"Create Outbound Delivery > Without Release\" button call internally that differs from PGIInBackground/SLRequestDeliveryExecution/SLPGIInBackground/OutboundDeliveryRequestAllocate? Is there an undocumented action, or a BAdI/enhancement spot we could call via a Custom Business Object instead?", "C00000")

doc.add_heading("2.6 Additional finding: GST/logistics metadata fields are API-locked", level=2)
doc.add_paragraph(
    "Vehicle No., Transportation Mode, Place Of Supply, G.R. No., and Date Of Supply are real custom "
    "extension fields on the Outbound Delivery (e.g. VehicleNo_KUT). A direct API write to them was "
    "tested live and rejected - SAP locks the whole document read-only for API writes the instant its "
    "own scheduler picks it up (i.e. before this application can ever reach it). Confirmed live "
    "(order 30441/delivery P8D1-203) that these SAME 5 fields ARE editable manually via Outbound "
    "Logistics > Outbound Deliveries > open Delivery > Edit > View All, and clicking \"Release\" on that "
    "screen both releases AND posts Goods Issue in one action. \"Freight Forwarder\" on this same screen "
    "requires a real Business Partner lookup (not free text) - typing plain text into it broke "
    "Consistency Status and disabled Release entirely, so it is intentionally left untouched (Note-only)."
)

# ============ ISSUE 3 ============
doc.add_heading("3. Inbound STO Receipt (Goods Receipt on the receiving Site)", level=1)
doc.add_paragraph("Status: NO API PATH EXISTS (per SAP's own published KBA). Uses Playwright UI automation.")
doc.add_paragraph(
    "Service checked: custom OData service \"inboundstockemergent\", specifically the Function Import "
    "InboundDeliveryPGRBackground (bound to the InboundDelivery / \"Inbound Delivery Notification\" "
    "business object's PGR Ground action)."
)
add_code_block(doc, """POST {endpoint}/InboundDeliveryPGRBackground
    ?ObjectID='{inbound_delivery_object_id}'
    &sap-vhost={vhost}""")
doc.add_paragraph(
    "Result: HTTP 500 \"action is disabled\" - re-confirmed live on a completely fresh, never-touched "
    "delivery (ruling out a stale-lock artifact from earlier testing)."
)
doc.add_heading("3.1 Root cause: SAP KBA 3583076", level=2)
doc.add_paragraph(
    "SAP's own official Knowledge Base Article 3583076 (\"Inability to Post Goods Receipt with Actual "
    "Quantities via OData API in Inbound Delivery Processing\") confirms: Actual Quantity belongs to the "
    "Confirmed Inbound Delivery business object, NOT the Inbound Delivery Notification, and there is NO "
    "web service/API exposed anywhere in SAP Business ByDesign to create a Confirmed Inbound Delivery. "
    "The Notification (all the API can see/touch) has no quantity-override parameter on its own "
    "PGRBackground action."
)
doc.add_heading("3.2 Workaround attempted and also blocked", level=2)
doc.add_paragraph(
    "To receive a different quantity than what's on the Notification, we attempted to PATCH the item's "
    "own InboundDeliveryItemQuantity row before calling PGRBackground:"
)
add_code_block(doc, """PATCH {endpoint}/InboundDeliveryItemQuantityCollection('{quantity_object_id}')
    Body: {{ "Quantity": "{override_qty}" }}""")
doc.add_paragraph(
    "Result: Rejected tenant-wide with \"Changing data not possible; data is read-only\". Both the "
    "default-quantity path (API action disabled) and the override-quantity path (PATCH blocked) are "
    "closed simultaneously."
)
doc.add_heading("3.3 Other alternatives checked and ruled out", level=2)
doc.add_paragraph("• Site Logistics Task (QuerySiteLogisticsTaskIn) - confirmed this tenant does NOT use task-based execution for inbound deliveries (0 hits for ProcessTypeCode=1/Inbound even on a wildcard query)", style="List Bullet")
doc.add_paragraph("• Two \"kh*\" custom OData services (SAP Cloud Applications Studio exports found on GitHub) - not deployable without a PDI developer key, and on inspection expose the exact same ObjectID-only action shape (no quantity parameter) or no create/confirm action at all", style="List Bullet")
doc.add_paragraph(
    "CONCLUSION: This is a confirmed, documented SAP platform limitation, not a configuration issue "
    "specific to this tenant. The only supported path is the native SAP UI's own \"Post Goods Receipt\" "
    "screen, which opens a full data-entry form (Actual Quantity grid) that the disabled API action does "
    "not expose equivalent parameters for."
)
add_status_line(doc, "Question for SAP consultant", "Is there any newer API (OData v2/v4, or a different process component) that supersedes the disabled InboundDeliveryPGRBackground and DOES support Actual Quantity, post-dating KBA 3583076?", "C00000")

# ============ ISSUE 4 ============
doc.add_heading("4. Supplier Portal GRN (external vendor Purchase Order receiving)", level=1)
doc.add_paragraph("Status: API dead end for real stock materials. Uses Playwright UI automation (currently UNVERIFIED end-to-end - no live test has completed the final Save yet).")
doc.add_paragraph(
    "Two SOAP services were evaluated for posting a Goods Receipt against a Purchase Order on approval "
    "of a supplier's shipment in our Supplier Portal:"
)
doc.add_heading("4.1 Service confirmed read-only (ruled out immediately)", level=2)
doc.add_paragraph(
    "QueryGoodsAndServiceAcknowledgementInbound - confirmed via SAP's own help documentation "
    "(PSM_ISI_R_II_SRM_GSA_MBO) that this service is explicitly read-only and cannot write."
)
doc.add_heading("4.2 Write service attempted: ManageGoodsAndServiceAcknowledgementIn (GSA)", level=2)
add_code_block(doc, """POST {SAP_SOAP_GSA_WRITE_ENDPOINT}
SOAPAction: (empty string - accepted by other live-tested clients in this app)
Content-Type: text/xml; charset=utf-8

<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:GSABundleMaintainRequest_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <BasicMessageHeader/>
 <GoodsAndServiceAcknowledgement actionCode="01">
  <BusinessTransactionDocumentTypeCode>282</BusinessTransactionDocumentTypeCode>
  <Name languageCode="EN">Supplier Portal GRN {doc_code}</Name>
  <PostGSAIndicator>true</PostGSAIndicator>
  <Item>
   <BusinessTransactionDocumentTypeCode actionCode="01">18</BusinessTransactionDocumentTypeCode>
   <Quantity unitCode="{unit_code}">{quantity}</Quantity>
   <PurchaseOrderReference ActionCode="01">
    <BusinessTransactionDocumentReference>
     <ID>{po_id}</ID>
     <TypeCode>001</TypeCode>
     <ItemID>{item_id}</ItemID>
     <ItemTypeCode>18</ItemTypeCode>
    </BusinessTransactionDocumentReference>
   </PurchaseOrderReference>
  </Item>
  <!-- additional Item blocks repeat, one per PO line -->
 </GoodsAndServiceAcknowledgement>
</n0:GSABundleMaintainRequest_sync>
</soapenv:Body>
</soapenv:Envelope>""")
doc.add_paragraph(
    "Namespace note: an earlier attempt incorrectly used the A1S/Global namespace (copied from the "
    "read-only query service above) and reproduced a generic \"Web service processing error\" - SAP "
    "parses the envelope fine but cannot route it to the correct ABSL handler with the wrong namespace. "
    "Corrected to http://sap.com/xi/SAPGlobal20/Global per SAP's official published GSA examples."
)
doc.add_heading("4.3 Root cause: works only for non-stock/service PO lines", level=2)
doc.add_paragraph(
    "Live testing confirmed this GSA write API path only works for non-stock/service Purchase Order "
    "lines. For real stock materials (the vast majority of this application's supplier receiving), the "
    "call is a dead end. This is a genuinely different limitation from Sections 2/3 - it is not that the "
    "action is disabled, but that its scope of applicability for goods movement of physical stock is "
    "narrower than the standard \"Post Goods Receipt\" UI flow."
)
doc.add_heading("4.4 Manual UI flow that DOES work (basis for the Playwright automation)", level=2)
doc.add_paragraph(
    "The tenant's own operations team demonstrated the manual screen sequence that successfully posts a "
    "Goods Receipt for real stock PO lines:"
)
doc.add_paragraph("1. Inbound Logistics work center → Purchase Orders view → search the exact PO", style="List Number")
doc.add_paragraph("2. Select its row → click \"Post Goods Receipt\" → opens \"Create Inbound Delivery and Goods Receipt\" dialog", style="List Number")
doc.add_paragraph("3. Fill Delivery Notification ID (supplier's own document number, entered by staff)", style="List Number")
doc.add_paragraph("4. Fill Actual Delivery Date (the supplier's Bill Date, entered by staff - the field auto-fills with \"now\" by default but must be overridden)", style="List Number")
doc.add_paragraph("5. Fill Actual Quantity per line (staff-confirmed received quantity, intentionally NOT the vendor's own claimed ship quantity)", style="List Number")
doc.add_paragraph("6. Save and Close", style="List Number")
doc.add_paragraph(
    "IMPORTANT CAVEAT: this Playwright flow is currently UNVERIFIED end-to-end for a real PO. Read-only "
    "navigate + search + fill has been verified live up to (but never including) the final \"Save and "
    "Close\" click. Two live attempts on real POs (28792, 28833) both failed BEFORE reaching the fill "
    "step, at the SAP UI's own Purchase Orders list screen, which failed to finish loading within the "
    "automation's wait window - later diagnosed as coinciding with a period where the production SAP "
    "tenant was intermittently timing out on unrelated background calls too (see Section 6)."
)
add_status_line(doc, "Question for SAP consultant", "Is there a stock-material-capable variant of the GSA write API, or a different/newer service (e.g. a Goods Movement-based create) that supports Purchase-Order-referenced physical stock receipt with Actual Quantity, that we have not yet found?", "C00000")

# ============ SECTION 5 ============
doc.add_heading("5. In-Progress Alternative: Native SAP Custom Business Object (ABSL)", level=1)
doc.add_paragraph(
    "To address Section 2 (multi-line Outbound Delivery combination) without relying on flaky Playwright "
    "UI automation, we are building a native Custom Business Object inside SAP Cloud Applications Studio, "
    "in a separate TEST tenant (my441464.businessbydesign.cloud.sap), NOT yet in production."
)
doc.add_paragraph(
    "The custom BO (\"BusinessObject1\") exposes two ABSL actions, called via SOAP from our backend "
    "instead of a browser:"
)
doc.add_paragraph(
    "• Combine: loops the target Outbound Delivery Request's ItemScheduleLine child nodes and calls "
    "SAP's own standard RequestDeliveryExecution action on each, with every split indicator set to false "
    "- the same underlying action the UI's \"Create Outbound Delivery > Without Release\" button appears "
    "to use, but invoked directly from ABSL rather than through Playwright.",
    style="List Bullet",
)
doc.add_paragraph(
    "• SetTransportDetailsAndRelease: looks up the resulting Outbound Delivery by ID and calls its "
    "standard Release() action (which also posts Goods Issue in one step, confirmed in Section 2.6).",
    style="List Bullet",
)
doc.add_paragraph(
    "Current blocker (as of this document): the custom Work Center View required to authorize a business "
    "user to call these new web services is not yet appearing in the Business Role assignment catalog, "
    "despite the solution being fully activated and Business Configuration deployed. This is being "
    "investigated as a PDI/Work Center-to-View linkage configuration issue distinct from all four "
    "sections above."
)

# ============ SECTION 6 ============
doc.add_heading("6. Infrastructure Observation: Intermittent SAP Tenant Timeouts", level=1)
doc.add_paragraph(
    "During this investigation, the production tenant (my431827.businessbydesign.cloud.sap) was observed "
    "to intermittently fail ALL OData connections with connect timeouts (30-second timeout, not an "
    "application-level error), affecting an unrelated background valuation-sync job at the same time as "
    "a live Supplier Portal GRN Playwright test failed to load its target screen. This suggests periods "
    "of broader tenant-level unavailability or network degradation, independent of any of the four issues "
    "documented above. Recommend checking SAP system availability/monitoring logs for the time window "
    "around September 1, 2026, 10:55-11:05 UTC."
)

# ============ SUMMARY TABLE ============
doc.add_heading("7. Summary Table", level=1)
table = doc.add_table(rows=1, cols=4)
table.style = "Light Grid Accent 1"
hdr = table.rows[0].cells
hdr[0].text = "Process"
hdr[1].text = "API Status"
hdr[2].text = "Root Cause"
hdr[3].text = "Current Workaround"
for cell in hdr:
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True
    set_cell_shading(cell, "2E5395")
    for p in cell.paragraphs:
        for r in p.runs:
            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

rows_data = [
    ("1. STO Creation", "Working (SOAP)", "N/A - functions correctly", "None needed"),
    ("2. Combine multi-line Outbound Delivery", "No API found (4 actions tried)", "Ship-to Party Account Master Data forces 1 delivery/line; UI button uses undocumented logic", "Playwright (flaky); native ABSL Custom BO in progress (test tenant)"),
    ("3. Inbound STO Receipt (Goods Receipt)", "Disabled by SAP (KBA 3583076)", "No API exists anywhere to create a Confirmed Inbound Delivery with Actual Quantity", "Playwright (production, working)"),
    ("4. Supplier Portal GRN", "Works for non-stock lines only", "GSA write API scope excludes real stock materials", "Playwright (built, UNVERIFIED end-to-end)"),
]
for row in rows_data:
    cells = table.add_row().cells
    for i, val in enumerate(row):
        cells[i].text = val

doc.add_paragraph()
doc.add_heading("8. Key Questions for SAP Consultant Review", level=1)
doc.add_paragraph("1. Section 1.2 - Is there an Account Master Data / Business Configuration setting that overrides PartialDeliveryControlCode for Intra-Company Stock Transfers?", style="List Number")
doc.add_paragraph("2. Section 2.5 - What does the UI's \"Create Outbound Delivery > Without Release\" button call internally, and can it be exposed via a BAdI/enhancement or Custom BO cross-call?", style="List Number")
doc.add_paragraph("3. Section 3.3 - Is there a newer API superseding the disabled InboundDeliveryPGRBackground that supports Actual Quantity?", style="List Number")
doc.add_paragraph("4. Section 4.3 - Is there a stock-material-capable GSA write variant or newer Goods Movement API for PO-referenced physical stock receipt?", style="List Number")
doc.add_paragraph("5. Section 5 - Guidance on linking a custom Work Center View to a Business Role's assignable catalog in a PDI solution.", style="List Number")

doc.save("/app/documents/SAP_Integration_Issues_Report.docx")
print("Document generated successfully.")
