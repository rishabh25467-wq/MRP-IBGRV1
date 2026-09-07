import { useState, useEffect, useCallback } from "react";
import axios from "axios";
import { Eye, CircleNotch, FunnelSimple, X } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const EMPTY_FILTERS = { supplier_code: "", site_id: "", product_id: "", created_by: "", po_date_from: "", po_date_to: "" };

export default function CreatedPurchaseOrdersPage() {
  const [pos, setPos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [productFilterInput, setProductFilterInput] = useState("");
  const [sites, setSites] = useState([]);
  const [filterOptions, setFilterOptions] = useState({ suppliers: [], created_by: [] });

  useEffect(() => {
    axios.get(`${API}/purchase-orders/sites`).then((r) => setSites(r.data?.sites || [])).catch(() => {});
    axios.get(`${API}/purchase-orders/history/filter-options`).then((r) => setFilterOptions(r.data || { suppliers: [], created_by: [] })).catch(() => {});
  }, []);

  const loadHistory = useCallback(() => {
    setLoading(true);
    const params = { limit: 200 };
    Object.entries(filters).forEach(([k, v]) => { if (v) params[k] = v; });
    axios.get(`${API}/purchase-orders/history`, { params })
      .then((r) => setPos(r.data || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [filters]);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const setFilter = (key, value) => setFilters((prev) => ({ ...prev, [key]: value }));
  const clearFilters = () => { setFilters(EMPTY_FILTERS); setProductFilterInput(""); };
  const activeFilterCount = Object.values(filters).filter(Boolean).length;

  const fmtDate = (v) => (v ? new Date(v).toLocaleString() : "—");

  return (
    <div className="min-h-screen bg-[#F5F6F7] flex flex-col font-sans">
      {/* Sep 7 2026 fix - real bug, "menu is not visible on this page":
          this header used a plain white bar while every other page uses a
          teal (#0E7C86) header - NavTabs/SapConnectionStatus render WHITE
          text (by design, meant to sit on that teal bar), so on a white
          background they were rendering but invisible (white-on-white).
          Only the active/current NavTabs item showed (its text turns teal
          when active). Matched the standard header used everywhere else. */}
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto max-w-[1400px] w-full mx-auto px-6 py-6 space-y-4">
        <div>
          <h1 className="font-heading text-xl font-bold text-[#1D2939]" data-testid="created-pos-page-title">Created Purchase Orders</h1>
          <p className="text-sm text-[#667085] mt-0.5">Every Purchase Order pushed to SAP ByDesign from this app, with its live SAP reference number.</p>
        </div>

        <div className="bg-white border border-[#D0D5DD] rounded-sm p-3" data-testid="created-pos-filter-bar">
          <div className="flex items-center gap-2 mb-2">
            <FunnelSimple size={14} className="text-[#004B87]" />
            <span className="text-xs font-bold text-[#344054] font-heading uppercase">Filters</span>
            {activeFilterCount > 0 && (
              <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-[11px] text-[#B42318] hover:bg-[#FEF3F2]" onClick={clearFilters} data-testid="created-pos-clear-filters-button">
                <X size={11} className="mr-1" /> Clear ({activeFilterCount})
              </Button>
            )}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2">
            <Select value={filters.supplier_code || "__all__"} onValueChange={(v) => setFilter("supplier_code", v === "__all__" ? "" : v)}>
              <SelectTrigger className="h-8 text-xs rounded-sm" data-testid="created-pos-filter-supplier"><SelectValue placeholder="Supplier" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All Suppliers</SelectItem>
                {filterOptions.suppliers.map((s) => (
                  <SelectItem key={s.code} value={s.code}>{s.code} - {s.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={filters.site_id || "__all__"} onValueChange={(v) => setFilter("site_id", v === "__all__" ? "" : v)}>
              <SelectTrigger className="h-8 text-xs rounded-sm" data-testid="created-pos-filter-site"><SelectValue placeholder="Plant" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All Plants</SelectItem>
                {sites.map((s) => (
                  <SelectItem key={s.id || s} value={s.id || s}>{s.name ? `${s.id} - ${s.name}` : s}</SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Input
              placeholder="Item / Product ID"
              className="h-8 text-xs rounded-sm"
              value={productFilterInput}
              onChange={(e) => setProductFilterInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") setFilter("product_id", productFilterInput.trim()); }}
              onBlur={() => setFilter("product_id", productFilterInput.trim())}
              data-testid="created-pos-filter-product"
            />

            <Select value={filters.created_by || "__all__"} onValueChange={(v) => setFilter("created_by", v === "__all__" ? "" : v)}>
              <SelectTrigger className="h-8 text-xs rounded-sm" data-testid="created-pos-filter-created-by"><SelectValue placeholder="Created By" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">Anyone</SelectItem>
                {filterOptions.created_by.map((c) => (
                  <SelectItem key={c} value={c}>{c}</SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Input
              type="date"
              className="h-8 text-xs rounded-sm font-data"
              value={filters.po_date_from}
              onChange={(e) => setFilter("po_date_from", e.target.value)}
              data-testid="created-pos-filter-date-from"
            />
            <Input
              type="date"
              className="h-8 text-xs rounded-sm font-data"
              value={filters.po_date_to}
              onChange={(e) => setFilter("po_date_to", e.target.value)}
              data-testid="created-pos-filter-date-to"
            />
          </div>
        </div>

        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="created-pos-table-card">
          {loading ? (
            <div className="p-8 flex items-center justify-center text-[#667085] text-sm" data-testid="created-pos-loading">
              <CircleNotch size={16} className="animate-spin mr-2" /> Loading...
            </div>
          ) : pos.length === 0 ? (
            <div className="p-8 text-center text-sm text-[#667085]" data-testid="created-pos-empty">
              {activeFilterCount > 0 ? "No Purchase Orders match these filters." : "No Purchase Orders have been created yet."}
            </div>
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
