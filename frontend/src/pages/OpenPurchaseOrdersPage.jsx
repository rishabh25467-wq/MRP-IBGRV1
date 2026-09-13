import { useState, useRef, useEffect } from "react";
import axios from "axios";
import { MagnifyingGlass, ShieldCheck, Package, CheckCircle, CircleNotch, ArrowsClockwise } from "@phosphor-icons/react";
import { toast } from "sonner";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { Shield } from "@phosphor-icons/react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const fmtMoney = (amount, ccy) =>
  new Intl.NumberFormat(ccy === "USD" ? "en-US" : "en-IN", { style: "currency", currency: ccy === "USD" ? "USD" : "INR", maximumFractionDigits: 2 }).format(amount || 0);

const fmtRelative = (iso) => {
  if (!iso) return "Not SAP-verified yet";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
};

export default function OpenPurchaseOrdersPage() {
  const [supplierQuery, setSupplierQuery] = useState("");
  const [supplierSuggestions, setSupplierSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [selectedSupplier, setSelectedSupplier] = useState(null);
  const [poNumberQuery, setPoNumberQuery] = useState("");
  const [poSearchActive, setPoSearchActive] = useState(false);
  const [items, setItems] = useState(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const wrapperRef = useRef(null);
  const debounceRef = useRef(null);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) setShowSuggestions(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const onQueryChange = (v) => {
    setSupplierQuery(v);
    setSelectedSupplier(null);
    setPoSearchActive(false);
    setItems(null);
    setShowSuggestions(true);
    clearTimeout(debounceRef.current);
    if (!v.trim()) { setSupplierSuggestions([]); return; }
    debounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/purchase-orders/suppliers/search`, { params: { q: v, limit: 15 } });
        setSupplierSuggestions(data);
      } catch { /* silent */ }
    }, 300);
  };

  const pickSupplier = async (s) => {
    setSelectedSupplier(s);
    setPoSearchActive(false);
    setSupplierQuery(`${s.supplier_code} - ${s.name}`);
    setShowSuggestions(false);
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/purchase-orders/open`, { params: { supplier_code: s.supplier_code } });
      setItems(data.items || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  };

  // Sep 14 2026, user's explicit ask: search this same screen by PO
  // number directly, without picking a vendor first.
  const searchByPoNumber = async () => {
    if (!poNumberQuery.trim()) return;
    setSelectedSupplier(null);
    setPoSearchActive(true);
    setShowSuggestions(false);
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/purchase-orders/lookup-by-number`, { params: { po_number: poNumberQuery.trim() } });
      setItems(data.items || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  };

  const groupedByPo = (items || []).reduce((acc, it) => {
    (acc[it.po_number] = acc[it.po_number] || []).push(it);
    return acc;
  }, {});

  // Sep 10 2026, user's explicit ask: a PO created directly in SAP
  // (not via this app's own PO Creation flow) only shows up here after
  // the shared 10-min background sync - this lets staff force that
  // sync right now instead of waiting.
  const refreshFromSap = async () => {
    setRefreshing(true);
    try {
      const { data } = await axios.post(`${API}/admin/purchase-orders/refresh-cache`);
      const jobId = data.job_id;
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const { data: poll } = await axios.get(`${API}/admin/purchase-orders/refresh-cache/poll/${jobId}`);
        if (poll.status === "done") {
          toast.success("Refreshed from SAP - re-searching");
          if (selectedSupplier) await pickSupplier(selectedSupplier);
          else if (poSearchActive) await searchByPoNumber();
          return;
        }
        if (poll.status === "failed") {
          toast.error(`Refresh failed: ${poll.error || "Unknown error"}`);
          return;
        }
      }
      toast.error("Refresh is taking longer than expected - try again shortly");
    } catch {
      toast.error("Could not start SAP refresh");
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F2F4F7] flex flex-col font-sans">
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0">
          <div className="w-8 h-8 rounded-sm bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Open Purchase Orders</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto w-full max-w-[1400px] mx-auto px-4 sm:px-6 lg:px-8 py-4 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="font-heading text-xl font-bold text-[#101828] tracking-tight flex items-center gap-2" data-testid="open-pos-page-title">
              <ShieldCheck size={18} className="text-[#004B87]" weight="fill" />
              Open Purchase Orders
            </h1>
            <p className="text-sm text-[#475467] mt-0.5">Pick a vendor to see every PO line still open against them - SAP-verified where available.</p>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="rounded-sm border-[#D0D5DD] text-[#344054] shrink-0"
            onClick={refreshFromSap}
            disabled={refreshing}
            data-testid="open-pos-refresh-sap-button"
          >
            {refreshing ? <CircleNotch size={14} className="animate-spin mr-1.5" /> : <ArrowsClockwise size={14} className="mr-1.5" />}
            {refreshing ? "Refreshing from SAP..." : "Refresh from SAP"}
          </Button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] p-4 space-y-2 relative" ref={wrapperRef} data-testid="open-pos-supplier-card">
            <Label className="text-xs font-medium text-[#344054]">Supplier</Label>
            <div className="relative">
              <MagnifyingGlass size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
              <Input
                value={supplierQuery}
                onChange={(e) => onQueryChange(e.target.value)}
                onFocus={() => setShowSuggestions(true)}
                placeholder="Search SAP supplier by name or code..."
                className="h-9 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87] pl-9"
                data-testid="open-pos-supplier-search-input"
              />
            </div>
            {showSuggestions && supplierSuggestions.length > 0 && (
              <div className="absolute z-20 left-4 right-4 mt-0.5 bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid="open-pos-supplier-suggestions">
                {supplierSuggestions.map((s) => (
                  <button
                    key={s.supplier_code}
                    type="button"
                    className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0"
                    onClick={() => pickSupplier(s)}
                    data-testid={`open-pos-supplier-suggestion-${s.supplier_code}`}
                  >
                    <span className="font-semibold text-[#101828] font-data">{s.supplier_code}</span>
                    <span className="text-[#667085]"> - {s.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] p-4 space-y-2" data-testid="open-pos-po-number-card">
            <Label className="text-xs font-medium text-[#344054]">PO Number</Label>
            <div className="relative flex gap-2">
              <div className="relative flex-1">
                <MagnifyingGlass size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                <Input
                  value={poNumberQuery}
                  onChange={(e) => setPoNumberQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && searchByPoNumber()}
                  placeholder="Search by exact PO number..."
                  className="h-9 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87] pl-9"
                  data-testid="open-pos-po-number-search-input"
                />
              </div>
              <Button
                size="sm"
                className="h-9 rounded-sm bg-[#004B87] hover:bg-[#003A6A]"
                onClick={searchByPoNumber}
                data-testid="open-pos-po-number-search-button"
              >
                Search
              </Button>
            </div>
          </div>
        </div>

        {loading && (
          <div className="p-8 flex items-center justify-center text-[#667085] text-sm" data-testid="open-pos-loading">
            <CircleNotch size={16} className="animate-spin mr-2" /> Loading open PO lines...
          </div>
        )}

        {!loading && items && items.length === 0 && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm p-8 text-center text-sm text-[#667085]" data-testid="open-pos-empty">
            {poSearchActive
              ? "No cached, open PO lines found for this PO number (it may be Cancelled, In Preparation, Rejected, or not in this tenant's recent SAP window)."
              : "No cached PO lines found for this vendor (only recently-active POs are cached here)."}
          </div>
        )}

        {!loading && items && items.length > 0 && (
          <div className="space-y-4" data-testid="open-pos-results">
            {Object.entries(groupedByPo).map(([poNumber, poItems]) => (
              <div key={poNumber} className="bg-white border border-[#D0D5DD] rounded-sm overflow-hidden" data-testid={`open-pos-po-card-${poNumber}`}>
                <div className="px-4 py-2.5 border-b border-[#D0D5DD] bg-[#F9FAFB] flex items-center justify-between flex-wrap gap-2">
                  <div className="flex items-center gap-2">
                    <Package size={14} className="text-[#004B87]" />
                    <span className="font-data font-bold text-[#101828] text-sm">PO {poNumber}</span>
                    <Badge className="bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6] rounded-sm text-[10px]" data-testid={`open-pos-status-badge-${poNumber}`}>Open in SAP</Badge>
                    {poItems[0]?.sap_po_number && (
                      <span className="font-data text-xs text-[#475467]" data-testid={`open-pos-printed-po-${poNumber}`}>Printed PO #: {poItems[0].sap_po_number}</span>
                    )}
                    {poSearchActive && poItems[0]?.vendor_name && (
                      <span className="text-xs text-[#475467]" data-testid={`open-pos-vendor-name-${poNumber}`}>Vendor: {poItems[0].vendor_code} - {poItems[0].vendor_name}</span>
                    )}
                    <span className="text-xs text-[#667085]">Buyer: {poItems[0]?.buyer_entity_name || "-"} · PO Date: {poItems[0]?.po_date || "-"}</span>
                  </div>
                </div>
                <table className="w-full text-xs border-collapse" data-testid={`open-pos-items-table-${poNumber}`}>
                  <thead>
                    <tr>
                      {["Item#", "Product", "Description", "PO Qty", "Shipped", "Open Qty", "UoM", "Value", "Due Date", "SAP Verified"].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {poItems.map((it, i) => {
                      const fullyDelivered = (it.remaining_qty ?? 0) <= 1e-6;
                      return (
                        <tr key={`${it.po_number}-${it.item_number}`} className={i % 2 === 1 ? "bg-[#F9FAFB]" : ""} data-testid={`open-pos-item-row-${poNumber}-${it.item_number}`}>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 font-data">{it.item_number}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 font-data">{it.product_id}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "-"}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 font-data text-right">{it.po_qty}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 font-data text-right">{it.already_shipped_qty}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 font-data text-right font-bold" data-testid={`open-pos-open-qty-${poNumber}-${it.item_number}`}>
                            {fullyDelivered ? (
                              <Badge className="bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6] rounded-sm text-[10px]"><CheckCircle size={10} className="mr-1" weight="fill" /> Fully Delivered</Badge>
                            ) : (
                              <span className="text-[#004B87]">{it.remaining_qty}</span>
                            )}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5">{it.unit_of_measure}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 font-data text-right whitespace-nowrap">{fmtMoney(it.subtotal, it.currency)}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 whitespace-nowrap">{it.due_date || "-"}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#98A2B3] whitespace-nowrap">{fmtRelative(it.sap_verified_at)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
