const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");
const formatDateTime = (iso) => {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "\u2014" : d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
};

// Sep 2026, user's explicit ask: a plain "Print / Save as PDF" slip
// (browser window.print(), matches GatePassPage's pattern - no backend
// change, no new library) so a requester can hand-carry the requirement
// to the store, or the store can keep a paper record - same single
// layout for both, content just reflects the request as-is. Shared by
// StoreApprovalPage.js (store-side detail view) and MyStockRequestsTab.jsx
// (requester-side "My Requests" tab) so a requester never needs the
// store_approval page permission just to print their own request.
export const RequestPrintSlip = ({ request }) => {
  if (!request) return null;
  return (
    <div className="hidden print:block p-10 text-[13px] text-[#101828]" style={{ fontFamily: "'DM Sans', sans-serif" }} data-testid="store-request-print-slip">
      <style>{"@media print { @page { margin: 0; size: auto; } }"}</style>
      <div className="text-center border-b-2 border-[#101828] pb-2">
        <h1 className="text-lg font-bold tracking-wide">Materials Hub</h1>
        <h2 className="text-sm font-bold uppercase mt-1">Material Requisition Slip</h2>
      </div>
      <div className="grid grid-cols-2 gap-3 mt-4">
        <div><span className="font-bold">Request ID:</span> {request._id}</div>
        <div><span className="font-bold">Proposal / Lot Ref:</span> {request.production_proposal_id || "\u2014"}</div>
        <div><span className="font-bold">Site:</span> {request.site_id}</div>
        <div><span className="font-bold">Requester:</span> {request.requester}</div>
        <div className="col-span-2"><span className="font-bold">Date &amp; Time:</span> {formatDateTime(request.created_at)}</div>
      </div>
      <table className="w-full text-[12px] border-collapse mt-4">
        <thead>
          <tr>
            {["Component", "Required Qty", "Issued Qty"].map((h) => (
              <th key={h} className="border border-[#101828] px-2 py-1 text-left font-bold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(request.components || []).map((c) => (
            <tr key={c.product_id}>
              <td className="border border-[#101828] px-2 py-1">{c.product_id}{c.description ? ` - ${c.description}` : ""}</td>
              <td className="border border-[#101828] px-2 py-1">{formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}</td>
              <td className="border border-[#101828] px-2 py-1">{c.issued_qty == null ? "\u2014" : `${formatQty(c.issued_qty)} ${formatUnit(c.unit_of_measure)}`}</td>
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
