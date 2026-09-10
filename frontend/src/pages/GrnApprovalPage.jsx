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
import { Progress } from "@/components/ui/progress";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const STATUS_BADGE = {
  in_transit: { label: "In Transit", className: "bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89] rounded-sm" },
  discrepancy: { label: "Discrepancy", className: "bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA] rounded-sm" },
  approved: { label: "Received", className: "bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6] rounded-sm" },
  rejected: { label: "Rejected", className: "bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA] rounded-sm" },
};

// Sep 2 2026 (user's ask: step-by-step visibility + a reverse timer
// instead of a plain spinner) - matches the `phase` strings server.py
// forwards from sap_playwright_supplier_pgr_service.py's progress_cb.
const GRN_STEP_LABELS = {
  searching: "Searching for PO",
  opening_receipt: "Opening Goods Receipt for PO",
  entering_quantities: "Entering quantities for PO",
  saving: "Saving to SAP for PO",
};
const GRN_PHASE_LABELS = {
  queued: "Queued...",
  logging_in: "Connecting to SAP...",
  retrying: "A step failed - retrying automatically...",
  moving_stock: "Moving stock into the warehouse...",
  done: "Done",
};
function describeGrnPhase(phase) {
  if (!phase) return "Starting...";
  if (GRN_PHASE_LABELS[phase]) return GRN_PHASE_LABELS[phase];
  const [step, po, poCount] = phase.split(":");
  const label = GRN_STEP_LABELS[step] || step;
  return po ? `${label} ${po}${poCount ? ` (${poCount})` : ""}` : label;
}
// Rough empirical average per discrete step, purely for the countdown's
// display - the real remaining time is unknowable in advance (live SAP
// UI automation), so this clamps at "Almost there..." instead of ever
// going negative or over-promising.
const GRN_SECONDS_PER_STEP = 9;

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
  const [jobElapsed, setJobElapsed] = useState(0);

  useEffect(() => {
    if (!busy) return;
    setJobElapsed(0);
    const start = Date.now();
    const tick = setInterval(() => setJobElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(tick);
  }, [busy]);

  const [discOpen, setDiscOpen] = useState(false);
  const [discReason, setDiscReason] = useState("");
  const [discItems, setDiscItems] = useState({});

  // Sep 10 2026, user's explicit ask: a browsable list of already-Received
  // GRNs, with a popup showing the full trail (supplier code, bill number,
  // SAP inbound delivery number, item-level qty/unit/warehouse) - so staff
  // don't need to remember/re-type a shipment code just to confirm a GRN
  // already went through.
  const [confirmed, setConfirmed] = useState([]);
  const [confirmedDetail, setConfirmedDetail] = useState(null);

  const loadPending = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/grn/shipments`, { params: { status: "in_transit" } });
      setPending(data.shipments || []);
    } catch (err) {
      toast.error("Could not load pending shipments", { description: err?.response?.data?.detail || err.message });
    }
  };

  const loadConfirmed = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/grn/shipments`, { params: { status: "approved" } });
      setConfirmed(data.shipments || []);
    } catch (err) {
      toast.error("Could not load confirmed GRNs", { description: err?.response?.data?.detail || err.message });
    }
  };

  const inboundDeliveryIds = (s) => [...new Set((s.sap_gr_result?.per_po || []).map((p) => p.inbound_delivery_id).filter(Boolean))];

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
    loadConfirmed();
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
      .then(({ data }) => {
        const whs = data.warehouses || [];
        setWarehouses(whs);
        // "Warehouse should always be QC default" (mrp vendor side changes.docx,
        // Sep 2026) - pre-select QC when this site actually has one, still
        // editable since not every site has a QC logistics area today.
        const qc = whs.find((w) => (w.warehouse_id || "").split("-").pop() === "QC");
        if (qc) setWarehouseId(qc.warehouse_id);
      })
      .catch((err) => toast.error("Could not load warehouses", { description: err?.response?.data?.detail || err.message }))
      .finally(() => setWarehousesLoading(false));
  }, [siteId]);

  // "RI and RT Site should be non-editable and pre-fixed based on shipment
  // code" (Sep 2026): the Site dropdown only ever offers sites that belong
  // to THIS shipment's buying entity (RI/RT), returned by the lookup as
  // `allowed_site_ids` - auto-locks (disabled) whenever that narrows to
  // exactly one, same as the old single-site-binding auto-lock. A LEGACY
  // shipment (no buyer_code at all) falls back to every known site since
  // its entity is unknown - but once the entity IS known, an empty
  // `allowed_site_ids` means "you're not bound to any of its sites", which
  // must NOT silently fall back to showing every site (testing_agent,
  // iteration_140 - the old `.length ? filter : sites` did exactly that).
  const computeEligibleSites = (shipmentDoc, sitesList) => {
    if (!shipmentDoc || !shipmentDoc.buyer_code) return sitesList;
    return sitesList.filter((s) => (shipmentDoc.allowed_site_ids || []).includes(s));
  };
  const eligibleSites = computeEligibleSites(shipment, sites);
  const siteAccessBlocked = !!shipment?.site_access_blocked;

  // Recomputed whenever the looked-up shipment OR the site list itself
  // changes (fixes a race where a lookup could resolve before loadSites()'s
  // own async call landed, iteration_140) rather than only inside lookup().
  useEffect(() => {
    if (!shipment) return;
    const eligible = computeEligibleSites(shipment, sites);
    setSiteId(eligible.length === 1 ? eligible[0] : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shipment, sites]);

  const resetApprovalForm = () => {
    setSupplierDocNum("");
    setWarehouseId("");
    setBillDate("");
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
      loadConfirmed();
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
    <div className="min-h-screen bg-[#F2F4F7] font-sans" data-testid="grn-approval-page">
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
        <ErpConnectionStatus />
      </header>
      <div className="flex-1 overflow-auto max-w-[1400px] w-full mx-auto p-4 md:p-6 space-y-4">
        <div className="flex items-center gap-2 mb-1">
          <Truck size={18} weight="fill" className="text-[#004B87]" />
          <h1 className="font-heading text-xl font-bold text-[#1D2939]">GRN Approval</h1>
        </div>
        <p className="text-sm text-[#475467]">Enter the 6-character shipment code from the delivery paperwork, physically match the goods and supplier invoice, then approve.</p>

        <div className="bg-white border border-[#D0D5DD] rounded-sm p-3 flex items-center gap-3 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)]">
          <Input
            placeholder="e.g. A3F9K2"
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && lookup()}
            className="h-8 font-data uppercase w-64 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
            maxLength={6}
            data-testid="grn-code-input"
          />
          <Button onClick={() => lookup()} disabled={searching} className="h-8 rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D] text-[13px] font-bold transition-colors" data-testid="grn-code-search-button">
            <MagnifyingGlass size={14} className="mr-1" /> {searching ? "Searching..." : "Lookup"}
          </Button>
        </div>
        {searchError && <div className="text-sm text-[#B91C1C] mt-2" data-testid="grn-search-error">{searchError}</div>}

        {shipment && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] p-5" data-testid="grn-shipment-detail">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-lg font-data font-bold text-[#004B87]">{shipment._id}</div>
                <div className="text-sm text-[#475467]">{shipment.company_name} ({shipment.vendor_code}) · {[...new Set(shipment.items.map((it) => it.po_number))].map((p) => `PO ${p}`).join(", ")}</div>
              </div>
              <Badge className={STATUS_BADGE[shipment.status].className} data-testid="grn-status-badge">{STATUS_BADGE[shipment.status].label}</Badge>
            </div>

            {shipment.buyer_entity_name && (
              <div className="mt-2 text-xs text-[#475467]" data-testid="grn-buyer-entity">
                Buyer entity: <span className="font-semibold text-[#1D2939]">{shipment.buyer_entity_name}</span>
                {eligibleSites.length === 1 && <span> · Site locked to {eligibleSites[0]} for this entity</span>}
              </div>
            )}

            {siteAccessBlocked && (
              <div className="mt-3 text-sm px-3 py-2.5 rounded-sm border border-[#E02424]/30 bg-[#E02424]/5 text-[#B91C1C]" data-testid="grn-site-access-blocked-banner">
                <div className="flex items-center gap-2 font-semibold"><WarningCircle size={16} weight="fill" /> You're not bound to any site for {shipment.buyer_entity_name}</div>
                <div className="mt-1 text-xs">Ask a Super Admin to grant you Store Assignment access to one of this entity's sites before this shipment can be approved.</div>
              </div>
            )}

            {shipment.status === "discrepancy" && (
              <div className="mt-4 text-sm px-3 py-2.5 rounded-sm border border-[#E02424]/30 bg-[#E02424]/5 text-[#B91C1C]" data-testid="grn-discrepancy-banner">
                <div className="flex items-center gap-2 font-semibold"><WarningCircle size={16} weight="fill" /> Discrepancy logged by {shipment.discrepancy_marked_by}</div>
                <div className="mt-1">{shipment.discrepancy_reason}</div>
                <div className="mt-1 text-xs">
                  Flagged items: {(shipment.discrepancy_items || []).map((it) => `${it.po_number}/${it.item_number}`).join(", ")}
                </div>
                <div className="mt-1 text-xs text-[#475467]">Ask the vendor to edit this shipment - saving their edit will automatically bring it back to "In Transit" for re-review.</div>
              </div>
            )}

            <table className="border-collapse w-full text-[13px] mt-4 border border-[#D0D5DD] rounded-sm overflow-hidden">
              <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                <tr>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">PO Number</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">Item</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">Description</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Ship Qty</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Open PO Qty</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Actual Qty</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">PO Price</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Line Value</th>
                </tr>
              </thead>
              <tbody>
                {shipment.items.map((it, i) => {
                  const key = `${it.po_number}::${it.item_number}`;
                  const effectiveQty = Number(actualQtys[key] ?? it.actual_qty ?? it.ship_qty ?? 0);
                  const lineValue = it.unit_price != null ? effectiveQty * it.unit_price : null;
                  return (
                    <tr key={i} className="bg-white odd:bg-[#F9FAFB]">
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{it.po_number}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{it.item_number}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1">{it.description}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data font-semibold">{it.ship_qty} {it.unit_of_measure}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data text-[#475467]" data-testid={`grn-item-open-po-qty-${i}`}>{it.open_po_qty ?? it.po_qty}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right">
                        {isActionable ? (
                          <Input
                            type="number"
                            value={actualQtys[key] ?? ""}
                            onChange={(e) => setActualQtys((prev) => ({ ...prev, [key]: e.target.value }))}
                            className="w-24 h-7 text-right font-data rounded-sm border-[#D0D5DD] ml-auto"
                            data-testid={`grn-actual-qty-input-${key}`}
                          />
                        ) : (
                          <span className="font-data font-semibold" data-testid={`grn-actual-qty-value-${key}`}>{it.actual_qty ?? it.ship_qty} {it.unit_of_measure}</span>
                        )}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data text-[#475467]" data-testid={`grn-item-unit-price-${i}`}>
                        {it.unit_price != null ? `${it.currency || ""} ${it.unit_price.toFixed(2)}` : "-"}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data font-semibold text-[#1D2939]" data-testid={`grn-item-line-value-${i}`}>
                        {lineValue != null ? `${it.currency || ""} ${lineValue.toFixed(2)}` : "-"}
                      </td>
                    </tr>
                  );
                })}
                {isActionable && (
                  <tr><td colSpan={8} className="border border-[#D0D5DD] px-2 py-1 text-xs text-[#475467]">Actual Qty defaults to Ship Qty - adjust only if the physical count differs. PO Price/Line Value are for reference only, from SAP's last cached rate.</td></tr>
                )}
              </tbody>
            </table>

            {busy && jobProgress && (
              <div className="mt-3 rounded-sm border border-[#EAECF0] bg-[#F9FAFB] px-3 py-3 space-y-2" data-testid="grn-job-progress">
                <div className="flex items-center justify-between text-xs">
                  <div className="flex items-center gap-2 text-[#344054] font-medium">
                    <ArrowsClockwise size={12} className="animate-spin text-[#475467]" />
                    <span data-testid="grn-job-progress-step">{describeGrnPhase(jobProgress.phase)}</span>
                  </div>
                  <span className="text-[#667085] font-data" data-testid="grn-job-progress-timer">
                    {(() => {
                      const remaining = GRN_SECONDS_PER_STEP * jobProgress.progress_total - jobElapsed;
                      return remaining > 0 ? `~${remaining}s left` : "Almost there...";
                    })()}
                  </span>
                </div>
                <Progress
                  value={Math.min(100, Math.round((jobProgress.progress_current / Math.max(1, jobProgress.progress_total)) * 100))}
                  className="h-1.5"
                  data-testid="grn-job-progress-bar"
                />
                <div className="text-[11px] text-[#98A2B3] font-data">Elapsed: {jobElapsed}s</div>
              </div>
            )}

            {isActionable && (
              <div className="mt-5 border-t border-[#D0D5DD] pt-4 space-y-3">
                <div className="grid sm:grid-cols-4 gap-3">
                  <div>
                    <Label className="text-xs text-[#475467]">Supplier Invoice Number</Label>
                    <Input
                      placeholder="e.g. INV-4521"
                      value={supplierDocNum}
                      onChange={(e) => setSupplierDocNum(e.target.value)}
                      className="rounded-sm border-[#D0D5DD] mt-1"
                      data-testid="grn-supplier-doc-num-input"
                    />
                  </div>
                  <div>
                    <Label className="text-xs text-[#475467]">Bill Date</Label>
                    <Input
                      type="date"
                      value={billDate}
                      onChange={(e) => setBillDate(e.target.value)}
                      className="rounded-sm border-[#D0D5DD] mt-1"
                      data-testid="grn-bill-date-input"
                    />
                  </div>
                  <div>
                    <Label className="text-xs text-[#475467]">Site</Label>
                    <Select value={siteId} onValueChange={setSiteId} disabled={siteAccessBlocked || eligibleSites.length === 1}>
                      <SelectTrigger className="rounded-sm border-[#D0D5DD] mt-1" data-testid="grn-site-select">
                        <SelectValue placeholder="Select site" />
                      </SelectTrigger>
                      <SelectContent>
                        {eligibleSites.map((s) => (<SelectItem key={s} value={s} data-testid={`grn-site-option-${s}`}>{s}</SelectItem>))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label className="text-xs text-[#475467]">Warehouse</Label>
                    <Select value={warehouseId} onValueChange={setWarehouseId} disabled={siteAccessBlocked || !siteId || warehousesLoading}>
                      <SelectTrigger className="rounded-sm border-[#D0D5DD] mt-1" data-testid="grn-warehouse-select">
                        <SelectValue placeholder={warehousesLoading ? "Loading..." : (siteId ? "Select warehouse" : "Select a site first")} />
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
                  <Button onClick={approve} disabled={busy || siteAccessBlocked} className="h-8 rounded-sm bg-[#027A48] hover:bg-[#02623A] text-white px-4 text-[13px] font-bold transition-colors" data-testid="grn-approve-button">
                    <CheckCircle size={14} className="mr-1" /> {busy ? "Posting..." : "Approve & Post to SAP"}
                  </Button>
                  <Button onClick={() => setRejectOpen(true)} disabled={busy} className="h-8 rounded-sm bg-[#B42318] hover:bg-[#912018] text-white px-4 text-[13px] font-bold transition-colors" data-testid="grn-reject-button">
                    <XCircle size={14} className="mr-1" /> Reject
                  </Button>
                  {shipment.status === "in_transit" && (
                    <Button onClick={openDiscrepancy} disabled={busy} className="h-8 rounded-sm bg-[#B54708] hover:bg-[#93370D] text-white px-4 text-[13px] font-bold transition-colors" data-testid="grn-mark-discrepancy-button">
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
                {shipment.sap_sync_status === "posted" && shipment.sap_gr_result?.per_po?.some((p) => p.inbound_delivery_id) && (
                  <div className="text-xs text-[#667085] px-3 font-data" data-testid="grn-inbound-delivery-ids">
                    SAP Inbound Delivery #: {shipment.sap_gr_result.per_po.filter((p) => p.inbound_delivery_id).map((p) => p.inbound_delivery_id).join(", ")}
                  </div>
                )}
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

        <h2 className="font-heading text-sm font-bold uppercase tracking-wider text-[#344054] mt-8">Pending Shipments</h2>
        <div className="mt-3 bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto">
          <table className="w-full text-[13px] border-collapse">
            <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
              <tr>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Code</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Vendor</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">PO Numbers</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Created</th>
              </tr>
            </thead>
            <tbody>
              {pending.length === 0 && (
                <tr><td colSpan={4} className="border border-[#D0D5DD] px-3 py-6 text-center text-[#475467]" data-testid="grn-pending-empty">No shipments awaiting GRN approval.</td></tr>
              )}
              {pending.map((s) => (
                <tr key={s._id} className="cursor-pointer bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" onClick={() => lookup(s._id)} data-testid={`grn-pending-row-${s._id}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-data font-bold text-[#004B87]">{s._id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1">{s.company_name}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-data">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{new Date(s.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <h2 className="font-heading text-sm font-bold uppercase tracking-wider text-[#344054] mt-8">Confirmed GRNs</h2>
        <p className="text-xs text-[#475467] -mt-1">Click a row for the full receipt trail - supplier bill number, SAP inbound delivery #, and item-level qty/unit/warehouse.</p>
        <div className="mt-3 bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto">
          <table className="w-full text-[13px] border-collapse">
            <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
              <tr>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Code</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Vendor</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">PO Numbers</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Supplier Invoice No</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">SAP Inbound Delivery #</th>
                <th className="border border-[#D0D5DD] p-1.5 text-left">Approved</th>
              </tr>
            </thead>
            <tbody>
              {confirmed.length === 0 && (
                <tr><td colSpan={6} className="border border-[#D0D5DD] px-3 py-6 text-center text-[#475467]" data-testid="grn-confirmed-empty">No confirmed GRNs yet.</td></tr>
              )}
              {confirmed.map((s) => (
                <tr key={s._id} className="cursor-pointer bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" onClick={() => setConfirmedDetail(s)} data-testid={`grn-confirmed-row-${s._id}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-data font-bold text-[#004B87]">{s._id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1">{s.company_name}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-data">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-data">{s.supplier_doc_num || "\u2014"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-data">{inboundDeliveryIds(s).join(", ") || "\u2014"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.approved_at ? new Date(s.approved_at).toLocaleString() : "\u2014"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
        <DialogContent className="rounded-sm" data-testid="grn-reject-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Reject Shipment</DialogTitle>
            <DialogDescription>This reason will be shown to the vendor.</DialogDescription>
          </DialogHeader>
          <Textarea placeholder="Reason" value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} className="rounded-sm border-[#D0D5DD]" data-testid="grn-reject-reason-input" />
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setRejectOpen(false)}>Cancel</Button>
            <Button onClick={reject} disabled={busy} className="rounded-sm bg-[#E02424] hover:bg-[#B91C1C] transition-colors duration-150" data-testid="grn-reject-confirm-button">Confirm Reject</Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={discOpen} onOpenChange={setDiscOpen}>
        <DialogContent className="rounded-sm" data-testid="grn-discrepancy-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Mark Discrepancy</DialogTitle>
            <DialogDescription>Select the mismatched line item(s) and explain what doesn't match - the vendor will need to edit this shipment before it can be re-reviewed.</DialogDescription>
          </DialogHeader>
          <Textarea
            placeholder="e.g. Invoice shows 500 EA but only 480 EA physically received on item 2"
            value={discReason}
            onChange={(e) => setDiscReason(e.target.value)}
            className="rounded-sm border-[#D0D5DD]"
            data-testid="grn-discrepancy-reason-input"
          />
          <div className="space-y-1.5 max-h-56 overflow-y-auto border border-[#D0D5DD] rounded-sm p-2">
            {shipment?.items.map((it, i) => {
              const key = `${it.po_number}::${it.item_number}`;
              return (
                <label key={i} className="flex items-center gap-2 text-sm cursor-pointer py-1" data-testid={`grn-discrepancy-item-label-${key}`}>
                  <Checkbox checked={!!discItems[key]} onCheckedChange={() => toggleDiscItem(it.po_number, it.item_number)} data-testid={`grn-discrepancy-item-checkbox-${key}`} />
                  <span className="font-data">{it.po_number}/{it.item_number}</span>
                  <span className="text-[#475467]">{it.description} · {it.ship_qty} {it.unit_of_measure}</span>
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
      <Dialog open={!!confirmedDetail} onOpenChange={(o) => !o && setConfirmedDetail(null)}>
        <DialogContent className="rounded-sm max-w-2xl" data-testid="grn-confirmed-detail-dialog">
          {confirmedDetail && (
            <>
              <DialogHeader>
                <DialogTitle className="font-heading font-data text-[#004B87]" data-testid="grn-confirmed-detail-code">{confirmedDetail._id}</DialogTitle>
                <DialogDescription>{confirmedDetail.company_name} ({confirmedDetail.vendor_code})</DialogDescription>
              </DialogHeader>
              <div className="grid grid-cols-2 gap-y-2 gap-x-6 text-sm" data-testid="grn-confirmed-detail-meta">
                <div><span className="text-[#475467]">PO Number(s):</span> <span className="font-data font-semibold">{[...new Set(confirmedDetail.items.map((it) => it.po_number))].join(", ")}</span></div>
                <div><span className="text-[#475467]">Supplier Invoice No:</span> <span className="font-data font-semibold">{confirmedDetail.supplier_doc_num || "\u2014"}</span></div>
                <div><span className="text-[#475467]">Bill Date:</span> <span className="font-data font-semibold">{confirmedDetail.bill_date || "\u2014"}</span></div>
                <div><span className="text-[#475467]">SAP Inbound Delivery #:</span> <span className="font-data font-semibold">{inboundDeliveryIds(confirmedDetail).join(", ") || "\u2014"}</span></div>
                <div><span className="text-[#475467]">Site:</span> <span className="font-data font-semibold">{confirmedDetail.site_id}</span></div>
                <div><span className="text-[#475467]">Approved By:</span> <span className="font-semibold">{confirmedDetail.approved_by} · {confirmedDetail.approved_at ? new Date(confirmedDetail.approved_at).toLocaleString() : "\u2014"}</span></div>
              </div>
              <table className="border-collapse w-full text-[13px] mt-2 border border-[#D0D5DD] rounded-sm overflow-hidden">
                <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                  <tr>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Item Code</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Description</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-right">Qty</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Unit</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Warehouse</th>
                  </tr>
                </thead>
                <tbody>
                  {confirmedDetail.items.map((it, i) => (
                    <tr key={i} className="bg-white odd:bg-[#F9FAFB]" data-testid={`grn-confirmed-detail-item-${i}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data font-semibold">{it.product_id || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1">{it.description}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data">{it.actual_qty ?? it.ship_qty}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{it.unit_of_measure}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{confirmedDetail.site_id}/{confirmedDetail.warehouse_id}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
