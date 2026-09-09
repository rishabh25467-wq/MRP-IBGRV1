const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");
const formatDateTime = (iso) => {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "\u2014" : d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
};

// Sep 9 2026, mirrors RequestPrintSlip's layout/pattern (plain browser
// window.print(), no backend change) for the new Return to Store
// workflow - shared by ReturnToStoreTab.jsx (Production) and
// StoreReturnsPanel.jsx (Store).
export const ReturnPrintSlip = ({ ret }) => {
  if (!ret) return null;
  return (
    <div className="hidden print:block print:fixed print:top-0 print:left-0 print:w-full print:bg-white p-10 text-[13px] text-[#101828]" style={{ fontFamily: "'DM Sans', sans-serif" }} data-testid="return-print-slip">
      <style>{"@media print { @page { margin: 0; size: auto; } }"}</style>
      <div className="flex justify-between items-start">
        <div />
        <div className="text-right">
          <h1 className="text-2xl font-bold tracking-wide">Return To Store</h1>
          <p className="text-xs text-[#667085] mt-1">Page 1 of 1</p>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-y-2 gap-x-10 mt-8 text-sm">
        <div><span className="font-bold">Return Request ID :</span> {ret._id}</div>
        <div><span className="font-bold">Return Type :</span> {ret.return_type === "against_request" ? "Against Request" : "Manual"}</div>
        <div><span className="font-bold">Original Request ID :</span> {ret.original_request_id || "\u2014"}</div>
        <div><span className="font-bold">Date :</span> {formatDateTime(ret.created_at)}</div>
        <div><span className="font-bold">Requester :</span> {ret.requester}</div>
        <div><span className="font-bold">Site :</span> {ret.site_id}</div>
        <div><span className="font-bold">Status :</span> {ret.status}</div>
      </div>
      <table className="w-full text-[12px] border-collapse mt-8">
        <thead>
          <tr>
            {["Item Code", "Material Description", "Return Qty", "UOM", "Return Reason", "Remarks", "SAP Goods Movement"].map((h) => (
              <th key={h} className="bg-[#E5E7EB] border border-[#101828] px-2 py-1.5 text-left font-bold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(ret.items || []).map((it, i) => (
            <tr key={`${it.product_id}-${i}`}>
              <td className="border border-[#101828] px-2 py-1.5">{it.product_id}</td>
              <td className="border border-[#101828] px-2 py-1.5">{it.description || "\u2014"}</td>
              <td className="border border-[#101828] px-2 py-1.5">{formatQty(it.return_qty)}</td>
              <td className="border border-[#101828] px-2 py-1.5">{formatUnit(it.unit_of_measure)}</td>
              <td className="border border-[#101828] px-2 py-1.5">{it.reason_label}</td>
              <td className="border border-[#101828] px-2 py-1.5">{it.remarks || "\u2014"}</td>
              <td className="border border-[#101828] px-2 py-1.5">{it.sap_goods_movement?.external_id || "\u2014"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex justify-between items-end mt-14 pt-6">
        <div className="text-xs text-[#667085]">
          <p className="border-t border-[#101828] pt-1 w-40">Requester Sign / Date</p>
        </div>
        <div className="text-xs text-[#667085]">
          <p className="border-t border-[#101828] pt-1 w-40">Store Confirmation Sign / Date</p>
        </div>
      </div>
    </div>
  );
};
