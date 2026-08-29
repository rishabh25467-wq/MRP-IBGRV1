import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { MagnifyingGlass, CheckCircle, XCircle, Truck, PlugsConnected, Shield, WarningCircle, ArrowsClockwise } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const STATUS_BADGE = {
  in_transit: { label: "In Transit", className: "bg-[#E3A008]/15 text-[#8A6116] rounded-sm" },
  discrepancy: { label: "Discrepancy", className: "bg-[#E02424]/15 text-[#B91C1C] rounded-sm" },
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

  const [sites, setSites] = useState([]);
  const [warehouses, setWarehouses] = useState([]);
  const [supplierDocNum, setSupplierDocNum] = useState("");
  const [siteId, setSiteId] = useState("");
  const [warehouseId, setWarehouseId] = useState("");
  const [warehousesLoading, setWarehousesLoading] = useState(false);
  const [billDate, setBillDate] = useState("");
  const [actualQtys, setActualQtys] = useState({});
  const [jobProgress, setJobProgress] = useState(null);

  const [discOpen, setDiscOpen] = useState(false);
  const [discReason, setDiscReason] = useState("");
  const [discItems, setDiscItems] = useState({});

  const loadPending = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/grn/shipments`, { params: { status: "in_transit" } });
      setPending(data.shipments || []);
    } catch (err) {
      toast.error("Could not load pending shipments", { description: err?.response?.data?.detail || err.message });
    }
  };

  const loadSites = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/grn/sites`);
      setSites(data.sites || []);
      if ((data.sites || []).length === 1) setSiteId(data.sites[0]);
    } catch (err) {
      toast.error("Could not load sites", { description: err?.response?.data?.detail || err.message });
    }
  };

  useEffect(() => {
    loadPending();
    loadSites();
  }, []);

  useEffect(() => {
    if (!siteId) {
      setWarehouses([]);
      setWarehouseId("");
      return;
    }
    setWarehousesLoading(true);
    setWarehouseId("");
    axios.get(`${API}/admin/grn/warehouses/${siteId}`)
      .then(({ data }) => setWarehouses(data.warehouses || []))
      .catch((err) => toast.error("Could not load warehouses", { description: err?.response?.data?.detail || err.message }))
      .finally(() => setWarehousesLoading(false));
  }, [siteId]);

  const resetApprovalForm = () => {
    setSupplierDocNum("");
    setWarehouseId("");
    setBillDate("");
    if (sites.length !== 1) setSiteId("");
  };

  const initActualQtys = (items) => {
    const next = {};
    (items || []).forEach((it) => { next[`${it.po_number}::${it.item_number}`] = it.actual_qty ?? it.ship_qty; });
    setActualQtys(next);
  };

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
      resetApprovalForm();
      initActualQtys(data.items);
    } catch (err) {
      setSearchError(err?.response?.data?.detail || "No shipment found for this code");
    } finally {
      setSearching(false);
    }
  };

  const pollGrnJob = async (jobId) => {
    const deadline = Date.now() + 6 * 60 * 1000; // multi-line, multi-PO GRNs can take a couple of minutes
    while (Date.now() < deadline) {
      const { data: job } = await axios.get(`${API}/admin/grn/receipt-status/${jobId}`);
      setJobProgress(job);
      if (job.status === "done") return job.result;
      if (job.status === "failed") throw new Error(job.error || "Goods Receipt posting failed");
      await new Promise((r) => setTimeout(r, 2500));
    }
    throw new Error("This is taking longer than expected - check back shortly, it may still complete in the background.");
  };

  const approve = async () => {
    if (!siteId || !warehouseId) {
      toast.error("Select a Site and Warehouse before approving");
      return;
    }
    setBusy(true);
    setJobProgress(null);
    try {
      const item_actual_qtys = shipment.items.map((it) => ({
        po_number: it.po_number, item_number: it.item_number,
        actual_qty: Number(actualQtys[`${it.po_number}::${it.item_number}`] ?? it.ship_qty),
      }));
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/approve`, {
        supplier_doc_num: supplierDocNum, bill_date: billDate, site_id: siteId, warehouse_id: warehouseId, item_actual_qtys,
      });
      setShipment(data.shipment);
      const result = await pollGrnJob(data.job_id);
      setShipment(result);
      if (result.sap_sync_status === "posted" && result.sap_movement_status === "posted") {
        toast.success(`Goods Receipt posted + stock moved to ${result.site_id}/${result.warehouse_id}`);
      } else if (result.sap_sync_status === "posted") {
        toast.warning("Goods Receipt posted to SAP - warehouse movement still pending", { description: JSON.stringify(result.sap_movement_result) });
      } else {
        toast.error("Approved internally - SAP posting failed, use Retry Goods Receipt below", { description: JSON.stringify(result.sap_gr_result) });
      }
      loadPending();
    } catch (err) {
      toast.error("Approval failed", { description: err?.response?.data?.detail || err.message });
      lookup(shipment?._id);
    } finally {
      setBusy(false);
      setJobProgress(null);
    }
  };

  const retryGoodsReceipt = async () => {
    setBusy(true);
    setJobProgress(null);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/retry-goods-receipt`);
      const result = await pollGrnJob(data.job_id);
      setShipment(result);
      if (result.sap_sync_status === "posted") {
        toast.success("Goods Receipt posted to SAP");
      } else {
        toast.warning("Still pending", { description: JSON.stringify(result.sap_gr_result) });
      }
    } catch (err) {
      toast.error("Retry failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
      setJobProgress(null);
    }
  };

  const retryMovement = async () => {
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/retry-movement`);
      setShipment(data);
      if (data.sap_movement_status === "posted") {
        toast.success(`Stock moved to ${data.site_id}/${data.warehouse_id}`);
      } else {
        toast.warning("Movement still pending", { description: JSON.stringify(data.sap_movement_result) });
      }
    } catch (err) {
      toast.error("Retry failed", { description: err?.response?.data?.detail || err.message });
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

  const openDiscrepancy = () => {
    setDiscReason("");
    setDiscItems({});
    setDiscOpen(true);
  };

  const toggleDiscItem = (poNumber, itemNumber) => {
    const key = `${poNumber}::${itemNumber}`;
    setDiscItems((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const submitDiscrepancy = async () => {
    const items = Object.entries(discItems)
      .filter(([, checked]) => checked)
      .map(([key]) => {
        const [po_number, item_number] = key.split("::");
        return { po_number, item_number };
      });
    if (!discReason.trim()) {
      toast.error("A discrepancy reason is required");
      return;
    }
    if (items.length === 0) {
      toast.error("Select at least one mismatched line item");
      return;
    }
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/discrepancy`, { reason: discReason, items });
      setShipment(data);
      setDiscOpen(false);
      toast.success("Discrepancy logged - shipment parked until the vendor edits it or you override");
      loadPending();
    } catch (err) {
      toast.error("Could not log discrepancy", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
    }
  };

  const isActionable = shipment && (shipment.status === "in_transit" || shipment.status === "discrepancy");

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
              <Badge className={STATUS_BADGE[shipment.status].className} data-testid="grn-status-badge">{STATUS_BADGE[shipment.status].label}</Badge>
            </div>

            {shipment.status === "discrepancy" && (
              <div className="mt-4 text-sm px-3 py-2.5 rounded-sm border border-[#E02424]/30 bg-[#E02424]/5 text-[#B91C1C]" data-testid="grn-discrepancy-banner">
                <div className="flex items-center gap-2 font-semibold"><WarningCircle size={16} weight="fill" /> Discrepancy logged by {shipment.discrepancy_marked_by}</div>
                <div className="mt-1">{shipment.discrepancy_reason}</div>
                <div className="mt-1 text-xs">
                  Flagged items: {(shipment.discrepancy_items || []).map((it) => `${it.po_number}/${it.item_number}`).join(", ")}
                </div>
                <div className="mt-1 text-xs text-[#5B738B]">Ask the vendor to edit this shipment - saving their edit will automatically bring it back to "In Transit" for re-review.</div>
              </div>
            )}

            <table className="w-full text-sm mt-4 border-collapse">
              <thead className="text-[#5B738B] text-xs uppercase">
                <tr>
                  <th className="text-left py-1 font-semibold">PO Number</th>
                  <th className="text-left py-1 font-semibold">Item</th>
                  <th className="text-left py-1 font-semibold">Description</th>
                  <th className="text-right py-1 font-semibold">Ship Qty</th>
                  <th className="text-right py-1 font-semibold">PO Qty</th>
                  <th className="text-right py-1 font-semibold">Actual Qty</th>
                </tr>
              </thead>
              <tbody>
                {shipment.items.map((it, i) => {
                  const key = `${it.po_number}::${it.item_number}`;
                  return (
                    <tr key={i} className="border-t border-[#CBD3DB]">
                      <td className="py-1.5 font-data">{it.po_number}</td>
                      <td className="py-1.5 font-data">{it.item_number}</td>
                      <td className="py-1.5">{it.description}</td>
                      <td className="py-1.5 text-right font-data font-semibold">{it.ship_qty} {it.unit_of_measure}</td>
                      <td className="py-1.5 text-right font-data text-[#5B738B]">{it.po_qty}</td>
                      <td className="py-1.5 text-right">
                        {isActionable ? (
                          <Input
                            type="number"
                            value={actualQtys[key] ?? ""}
                            onChange={(e) => setActualQtys((prev) => ({ ...prev, [key]: e.target.value }))}
                            className="w-24 h-7 text-right font-data rounded-sm border-[#CBD3DB] ml-auto"
                            data-testid={`grn-actual-qty-input-${key}`}
                          />
                        ) : (
                          <span className="font-data font-semibold" data-testid={`grn-actual-qty-value-${key}`}>{it.actual_qty ?? it.ship_qty} {it.unit_of_measure}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {isActionable && (
                  <tr><td colSpan={6} className="py-1.5 text-xs text-[#5B738B]">Actual Qty defaults to Ship Qty - adjust only if the physical count differs.</td></tr>
                )}
              </tbody>
            </table>

            {busy && jobProgress && (
              <div className="mt-3 text-xs text-[#5B738B] flex items-center gap-2 bg-[#F1F5F9] rounded-sm px-3 py-2" data-testid="grn-job-progress">
                <ArrowsClockwise size={12} className="animate-spin" />
                Posting to SAP... {jobProgress.progress_total > 1 ? `(PO ${Math.min(jobProgress.progress_current + 1, jobProgress.progress_total)}/${jobProgress.progress_total})` : ""}
              </div>
            )}

            {isActionable && (
              <div className="mt-5 border-t border-[#CBD3DB] pt-4 space-y-3">
                <div className="grid sm:grid-cols-4 gap-3">
                  <div>
                    <Label className="text-xs text-[#5B738B]">Supplier Invoice Number</Label>
                    <Input
                      placeholder="e.g. INV-4521"
                      value={supplierDocNum}
                      onChange={(e) => setSupplierDocNum(e.target.value)}
                      className="rounded-sm border-[#CBD3DB] mt-1"
                      data-testid="grn-supplier-doc-num-input"
                    />
                  </div>
                  <div>
                    <Label className="text-xs text-[#5B738B]">Bill Date</Label>
                    <Input
                      type="date"
                      value={billDate}
                      onChange={(e) => setBillDate(e.target.value)}
                      className="rounded-sm border-[#CBD3DB] mt-1"
                      data-testid="grn-bill-date-input"
                    />
                  </div>
                  <div>
                    <Label className="text-xs text-[#5B738B]">Site</Label>
                    <Select value={siteId} onValueChange={setSiteId} disabled={sites.length === 1}>
                      <SelectTrigger className="rounded-sm border-[#CBD3DB] mt-1" data-testid="grn-site-select">
                        <SelectValue placeholder="Select site" />
                      </SelectTrigger>
                      <SelectContent>
                        {sites.map((s) => (<SelectItem key={s} value={s} data-testid={`grn-site-option-${s}`}>{s}</SelectItem>))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label className="text-xs text-[#5B738B]">Warehouse</Label>
                    <Select value={warehouseId} onValueChange={setWarehouseId} disabled={!siteId || warehousesLoading}>
                      <SelectTrigger className="rounded-sm border-[#CBD3DB] mt-1" data-testid="grn-warehouse-select">
                        <SelectValue placeholder={warehousesLoading ? "Loading..." : "Select warehouse"} />
                      </SelectTrigger>
                      <SelectContent>
                        {warehouses.map((w) => (
                          <SelectItem key={w.warehouse_id} value={w.warehouse_id} data-testid={`grn-warehouse-option-${w.warehouse_id}`}>
                            {w.warehouse_name || w.warehouse_id} ({w.warehouse_id})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="flex gap-2">
                  <Button onClick={approve} disabled={busy} className="rounded-sm bg-[#10B981] hover:bg-[#0B7A56] transition-colors duration-150" data-testid="grn-approve-button">
                    <CheckCircle size={14} className="mr-1" /> {busy ? "Posting..." : "Approve & Post to SAP"}
                  </Button>
                  <Button variant="outline" onClick={() => setRejectOpen(true)} disabled={busy} className="rounded-sm border-[#E02424]/40 text-[#B91C1C]" data-testid="grn-reject-button">
                    <XCircle size={14} className="mr-1" /> Reject
                  </Button>
                  {shipment.status === "in_transit" && (
                    <Button variant="outline" onClick={openDiscrepancy} disabled={busy} className="rounded-sm border-[#E3A008]/50 text-[#8A6116]" data-testid="grn-mark-discrepancy-button">
                      <WarningCircle size={14} className="mr-1" /> Mark Discrepancy
                    </Button>
                  )}
                </div>
              </div>
            )}

            {shipment.status === "approved" && !busy && (
              <div className="mt-4 space-y-2">
                <div className="text-sm px-3 py-2 rounded-sm flex items-center justify-between gap-2 border" data-testid="grn-sap-sync-status"
                     style={shipment.sap_sync_status === "posted" ? { color: "#0B7A56", background: "rgba(16,185,129,0.1)", borderColor: "rgba(16,185,129,0.3)" } : { color: "#B45309", background: "rgba(227,160,8,0.1)", borderColor: "rgba(227,160,8,0.3)" }}>
                  <span className="flex items-center gap-2">
                    {shipment.sap_sync_status === "posted" ? <CheckCircle size={16} /> : <PlugsConnected size={16} />}
                    {shipment.sap_sync_status === "posted"
                      ? "Goods Receipt posted to SAP"
                      : shipment.sap_sync_status === "skipped"
                      ? (shipment.sap_gr_result?.per_po?.[0]?.error || "PO not found in SAP - check it's released")
                      : `SAP posting pending${shipment.sap_gr_result?.per_po?.find((p) => p.error)?.error ? ` - ${shipment.sap_gr_result.per_po.find((p) => p.error).error}` : ""}`}
                  </span>
                  {shipment.sap_sync_status !== "posted" && (
                    <Button size="sm" variant="outline" onClick={retryGoodsReceipt} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-retry-goods-receipt-button">
                      <ArrowsClockwise size={12} className="mr-1" /> Retry
                    </Button>
                  )}
                </div>
                <div className="text-sm px-3 py-2 rounded-sm flex items-center justify-between gap-2 border" data-testid="grn-sap-movement-status"
                     style={shipment.sap_movement_status === "posted" ? { color: "#0B7A56", background: "rgba(16,185,129,0.1)", borderColor: "rgba(16,185,129,0.3)" } : { color: "#B45309", background: "rgba(227,160,8,0.1)", borderColor: "rgba(227,160,8,0.3)" }}>
                  <span className="flex items-center gap-2">
                    {shipment.sap_movement_status === "posted" ? <CheckCircle size={16} /> : <PlugsConnected size={16} />}
                    {shipment.sap_movement_status === "posted"
                      ? `Stock moved to ${shipment.site_id}/${shipment.warehouse_id}`
                      : shipment.sap_movement_status === "not_applicable"
                      ? "Warehouse movement skipped - Goods Receipt has not posted to SAP yet"
                      : `Warehouse movement pending${shipment.warehouse_id ? ` (target ${shipment.site_id}/${shipment.warehouse_id})` : ""}`}
                  </span>
                  {shipment.sap_movement_status !== "posted" && shipment.sap_sync_status === "posted" && (
                    <Button size="sm" variant="outline" onClick={retryMovement} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-retry-movement-button">
                      <ArrowsClockwise size={12} className="mr-1" /> Retry
                    </Button>
                  )}
                </div>
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

      <Dialog open={discOpen} onOpenChange={setDiscOpen}>
        <DialogContent className="rounded-sm" data-testid="grn-discrepancy-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Mark Discrepancy</DialogTitle>
            <DialogDescription>Select the mismatched line item(s) and explain what doesn't match - the vendor will need to edit this shipment before it can be re-reviewed.</DialogDescription>
          </DialogHeader>
          <Textarea
            placeholder="e.g. Invoice shows 500 EA but only 480 EA physically received on item 2"
            value={discReason}
            onChange={(e) => setDiscReason(e.target.value)}
            className="rounded-sm border-[#CBD3DB]"
            data-testid="grn-discrepancy-reason-input"
          />
          <div className="space-y-1.5 max-h-56 overflow-y-auto border border-[#CBD3DB] rounded-sm p-2">
            {shipment?.items.map((it, i) => {
              const key = `${it.po_number}::${it.item_number}`;
              return (
                <label key={i} className="flex items-center gap-2 text-sm cursor-pointer py-1" data-testid={`grn-discrepancy-item-label-${key}`}>
                  <Checkbox checked={!!discItems[key]} onCheckedChange={() => toggleDiscItem(it.po_number, it.item_number)} data-testid={`grn-discrepancy-item-checkbox-${key}`} />
                  <span className="font-data">{it.po_number}/{it.item_number}</span>
                  <span className="text-[#5B738B]">{it.description} · {it.ship_qty} {it.unit_of_measure}</span>
                </label>
              );
            })}
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setDiscOpen(false)}>Cancel</Button>
            <Button onClick={submitDiscrepancy} disabled={busy} className="rounded-sm bg-[#E3A008] hover:bg-[#B87F06] text-white transition-colors duration-150" data-testid="grn-discrepancy-confirm-button">Log Discrepancy</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
