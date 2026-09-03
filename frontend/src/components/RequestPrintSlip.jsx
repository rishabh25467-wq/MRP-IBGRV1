const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");
const formatDateTime = (iso) => {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "\u2014" : d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
};

// Sep 2026, user's explicit ask: a plain "Print / Save as PDF" slip
// (browser window.print(), matches GatePassPage's pattern - no backend
// change, no new library) so a requester can hand-carry the requirement
// to the store, or the store can keep a paper record - same single
// layout for both, content just reflects the request as-is. Shared by
// StoreApprovalPage.js (store-side detail view) and MyStockRequestsTab.jsx
// (requester-side "My Requests" tab) so a requester never needs the
// store_approval page permission just to print their own request.
// Layout rebuilt (same session, user provided a "Request For Store"
// reference screenshot) to match that reference: right-aligned title
// block, Store ID/Requester/Department left column + Production Order
// ID/Site/Date right column, Serial No/Product ID/Description columns
// split out instead of one combined "Component" cell. "Department" has
// no real data source yet - user's explicit choice: show Site ID there
// as a stand-in. "Issue For" from the reference is intentionally NOT
// printed (user's explicit choice - meaning wasn't applicable here).
export const RequestPrintSlip = ({ request, heading = "Goods Issue From Store" }) => {
  if (!request) return null;
  return (
    <div className="hidden print:block print:fixed print:top-0 print:left-0 print:w-full print:bg-white p-10 text-[13px] text-[#101828]" style={{ fontFamily: "'DM Sans', sans-serif" }} data-testid="store-request-print-slip">
      <style>{"@media print { @page { margin: 0; size: auto; } }"}</style>
      <div className="flex justify-between items-start">
        <div />
        <div className="text-right">
          <h1 className="text-2xl font-bold tracking-wide">{heading}</h1>
          <p className="text-xs text-[#667085] mt-1">Page 1 of 1</p>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-y-2 gap-x-10 mt-8 text-sm">
        <div><span className="font-bold">Store ID :</span> {request._id}</div>
        <div><span className="font-bold">Production Order ID :</span> {request.production_proposal_id || "\u2014"}</div>
        <div><span className="font-bold">Requester :</span> {request.requester}</div>
        <div><span className="font-bold">Site :</span> {request.site_id}</div>
        <div><span className="font-bold">Department :</span> {request.site_id}</div>
        <div><span className="font-bold">Date :</span> {formatDateTime(request.created_at)}</div>
      </div>
      <table className="w-full text-[12px] border-collapse mt-8">
        <thead>
          <tr>
            {["Serial No", "Product ID", "Description", "Required Qty", "Issued Qty"].map((h) => (
              <th key={h} className="bg-[#E5E7EB] border border-[#101828] px-2 py-1.5 text-left font-bold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(request.components || []).map((c, i) => (
            <tr key={c.product_id}>
              <td className="border border-[#101828] px-2 py-1.5 text-center">{i + 1}</td>
              <td className="border border-[#101828] px-2 py-1.5">{c.product_id}</td>
              <td className="border border-[#101828] px-2 py-1.5">{c.description || "\u2014"}</td>
              <td className="border border-[#101828] px-2 py-1.5">{formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}</td>
              <td className="border border-[#101828] px-2 py-1.5">{c.issued_qty == null ? "\u2014" : `${formatQty(c.issued_qty)} ${formatUnit(c.unit_of_measure)}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex justify-between items-end mt-14 pt-6">
        <div className="text-xs text-[#667085]">
          <p className="border-t border-[#101828] pt-1 w-40">Requester Sign / Date</p>
        </div>
        <div className="text-xs text-[#667085]">
          <p className="border-t border-[#101828] pt-1 w-40">Store Sign / Date</p>
        </div>
      </div>
    </div>
  );
};
