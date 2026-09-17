import { useState, useEffect, useCallback, useRef, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import { Truck, Shield, CircleNotch, CaretDown, CaretUp, CheckCircle, ArrowRight, WarningCircle, Clock } from "@phosphor-icons/react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

// Rough average time SAP's own receipt screen takes per delivery
// (documented range 40-90s) - purely to render a reassuring, generic
// countdown; never shown to the user as anything more specific than
// "processing" - no SAP/browser wording ever reaches this page, by design.
const AVG_SECONDS_PER_DELIVERY = 65;

const formatQty = (v) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 }));
const formatDate = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const formatDuration = (seconds) => {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
};

const COMPLETED_STATUS_STYLE = {
  received: { label: "Received", cls: "bg-[#ECFDF3] text-[#027A48]" },
  partial: { label: "Partially Received", cls: "bg-[#FEF3C7] text-[#92400E]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

const STATUS_STYLE = {
  pending: { label: "Pending Receipt", cls: "bg-[#FEF3C7] text-[#92400E]" },
  partial: { label: "Partially Received", cls: "bg-[#FEE4E2] text-[#B42318]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

const etaText = (job, nowMs) => {
  const total = job?.progress_total || 1;
  const totalEstimateSeconds = total * AVG_SECONDS_PER_DELIVERY;
  let remaining;
  if (job?.processing_started_at) {
    const elapsed = (nowMs - new Date(job.processing_started_at).getTime()) / 1000;
    remaining = Math.max(0, Math.round(totalEstimateSeconds - elapsed));
  } else {
    remaining = Math.max(0, (total - (job?.progress_current || 0)) * AVG_SECONDS_PER_DELIVERY);
  }
  if (remaining <= 0) return "finishing up…";
  if (remaining < 60) return `est. ${remaining}s remaining`;
  return `est. ${Math.ceil(remaining / 60)} min remaining`;
};

const jobBadge = (job, nowMs) => {
  if (!job) return null;
  // A job can finish executing cleanly (status="done") yet the receipt
  // itself was rejected/partially rejected by SAP (result.status !=
  // "received") - the badge must reflect the receipt outcome, not just
  // whether the background job crashed (testing_agent iteration_134).
  const receiptFailed = job.status === "failed" || (job.status === "done" && job.result && job.result.status !== "received");
  if (receiptFailed) {
    return { icon: WarningCircle, cls: "text-[#B42318]", iconCls: "", label: "Failed", detail: job.error || job.result?.error };
  }
  if (job.status === "done") {
    return { icon: CheckCircle, cls: "text-[#027A48]", iconCls: "", label: "Done", detail: null };
  }
  if (job.phase === "queued") {
    return { icon: Clock, cls: "text-[#92400E]", iconCls: "", label: "Queued", detail: "Waiting for an available processing slot" };
  }
  return { icon: CircleNotch, cls: "text-[#0B6B74]", iconCls: "animate-spin", label: "Processing", detail: etaText(job, nowMs) };
};

// Per-line breakdown popover for the "Warehouse Move" column (user's
// explicit ask) - shows each item alongside its own real SAP Goods
// Movement ID (success) or its own failure reason, instead of a single
// summary line that hides which item got which movement.
const RelocationPopover = ({ relocation, stoId, children }) => (
  <Popover>
    <PopoverTrigger asChild>
      <button className="underline-offset-2 hover:underline text-left" data-testid={`inbound-receipts-completed-relocation-trigger-${stoId}`}>
        {children}
      </button>
    </PopoverTrigger>
    <PopoverContent className="w-72 p-3" align="start" data-testid={`inbound-receipts-completed-relocation-popover-${stoId}`}>
      <p className="text-xs font-semibold text-[#101828] mb-2">Warehouse Move — {relocation.to}</p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[#667085]">
            <th className="text-left font-medium py-1">Item</th>
            <th className="text-right font-medium py-1">Movement</th>
          </tr>
        </thead>
        <tbody>
          {(relocation.lines || []).map((l) => (
            <tr key={l.product_id} className="border-t border-[#EAECF0]">
              <td className="py-1.5 pr-2 font-mono text-[#344054]">{l.product_id}</td>
              <td className="py-1.5 text-right font-mono">
                {l.ok ? <span className="text-[#027A48]">GM {l.gac_id}</span> : <span className="text-[#B42318]">{l.error}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </PopoverContent>
  </Popover>
);

// Inbound STO Receipt (Aug 28 2026) - lets any logged-in user receive a
// multi-line STO in one click instead of the SAP UI showing one row per
// line. See server.py's /api/inbound-receipts/* and
// inbound_receipt_service.py for the backend logic this drives.
// Batch receiving + per-row job badges + reverse-counter progress added
// Aug 2026 (user's explicit ask) - wording never reveals SAP/browser
// automation is happening underneath.
export default function InboundReceiptsPage() {
  const { user } = useAuth();
  const [sites, setSites] = useState([]);
  const [siteFilter, setSiteFilter] = useState("all");
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  const [activeJobs, setActiveJobs] = useState({}); // { sto_id: {job_id, status, phase, progress_current, progress_total, processing_started_at, error} }
  const activeJobsRef = useRef(activeJobs);
  activeJobsRef.current = activeJobs;
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [activeTab, setActiveTab] = useState("pending"); // "pending" | "completed"
  const [completedOrders, setCompletedOrders] = useState([]);
  const [completedLoading, setCompletedLoading] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [retryingRelocationStoId, setRetryingRelocationStoId] = useState(null);

  const handleRetryReceiptRelocation = async (stoId) => {
    setRetryingRelocationStoId(stoId);
    try {
      await axios.post(`${API}/inbound-receipts/${stoId}/retry-receipt-relocation`);
      toast.success("Warehouse move retried.");
      loadCompletedOrders();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not retry the warehouse move.");
    } finally {
      setRetryingRelocationStoId(null);
    }
  };

  useEffect(() => {
    axios.get(`${API}/inbound-receipts/sites`).then(({ data }) => {
      setSites(data.sites || []);
      const defaultSite = (user?.bound_sites || []).find((s) => (data.sites || []).includes(s));
      if (defaultSite) setSiteFilter(defaultSite);
    }).catch(() => toast.error("Could not load receiving sites."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadCompletedOrders = useCallback(async () => {
    setCompletedLoading(true);
    try {
      const params = {};
      if (siteFilter !== "all") params.site_id = siteFilter;
      if (dateFrom) params.date_from = dateFrom;
      if (dateTo) params.date_to = dateTo;
      const { data } = await axios.get(`${API}/inbound-receipts/completed`, { params });
      setCompletedOrders(data.orders || []);
    } catch {
      toast.error("Could not load completed receipts.");
    } finally {
      setCompletedLoading(false);
    }
  }, [siteFilter, dateFrom, dateTo]);

  useEffect(() => {
    if (activeTab === "completed") loadCompletedOrders();
  }, [activeTab, loadCompletedOrders]);

  const loadOrders = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/inbound-receipts/pending`, {
        params: siteFilter === "all" ? {} : { site_id: siteFilter },
      });
      setOrders(data.orders || []);
      // Rehydrate any job the backend still shows as running that this
      // tab doesn't know about yet (page refresh, or started from
      // another tab/device) - so the badge never silently disappears
      // while the job is genuinely still on (user's explicit ask).
      setActiveJobs((prev) => {
        const next = { ...prev };
        (data.orders || []).forEach((o) => {
          if (o.active_job?.job_id && !next[o.sto_id]) {
            next[o.sto_id] = { job_id: o.active_job.job_id, status: "running", phase: o.active_job.phase, progress_current: o.active_job.progress_current, progress_total: o.active_job.progress_total, processing_started_at: o.active_job.processing_started_at };
          }
        });
        return next;
      });
    } catch {
      toast.error("Could not load pending receipts.");
    } finally {
      setLoading(false);
    }
  }, [siteFilter]);

  useEffect(() => { loadOrders(); }, [loadOrders]);

  // Ticks every second while any job is actively processing, purely so
  // the "est. Xs remaining" countdown visibly counts down instead of
  // only updating on the 2.5s poll cadence (the "reverse counter" ask).
  useEffect(() => {
    const anyRowProcessing = Object.values(activeJobs).some((j) => j.status === "running" && j.phase === "processing");
    if (!anyRowProcessing) return;
    const tick = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [activeJobs]);

  // Background poller for every batch/single job tracked in activeJobs -
  // keeps running independently of any dialog being open, so closing the
  // batch confirm dialog right after submitting never loses progress.
  useEffect(() => {
    const hasLive = Object.values(activeJobs).some((j) => j.status === "running");
    if (!hasLive) return;
    const interval = setInterval(async () => {
      const current = activeJobsRef.current;
      const liveEntries = Object.entries(current).filter(([, j]) => j.status === "running");
      if (liveEntries.length === 0) return;
      const updates = await Promise.all(liveEntries.map(async ([stoId, job]) => {
        try {
          const { data } = await axios.get(`${API}/inbound-receipts/receive-status/${job.job_id}`);
          return [stoId, { ...job, status: data.status, phase: data.phase, progress_current: data.progress_current, progress_total: data.progress_total, processing_started_at: data.processing_started_at || job.processing_started_at, error: data.error, result: data.result }];
        } catch {
          return [stoId, job];
        }
      }));
      let anyFinished = false;
      setActiveJobs((prev) => {
        const next = { ...prev };
        updates.forEach(([stoId, job]) => {
          next[stoId] = job;
          if (job.status === "done" || job.status === "failed") anyFinished = true;
        });
        return next;
      });
      if (anyFinished) {
        loadOrders();
        setTimeout(() => {
          setActiveJobs((prev) => {
            const next = {};
            Object.entries(prev).forEach(([stoId, job]) => {
              if (job.status === "running") next[stoId] = job;
            });
            return next;
          });
        }, 6000);
      }
    }, 2500);
    return () => clearInterval(interval);
  }, [activeJobs, loadOrders]);

  const startReceiveJobs = async (targetOrders) => {
    const results = await Promise.all(targetOrders.map(async (order) => {
      const items = order.items.map((it) => ({ line_no: it.line_no, received_qty: it.requested_qty }));
      try {
        const { data } = await axios.post(`${API}/inbound-receipts/${order.sto_id}/receive`, { items });
        return { stoId: order.sto_id, jobId: data.job_id, alreadyReceived: data.already_received };
      } catch (e) {
        return { stoId: order.sto_id, error: e?.response?.data?.detail || "Could not start" };
      }
    }));
    setActiveJobs((prev) => {
      const next = { ...prev };
      results.forEach((r) => {
        if (r.alreadyReceived) return;
        next[r.stoId] = r.jobId
          ? { job_id: r.jobId, status: "running", phase: "queued", progress_current: 0, progress_total: 1 }
          : { status: "failed", error: r.error };
      });
      return next;
    });
    return results;
  };

  // "Receive" button on a row (user's explicit ask, Sep 2026: no qty
  // dialog/progress popup - just receive the full shipped quantity
  // straight away, with a small inline progress indicator on the row
  // itself until it's done) - reuses the same background-job mechanism
  // the bulk action already relies on.
  const handleReceiveOne = async (order) => {
    const [result] = await startReceiveJobs([order]);
    if (result.alreadyReceived) {
      toast.success(`${order.sto_id} was already received.`);
      loadOrders();
    } else if (!result.jobId) {
      toast.error(result.error || "Could not start the receipt.");
    }
  };

  const toggleSelected = (stoId, checked) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(stoId); else next.delete(stoId);
      return next;
    });
  };

  const selectableOrders = orders.filter((o) => !activeJobs[o.sto_id] || activeJobs[o.sto_id].status !== "running");
  const allSelected = selectableOrders.length > 0 && selectableOrders.every((o) => selectedIds.has(o.sto_id));
  const toggleSelectAll = (checked) => {
    setSelectedIds(checked ? new Set(selectableOrders.map((o) => o.sto_id)) : new Set());
  };

  const submitBulkReceive = async () => {
    const targets = orders.filter((o) => selectedIds.has(o.sto_id));
    setBulkSubmitting(true);
    try {
      const results = await startReceiveJobs(targets);
      const started = results.filter((r) => r.jobId).length;
      toast.success(`Started receiving ${started} order${started === 1 ? "" : "s"} — you can keep working, progress shows on each row.`);
      setSelectedIds(new Set());
    } finally {
      setBulkSubmitting(false);
      setBulkConfirmOpen(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="inbound-receipts-page">
      <Toaster position="top-right" richColors />
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Inbound STO Receipt</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0"><NavTabs /></div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>
      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 py-8">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-6">
          <div>
            <h1 className="font-heading text-2xl sm:text-3xl font-bold text-[#101828] flex items-center gap-2">
              <Truck size={28} weight="fill" className="text-[#0B6B74]" />
              Inbound STO Receipt
            </h1>
            <p className="text-sm text-[#667085] mt-1">
              Receive a Stock Transfer Order in one click — closes every SAP delivery line for it automatically.
            </p>
          </div>
          <div className="w-full sm:w-56">
            <Select value={siteFilter} onValueChange={setSiteFilter}>
              <SelectTrigger data-testid="inbound-receipts-site-filter">
                <SelectValue placeholder="Receiving site" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Receiving Sites</SelectItem>
                {sites.map((s) => (
                  <SelectItem key={s} value={s}>{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex items-center gap-1 mb-5 bg-[#EEF2F1] p-1 rounded-lg w-fit" data-testid="inbound-receipts-tabs">
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${activeTab === "pending" ? "bg-white text-[#0B6B74] shadow-sm" : "text-[#667085] hover:text-[#344054]"}`}
            onClick={() => setActiveTab("pending")}
            data-testid="inbound-receipts-tab-pending"
          >
            Pending
          </button>
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${activeTab === "completed" ? "bg-white text-[#0B6B74] shadow-sm" : "text-[#667085] hover:text-[#344054]"}`}
            onClick={() => setActiveTab("completed")}
            data-testid="inbound-receipts-tab-completed"
          >
            Completed
          </button>
        </div>

        {activeTab === "completed" && (
          <div className="flex items-end gap-3 flex-wrap mb-5" data-testid="inbound-receipts-completed-filters">
            <div>
              <label className="text-xs font-medium text-[#667085] block mb-1">From</label>
              <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-40" data-testid="inbound-receipts-date-from" />
            </div>
            <div>
              <label className="text-xs font-medium text-[#667085] block mb-1">To</label>
              <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-40" data-testid="inbound-receipts-date-to" />
            </div>
            {(dateFrom || dateTo) && (
              <Button variant="outline" size="sm" onClick={() => { setDateFrom(""); setDateTo(""); }} data-testid="inbound-receipts-date-clear-btn">
                Clear dates
              </Button>
            )}
          </div>
        )}

        {activeTab === "pending" && (
        <Fragment>
        {selectedIds.size > 0 && (
          <div className="flex items-center justify-between bg-[#F0F9FA] border border-[#B4E4E8] rounded-lg px-4 py-2.5 mb-4" data-testid="inbound-receipts-bulk-bar">
            <span className="text-sm font-medium text-[#0B6B74]">{selectedIds.size} order{selectedIds.size === 1 ? "" : "s"} selected</span>
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => setSelectedIds(new Set())} data-testid="inbound-receipts-bulk-clear-btn">Clear</Button>
              <Button size="sm" onClick={() => setBulkConfirmOpen(true)} data-testid="inbound-receipts-bulk-receive-btn">
                Receive Selected ({selectedIds.size})
              </Button>
            </div>
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-24 text-[#667085]" data-testid="inbound-receipts-loading">
            <CircleNotch size={24} className="animate-spin mr-2" /> Loading pending receipts…
          </div>
        ) : orders.length === 0 ? (
          <div className="text-center py-24 text-[#667085] bg-white rounded-xl border border-[#EAECF0]" data-testid="inbound-receipts-empty">
            Nothing pending — every shipped STO for this site has been received.
          </div>
        ) : (
          <div className="bg-white rounded-xl border border-[#EAECF0] overflow-x-auto" data-testid="inbound-receipts-list">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <Checkbox
                      checked={allSelected}
                      onCheckedChange={(v) => toggleSelectAll(!!v)}
                      data-testid="inbound-receipt-select-all"
                    />
                  </TableHead>
                  <TableHead>STO</TableHead>
                  <TableHead>Route</TableHead>
                  <TableHead>Ship To</TableHead>
                  <TableHead>Shipped On</TableHead>
                  <TableHead>Lines</TableHead>
                  <TableHead className="min-w-[220px]">Status</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {orders.map((order) => {
                  const expanded = expandedId === order.sto_id;
                  const status = STATUS_STYLE[order.receipt_status] || STATUS_STYLE.pending;
                  const job = activeJobs[order.sto_id];
                  const badge = jobBadge(job, nowMs);
                  const rowBusy = job && job.status === "running";
                  return (
                    <Fragment key={order.sto_id}>
                      <TableRow data-testid={`inbound-receipt-row-${order.sto_id}`}>
                        <TableCell>
                          <Checkbox
                            checked={selectedIds.has(order.sto_id)}
                            onCheckedChange={(v) => toggleSelected(order.sto_id, !!v)}
                            disabled={rowBusy}
                            data-testid={`inbound-receipt-select-${order.sto_id}`}
                          />
                        </TableCell>
                        <TableCell>
                          <button
                            className="flex items-center gap-1 font-semibold text-[#101828] hover:text-[#0B6B74]"
                            onClick={() => setExpandedId(expanded ? null : order.sto_id)}
                            data-testid={`inbound-receipt-toggle-${order.sto_id}`}
                          >
                            {expanded ? <CaretUp size={14} /> : <CaretDown size={14} />}
                            {order.sto_id}
                          </button>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                          {order.outbound_delivery_ids.length > 0 && (
                            <div className="text-[11px] text-[#667085] font-mono mt-0.5" data-testid={`inbound-receipt-delivery-ids-${order.sto_id}`}>
                              SAP Delivery Notif: {order.outbound_delivery_ids.join(", ")}
                            </div>
                          )}
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-sm text-[#344054]">
                            {order.ship_from_site_id} <ArrowRight size={12} /> {order.ship_to_site_id}
                          </span>
                        </TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.ship_to_location_name || "—"}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{formatDate(order.created_at)}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.items.length}</TableCell>
                        <TableCell>
                          <span className={`text-xs font-medium px-2 py-1 rounded-full ${status.cls}`} data-testid={`inbound-receipt-status-${order.sto_id}`}>
                            {status.label}
                          </span>
                          {order.receipt_error && (
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs truncate" title={order.receipt_error}>{order.receipt_error}</div>
                          )}
                          {badge && (
                            <div className={`flex items-center gap-1 text-xs font-medium mt-1 whitespace-nowrap ${badge.cls}`} title={badge.detail || ""} data-testid={`inbound-receipt-job-badge-${order.sto_id}`}>
                              <badge.icon size={13} className={`shrink-0 ${badge.iconCls}`} />
                              <span className="truncate">{badge.label}{badge.label === "Processing" && badge.detail ? ` — ${badge.detail}` : ""}</span>
                            </div>
                          )}
                          {rowBusy && (
                            <div className="w-32 h-1 bg-[#D6EEF0] rounded-full mt-1.5 overflow-hidden" data-testid={`inbound-receipt-progress-bar-${order.sto_id}`}>
                              <div className="h-full w-1/3 bg-[#0B6B74] rounded-full animate-[pulse_1.5s_ease-in-out_infinite]" />
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button size="sm" onClick={() => handleReceiveOne(order)} disabled={rowBusy} data-testid={`inbound-receipt-receive-btn-${order.sto_id}`}>
                            {rowBusy ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}
                            Receive
                          </Button>
                        </TableCell>
                      </TableRow>
                      {expanded && (
                        <TableRow key={`${order.sto_id}-detail`}>
                          <TableCell colSpan={8} className="bg-[#F9FAFB]">
                            <div className="py-2">
                              <table className="w-full text-sm">
                                <thead>
                                  <tr className="text-[#667085] text-xs">
                                    <th className="text-left py-1">Line</th>
                                    <th className="text-left py-1">Product</th>
                                    <th className="text-left py-1">Description</th>
                                    <th className="text-right py-1">Qty</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {order.items.map((it) => (
                                    <tr key={it.line_no} data-testid={`inbound-receipt-item-${order.sto_id}-${it.line_no}`}>
                                      <td className="py-1">{it.line_no}</td>
                                      <td className="py-1 font-mono text-xs">{it.product_id}</td>
                                      <td className="py-1">{it.description}</td>
                                      <td className="py-1 text-right">{formatQty(it.requested_qty)} {it.unit_of_measure}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
        </Fragment>
        )}

        {activeTab === "completed" && (
          completedLoading ? (
            <div className="flex items-center justify-center py-24 text-[#667085]" data-testid="inbound-receipts-completed-loading">
              <CircleNotch size={24} className="animate-spin mr-2" /> Loading completed receipts…
            </div>
          ) : completedOrders.length === 0 ? (
            <div className="text-center py-24 text-[#667085] bg-white rounded-xl border border-[#EAECF0]" data-testid="inbound-receipts-completed-empty">
              No completed receipts found for this filter.
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-[#EAECF0] overflow-x-auto" data-testid="inbound-receipts-completed-list">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>STO</TableHead>
                    <TableHead>Route</TableHead>
                    <TableHead>Ship To</TableHead>
                    <TableHead>Received At</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Warehouse Move</TableHead>
                    <TableHead className="text-right">Time Taken</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {completedOrders.map((order) => {
                    const status = COMPLETED_STATUS_STYLE[order.receipt_status] || COMPLETED_STATUS_STYLE.received;
                    const relocation = order.receipt_relocation;
                    return (
                      <TableRow key={order.sto_id} data-testid={`inbound-receipts-completed-row-${order.sto_id}`}>
                        <TableCell>
                          <span className="font-semibold text-[#101828]">{order.sto_id}</span>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-sm text-[#344054]">
                            {order.ship_from_site_id} <ArrowRight size={12} /> {order.ship_to_site_id}
                          </span>
                        </TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.ship_to_location_name || "—"}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{formatDate(order.received_at || order.receipt_completed_at)}</TableCell>
                        <TableCell>
                          <span className={`text-xs font-medium px-2 py-1 rounded-full ${status.cls}`} data-testid={`inbound-receipts-completed-status-${order.sto_id}`}>
                            {status.label}
                          </span>
                          {order.receipt_error && (
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs truncate" title={order.receipt_error}>{order.receipt_error}</div>
                          )}
                        </TableCell>
                        <TableCell data-testid={`inbound-receipts-completed-relocation-${order.sto_id}`}>
                          {!relocation ? (
                            <span className="text-xs text-[#98A2B3]">—</span>
                          ) : relocation.status === "skipped_same_warehouse" ? (
                            <span className="text-xs text-[#667085]">Received directly into {relocation.to || order.ship_to_location_name}</span>
                          ) : relocation.status === "done" ? (
                            <RelocationPopover relocation={relocation} stoId={order.sto_id}>
                              <span className="text-xs text-[#027A48]">Moved to {relocation.to}</span>
                            </RelocationPopover>
                          ) : (
                            <div className="flex items-center gap-2">
                              <RelocationPopover relocation={relocation} stoId={order.sto_id}>
                                <span className="text-xs text-[#B42318]">
                                  {relocation.status === "partial" ? "Partially moved" : "Move failed"}
                                </span>
                              </RelocationPopover>
                              <Button
                                size="sm" variant="outline"
                                onClick={() => handleRetryReceiptRelocation(order.sto_id)}
                                disabled={retryingRelocationStoId === order.sto_id}
                                data-testid={`inbound-receipts-completed-retry-relocation-${order.sto_id}`}
                              >
                                {retryingRelocationStoId === order.sto_id ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}Retry
                              </Button>
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm text-[#344054]" data-testid={`inbound-receipts-completed-duration-${order.sto_id}`}>
                          {formatDuration(order.receipt_duration_seconds)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )
        )}
      </div>

      <Dialog open={bulkConfirmOpen} onOpenChange={(open) => !bulkSubmitting && setBulkConfirmOpen(open)}>
        <DialogContent className="max-w-md" data-testid="inbound-receipt-bulk-dialog">
          <DialogHeader>
            <DialogTitle>Receive {selectedIds.size} order{selectedIds.size === 1 ? "" : "s"}?</DialogTitle>
            <DialogDescription>
              Each order will be received at its full shipped quantity and closed in SAP. This runs in the
              background — you can keep working, progress shows on each row.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBulkConfirmOpen(false)} disabled={bulkSubmitting} data-testid="inbound-receipt-bulk-cancel-btn">
              Cancel
            </Button>
            <Button onClick={submitBulkReceive} disabled={bulkSubmitting} data-testid="inbound-receipt-bulk-confirm-btn">
              {bulkSubmitting ? <><CircleNotch size={16} className="animate-spin mr-2" /> Starting…</> : "Confirm"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
