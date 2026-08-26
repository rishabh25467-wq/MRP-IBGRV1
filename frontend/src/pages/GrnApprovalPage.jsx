import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { MagnifyingGlass, CheckCircle, XCircle, Truck, PlugsConnected, Shield } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const STATUS_BADGE = {
  in_transit: { label: "In Transit", className: "bg-[#E3A008]/15 text-[#8A6116] rounded-sm" },
  approved: { label: "Received", className: "bg-[#10B981]/15 text-[#0B7A56] rounded-sm" },
  rejected: { label: "Rejected", className: "bg-[#E02424]/10 text-[#B91C1C] rounded-sm" },
};

export default function GrnApprovalPage() {
  const [code, setCode] = useState("");
  const [shipment, setShipment] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [pending, setPending] = useState([]);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [busy, setBusy] = useState(false);

  const loadPending = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/grn/shipments`, { params: { status: "in_transit" } });
      setPending(data.shipments || []);
    } catch (err) {
      toast.error("Could not load pending shipments", { description: err?.response?.data?.detail || err.message });
    }
  };

  useEffect(() => {
    loadPending();
  }, []);

  const lookup = async (targetCode) => {
    const value = (targetCode || code).trim();
    if (!value) return;
    setSearching(true);
    setSearchError("");
    setShipment(null);
    try {
      const { data } = await axios.get(`${API}/admin/grn/lookup/${value}`);
      setShipment(data);
      setCode(value);
    } catch (err) {
      setSearchError(err?.response?.data?.detail || "No shipment found for this code");
    } finally {
      setSearching(false);
    }
  };

  const approve = async () => {
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/approve`);
      setShipment(data);
      if (data.sap_sync_status === "posted") {
        toast.success("Goods Receipt posted to SAP");
      } else {
        toast.warning("Approved internally - SAP posting is pending", { description: data.sap_gr_result?.reason });
      }
      loadPending();
    } catch (err) {
      toast.error("Approval failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/reject`, { reason: rejectReason });
      setShipment(data);
      setRejectOpen(false);
      setRejectReason("");
      toast.success("Shipment rejected");
      loadPending();
    } catch (err) {
      toast.error("Rejection failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="grn-approval-page">
      <Toaster position="top-right" richColors />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">GRN Approval</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
      </header>
      <div className="max-w-4xl mx-auto p-4 md:p-6">
        <div className="flex items-center gap-2 mb-1">
          <Truck size={18} weight="fill" className="text-[#0076CC]" />
          <h1 className="font-sans text-base font-bold text-[#111827]">GRN Approval</h1>
        </div>
        <p className="text-sm text-[#5B738B]">Enter the 6-character shipment code from the delivery paperwork, physically match the goods and supplier invoice, then approve.</p>

        <div className="flex gap-2 mt-5">
          <Input
            placeholder="e.g. A3F9K2"
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && lookup()}
            className="font-data uppercase max-w-xs rounded-sm border-[#CBD3DB] focus:ring-2 focus:ring-[#4DA3E0] focus:outline-none"
            maxLength={6}
            data-testid="grn-code-input"
          />
          <Button onClick={() => lookup()} disabled={searching} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="grn-code-search-button">
            <MagnifyingGlass size={14} className="mr-1" /> {searching ? "Searching..." : "Lookup"}
          </Button>
        </div>
        {searchError && <div className="text-sm text-[#B91C1C] mt-2" data-testid="grn-search-error">{searchError}</div>}

        {shipment && (
          <div className="mt-6 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-5" data-testid="grn-shipment-detail">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-lg font-data font-bold text-[#0076CC]">{shipment._id}</div>
                <div className="text-sm text-[#5B738B]">{shipment.company_name} ({shipment.vendor_code}) · {[...new Set(shipment.items.map((it) => it.po_number))].map((p) => `PO ${p}`).join(", ")}</div>
              </div>
              <Badge className={STATUS_BADGE[shipment.status].className}>{STATUS_BADGE[shipment.status].label}</Badge>
            </div>

            <table className="w-full text-sm mt-4 border-collapse">
              <thead className="text-[#5B738B] text-xs uppercase">
                <tr>
                  <th className="text-left py-1 font-semibold">PO Number</th>
                  <th className="text-left py-1 font-semibold">Item</th>
                  <th className="text-left py-1 font-semibold">Description</th>
                  <th className="text-right py-1 font-semibold">Ship Qty</th>
                  <th className="text-right py-1 font-semibold">PO Qty</th>
                </tr>
              </thead>
              <tbody>
                {shipment.items.map((it, i) => (
                  <tr key={i} className="border-t border-[#CBD3DB]">
                    <td className="py-1.5 font-data">{it.po_number}</td>
                    <td className="py-1.5 font-data">{it.item_number}</td>
                    <td className="py-1.5">{it.description}</td>
                    <td className="py-1.5 text-right font-data font-semibold">{it.ship_qty} {it.unit_of_measure}</td>
                    <td className="py-1.5 text-right font-data text-[#5B738B]">{it.po_qty}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {shipment.status === "in_transit" && (
              <div className="flex gap-2 mt-5">
                <Button onClick={approve} disabled={busy} className="rounded-sm bg-[#10B981] hover:bg-[#0B7A56] transition-colors duration-150" data-testid="grn-approve-button">
                  <CheckCircle size={14} className="mr-1" /> Approve &amp; Post to SAP
                </Button>
                <Button variant="outline" onClick={() => setRejectOpen(true)} className="rounded-sm border-[#E02424]/40 text-[#B91C1C]" data-testid="grn-reject-button">
                  <XCircle size={14} className="mr-1" /> Reject
                </Button>
              </div>
            )}

            {shipment.status === "approved" && (
              <div className="mt-4 text-sm px-3 py-2 rounded-sm flex items-center gap-2 border" data-testid="grn-sap-sync-status"
                   style={shipment.sap_sync_status === "posted" ? { color: "#0B7A56", background: "rgba(16,185,129,0.1)", borderColor: "rgba(16,185,129,0.3)" } : { color: "#1D4ED8", background: "rgba(63,131,248,0.1)", borderColor: "rgba(63,131,248,0.3)" }}>
                {shipment.sap_sync_status === "posted"
                  ? <CheckCircle size={16} />
                  : <PlugsConnected size={16} />}
                {shipment.sap_sync_status === "posted"
                  ? "Goods Receipt posted to SAP"
                  : `SAP posting pending${shipment.sap_gr_result?.reason ? ` - ${shipment.sap_gr_result.reason}` : ""}`}
              </div>
            )}

            {shipment.status === "rejected" && shipment.rejection_reason && (
              <div className="mt-4 text-sm text-[#B91C1C]" data-testid="grn-rejection-reason">Reason: {shipment.rejection_reason}</div>
            )}
          </div>
        )}

        <h2 className="font-sans text-sm font-bold text-[#111827] mt-8">Pending Shipments</h2>
        <div className="mt-3 bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
              <tr>
                <th className="text-left px-3 py-2 font-semibold">Code</th>
                <th className="text-left px-3 py-2 font-semibold">Vendor</th>
                <th className="text-left px-3 py-2 font-semibold">PO Numbers</th>
                <th className="text-left px-3 py-2 font-semibold">Created</th>
              </tr>
            </thead>
            <tbody>
              {pending.length === 0 && (
                <tr><td colSpan={4} className="px-3 py-6 text-center text-[#5B738B]" data-testid="grn-pending-empty">No shipments awaiting GRN approval.</td></tr>
              )}
              {pending.map((s) => (
                <tr key={s._id} className="border-b border-[#CBD3DB] cursor-pointer hover:bg-[#F5F6F7] transition-colors duration-150" onClick={() => lookup(s._id)} data-testid={`grn-pending-row-${s._id}`}>
                  <td className="px-3 py-2 font-data font-bold">{s._id}</td>
                  <td className="px-3 py-2">{s.company_name}</td>
                  <td className="px-3 py-2 font-data">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                  <td className="px-3 py-2 text-[#5B738B]">{new Date(s.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
        <DialogContent className="rounded-sm" data-testid="grn-reject-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Reject Shipment</DialogTitle>
            <DialogDescription>This reason will be shown to the vendor.</DialogDescription>
          </DialogHeader>
          <Textarea placeholder="Reason" value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} className="rounded-sm border-[#CBD3DB]" data-testid="grn-reject-reason-input" />
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setRejectOpen(false)}>Cancel</Button>
            <Button onClick={reject} disabled={busy} className="rounded-sm bg-[#E02424] hover:bg-[#B91C1C] transition-colors duration-150" data-testid="grn-reject-confirm-button">Confirm Reject</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
