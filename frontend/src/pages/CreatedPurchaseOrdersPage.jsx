import { useState, useEffect } from "react";
import axios from "axios";
import { Eye, CircleNotch } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

export default function CreatedPurchaseOrdersPage() {
  const [pos, setPos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    axios.get(`${API}/purchase-orders/history`, { params: { limit: 100 } })
      .then((r) => setPos(r.data || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const fmtDate = (v) => (v ? new Date(v).toLocaleString() : "—");

  return (
    <div className="min-h-screen bg-[#F5F6F7] flex flex-col font-sans">
      <header className="bg-white border-b border-[#D0D5DD] px-6 py-3 flex items-center justify-between gap-4 flex-wrap">
        <NavTabs />
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto max-w-[1200px] w-full mx-auto px-6 py-6 space-y-4">
        <div>
          <h1 className="font-heading text-xl font-bold text-[#1D2939]" data-testid="created-pos-page-title">Created Purchase Orders</h1>
          <p className="text-sm text-[#667085] mt-0.5">Every Purchase Order pushed to SAP ByDesign from this app, with its live SAP reference number.</p>
        </div>

        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="created-pos-table-card">
          {loading ? (
            <div className="p-8 flex items-center justify-center text-[#667085] text-sm" data-testid="created-pos-loading">
              <CircleNotch size={16} className="animate-spin mr-2" /> Loading...
            </div>
          ) : pos.length === 0 ? (
            <div className="p-8 text-center text-sm text-[#667085]" data-testid="created-pos-empty">No Purchase Orders have been created yet.</div>
          ) : (
            <table className="w-full text-xs border-collapse min-w-[900px]" data-testid="created-pos-table">
              <thead>
                <tr>
                  {["SAP PO #", "Supplier", "Site", "Bill-To", "PO Date", "PR Number", "Items", "Created By", "Created At", ""].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pos.map((po, idx) => (
                  <tr key={po._id} className={idx % 2 === 1 ? "bg-[#F9FAFB]" : ""} data-testid={`created-pos-row-${po.po_number}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-data font-semibold text-[#1D2939]">{po.po_number}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{po.supplier_code}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{po.purchase_unit_site}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{po.bill_to_company}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{po.po_date}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{po.pr_number || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{(po.items || []).length}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{po.created_by || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 whitespace-nowrap">{fmtDate(po.created_at)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-center">
                      <Button type="button" variant="ghost" size="icon" className="h-7 w-7" onClick={() => setDetail(po)} data-testid={`created-pos-view-button-${po.po_number}`}>
                        <Eye size={14} className="text-[#004B87]" />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </main>

      <Dialog open={!!detail} onOpenChange={(open) => !open && setDetail(null)}>
        <DialogContent className="max-w-2xl" data-testid="created-pos-detail-dialog">
          <DialogHeader>
            <DialogTitle>SAP Purchase Order {detail?.po_number}</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="text-sm space-y-3 text-[#344054]">
              <div className="grid grid-cols-2 gap-2">
                <p><b>Supplier:</b> {detail.supplier_code}</p>
                <p><b>Purchase Unit:</b> {detail.purchase_unit_site}</p>
                <p><b>Company:</b> {detail.company_code}</p>
                <p><b>Bill-To:</b> {detail.bill_to_company}</p>
                <p><b>PO Date:</b> {detail.po_date}</p>
                <p><b>PR Number:</b> {detail.pr_number || "—"}</p>
                <p><b>Currency:</b> {detail.currency}</p>
                <p><b>Created By:</b> {detail.created_by || "—"}</p>
              </div>
              <table className="w-full text-xs border-collapse" data-testid="created-pos-detail-items-table">
                <thead>
                  <tr>
                    {["Product", "Description", "Qty", "UoM", "Unit Price", "Delivery Date"].map((h) => (
                      <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(detail.items || []).map((it, i) => (
                    <tr key={i}>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{it.product_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{it.quantity}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{it.unit_of_measure}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{it.unit_price}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{it.delivery_date}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
