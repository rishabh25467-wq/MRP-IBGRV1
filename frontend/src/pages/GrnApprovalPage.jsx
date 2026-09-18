import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { MagnifyingGlass, CheckCircle, XCircle, Truck, PlugsConnected, Shield, WarningCircle, ArrowsClockwise, LockKey } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Progress } from "@/components/ui/progress";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { useAuth } from "@/contexts/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const STATUS_BADGE = {
  in_transit: { label: "In Transit", className: "bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89] rounded-sm" },
  discrepancy: { label: "Discrepancy", className: "bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA] rounded-sm" },
  approved: { label: "Received", className: "bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6] rounded-sm" },
  rejected: { label: "Rejected", className: "bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA] rounded-sm" },
};

// Warehouse-move item/movement-ID breakdown popover (user's explicit
// ask, Sep 2026) - same pattern already used on the STO pages, applied
// here to sap_movement_result.per_item (product_id + the real SAP
// Goods Movement external_id/error per line).
const MovementPopover = ({ items, warehouseLabel, docCode, children }) => (
  <Popover>
    <PopoverTrigger asChild>
      <button className="underline-offset-2 hover:underline text-left" onClick={(e) => e.stopPropagation()} data-testid={`grn-movement-popover-trigger-${docCode}`}>
        {children}
      </button>
    </PopoverTrigger>
    <PopoverContent className="w-72 p-3" align="start" data-testid={`grn-movement-popover-${docCode}`}>
      <p className="text-xs font-semibold text-[#101828] mb-2">Warehouse Move{warehouseLabel ? ` — ${warehouseLabel}` : ""}</p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[#667085]">
            <th className="text-left font-medium py-1">Item</th>
            <th className="text-right font-medium py-1">Movement</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it, i) => (
            <tr key={`${it.product_id}-${i}`} className="border-t border-[#EAECF0]">
              <td className="py-1.5 pr-2 font-mono text-[#344054]">{it.product_id}</td>
              <td className="py-1.5 text-right font-mono">
                {it.ok !== false && it.external_id ? <span className="text-[#027A48]">GM {it.external_id}</span> : it.skipped ? <span className="text-[#667085]">{it.note || "Skipped"}</span> : <span className="text-[#B42318]">{it.error || "Failed"}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </PopoverContent>
  </Popover>
);

// Sep 10 2026, user's explicit ask: "do not show status received until
// the SAP inbound number is received" - internal `status` becomes
// "approved" the instant staff physically match goods (independent of
// SAP), so the top badge must NOT say "Received" (implying fully done)
// until sap_sync_status actually confirms SAP posted the Goods Receipt.
const resolveStatusBadge = (shipment) => {
  if (shipment.status === "approved" && shipment.sap_sync_status !== "posted" && shipment.sap_sync_status !== "partial") {
    // Sep 17 2026, Manual GRN feature - distinct badges for the two new
    // no-Playwright states so the pending/confirmed lists read correctly
    // without needing to open the shipment.
    if (shipment.sap_sync_status === "awaiting_manual_gr") {
      return { label: "Awaiting Manual GR in SAP", className: "bg-[#EFF4FF] text-[#3538CD] border border-[#C7D7FE] rounded-sm" };
    }
    if (shipment.sap_sync_status === "manual_mismatch") {
      return { label: "Qty Mismatch - Blocked", className: "bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA] rounded-sm" };
    }
    return shipment.sap_sync_status === "failed"
      ? { label: "SAP Sync Failed", className: "bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA] rounded-sm" }
      : { label: "Pending SAP Sync", className: "bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89] rounded-sm" };
  }
  if (shipment.status === "approved" && shipment.sap_sync_status === "partial") {
    return { label: "Partially Posted", className: "bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6] rounded-sm" };
  }
  return STATUS_BADGE[shipment.status];
};

// Sep 2 2026 (user's ask: step-by-step visibility + a reverse timer
// instead of a plain spinner) - matches the `phase` strings server.py
// forwards from sap_playwright_supplier_pgr_service.py's progress_cb.
// Sep 14 2026 fix (user's question: "is 'Searching for PO' relevant in
// the new method of GRN?") - it wasn't. The step name "searching" is
// still emitted as-is by sap_playwright_supplier_pgr_service.py (kept
// unchanged there so server.py's phase-string contract doesn't need to
// change), but since the Sep 12 2026 hybrid rewrite that step is a SOAP
// call creating the Inbound Delivery Notification directly - there is
// no more UI "search for the PO" at all (see that module's own
// docstring). Relabeled here to describe what's actually happening now.
const GRN_STEP_LABELS = {
  searching: "Creating Delivery Notification for PO",
  opening_receipt: "Opening Goods Receipt for PO",
  entering_quantities: "Entering quantities for PO",
  saving: "Saving to SAP for PO",
};
const GRN_PHASE_LABELS = {
  queued: "Queued...",
  logging_in: "Connecting to SAP...",
  retrying: "A step failed - retrying automatically...",
  moving_stock: "Moving stock into the warehouse...",
  creating_notifications: "Creating SAP Notification...",
  done: "Done",
};
function describeGrnPhase(phase) {
  if (!phase) return "Starting...";
  if (GRN_PHASE_LABELS[phase]) return GRN_PHASE_LABELS[phase];
  const [step, po, poCount] = phase.split(":");
  const label = GRN_STEP_LABELS[step] || step;
  return po ? `${label} ${po}${poCount ? ` (${poCount})` : ""}` : label;
}
// Sep 14 2026 fix (same investigation as GRN_STEP_LABELS above) - the
// Diagnostics modal, the failure screenshot dialog's title, and both
// "View exact SAP screen at time of failure (...)" links all rendered
// the raw internal `failed_step` string (e.g. "searching") straight to
// the user - same stale-wording problem, now fixed everywhere it's
// shown, not just the live progress bar.
const FAILED_STEP_LABELS = {
  searching: "creating/locating the SAP Delivery Notification",
  opening_receipt: "opening the Goods Receipt screen",
  entering_quantities: "entering item quantities",
  saving: "saving to SAP",
  unexpected_crash: "an unexpected error",
};
function describeFailedStep(step) {
  return FAILED_STEP_LABELS[step] || step || "unknown step";
}
// Rough empirical average per discrete step, purely for the countdown's
// display - the real remaining time is unknowable in advance (live SAP
// UI automation), so this clamps at "Almost there..." instead of ever
// going negative or over-promising.
const GRN_SECONDS_PER_STEP = 9;

// Sep 12 2026 (user's ask - the raw `JSON.stringify(sap_gr_result)` dump
// in the toast description was unreadable AND the toast disappears
// before anyone can read it anyway). Short, human sentence for the
// toast; the FULL per-PO detail always goes into the persistent
// Diagnostics modal instead (see openDiagnosticsIfFailed below).
function summarizeGrResult(grResult) {
  const perPo = grResult?.per_po || [];
  if (!perPo.length) return "No SAP posting was attempted.";
  const posted = perPo.filter((p) => p.status === "posted");
  const notPosted = perPo.filter((p) => p.status !== "posted");
  if (!notPosted.length) return `All ${posted.length} PO(s) posted to SAP.`;
  const details = notPosted.map((p) => `PO ${p.po_number}: ${p.error || "failed - see Diagnostics"}`).join(" | ");
  return `${posted.length} of ${perPo.length} PO(s) posted. ${details}`;
}

function summarizeMovementResult(movementResult) {
  const perItem = movementResult?.per_item || [];
  const failed = perItem.filter((i) => !i.ok);
  if (!failed.length) return "Stock movement is still pending.";
  return failed.map((i) => `${i.product_id || `item ${i.item_number}`}: ${i.error || "failed"}`).join(" | ");
}

// Sep 18 2026, user's explicit ask - the Diagnostics modal used to only
// ever look at the Goods Receipt result (sap_gr_result.per_po), so a PO
// that posted cleanly (step 1) but whose warehouse movement (step 2)
// then failed showed the same hardcoded "item(s) below were dropped...
// likely crashed immediately" text as a genuinely dropped/crashed PO -
// misleading for GRN S000001 (Site P3, IRON-SCR/13INTIEBELT: GR posted
// fine, only the warehouse move itself failed). Attaches this PO's own
// movement errors (matched by po_number, same shape _post_goods_
// movement_for_items already returns) so the modal can tell "clean
// success" apart from "GR fine, movement failed" apart from "items
// genuinely dropped".
function poDiagnostics(doc, poResult) {
  return {
    ...poResult,
    movement_issues: (doc?.sap_movement_result?.per_item || []).filter((m) => m.po_number === poResult.po_number && m.error),
  };
}

export default function GrnApprovalPage() {
  const { user, refresh: refreshAuth } = useAuth();
  const [code, setCode] = useState("");
  const [shipment, setShipment] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [pending, setPending] = useState([]);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [screenshotModal, setScreenshotModal] = useState(null);

  // Sep 17 2026, Manual GRN (No-Playwright) feature - user's explicit ask
  // ("keep the choice for users") to self-toggle between the existing
  // Playwright automation and manually posting the Goods Receipt in SAP.
  const [prefBusy, setPrefBusy] = useState(false);
  const toggleManualGrnPreference = async (checked) => {
    setPrefBusy(true);
    try {
      await axios.put(`${API}/admin/grn/my-preference`, { manual_grn_preference: checked });
      await refreshAuth();
      toast.success(checked ? "Manual GRN mode enabled - future approvals only create the SAP Notification, you post the Goods Receipt yourself in SAP" : "Auto (Playwright) GRN mode enabled");
    } catch (err) {
      toast.error("Could not save preference", { description: err?.response?.data?.detail || err.message });
    } finally {
      setPrefBusy(false);
    }
  };

  // Admin-only "Blocked Users" override panel (Sep 17 2026).
  const [blockedUsersOpen, setBlockedUsersOpen] = useState(false);
  const [blockedUsers, setBlockedUsers] = useState([]);
  const [overrideReasons, setOverrideReasons] = useState({});
  const [overrideBusyId, setOverrideBusyId] = useState(null);
  const isGrnAdmin = user && (user.role === "super_admin" || user.role === "admin");
  const loadBlockedUsers = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/grn/blocked-users`);
      setBlockedUsers(data.users || []);
    } catch (err) {
      toast.error("Could not load blocked users", { description: err?.response?.data?.detail || err.message });
    }
  };
  const openBlockedUsers = () => {
    setBlockedUsersOpen(true);
    loadBlockedUsers();
  };
  const overrideBlock = async (userId) => {
    const reason = (overrideReasons[userId] || "").trim();
    if (!reason) {
      toast.error("An override reason is required");
      return;
    }
    setOverrideBusyId(userId);
    try {
      await axios.post(`${API}/admin/grn/override-block/${userId}`, { reason });
      toast.success("User unblocked");
      loadBlockedUsers();
      refreshAuth();
    } catch (err) {
      toast.error("Override failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setOverrideBusyId(null);
    }
  };

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
  const [confirmedRetryBusy, setConfirmedRetryBusy] = useState(false);
  // Sep 12 2026, user's explicit ask: "diagnostics" popup - which SAP
  // screen the automation failed on, the SAP error itself, and every
  // milestone it successfully got through beforehand (see `events` on
  // each per_po entry, built up live by sap_playwright_supplier_pgr_
  // service.py's _post_one_po).
  const [diagnosticsModal, setDiagnosticsModal] = useState(null);
  // Sep 12 2026 (user's ask - "put them in diagnostics report, currently
  // toast disappears"): auto-open the modal on any failure/partial
  // success, instead of making the user hunt for a "View Diagnostics"
  // button under a toast that's already gone.
  const openDiagnosticsIfFailed = (grResult) => {
    const failing = grResult?.per_po?.find((p) => p.status !== "posted" && p.events?.length);
    if (failing) setDiagnosticsModal(failing);
  };

  // Sep 10 2026, user's explicit ask: don't let anyone hit Retry while the
  // background job might still legitimately be running - the whole flow
  // (3 attempts x Playwright login/navigate/save + 8s pauses) can
  // genuinely take a few minutes, so gate Retry behind a 5 min cooldown
  // from approval and show "In Process" until then.
  const RETRY_COOLDOWN_MIN = 5;
  const [nowTick, setNowTick] = useState(Date.now());
  useEffect(() => {
    const tick = setInterval(() => setNowTick(Date.now()), 15000);
    return () => clearInterval(tick);
  }, []);
  const minutesSince = (dateStr) => dateStr ? (nowTick - new Date(dateStr).getTime()) / 60000 : Infinity;
  const retryUnlockInMin = (dateStr) => Math.max(0, Math.ceil(RETRY_COOLDOWN_MIN - minutesSince(dateStr)));

  const retryConfirmedGoodsReceipt = async (doc) => {
    // Sep 12 2026 bug fix (user's explicit report: "retry does not
    // work... nothing shows diagnostics", "retry history shows
    // nowhere, still shows original date/time"): this used to fire the
    // retry job and just hope a single 3-second-later list reload
    // would pick up the result. A real SAP GRN attempt takes 1-2+
    // minutes, so that reload almost always ran while the job was
    // still in progress - and it only ever refreshed the closed LIST,
    // never the currently OPEN detail dialog (`confirmedDetail`), which
    // kept showing the pre-retry sap_gr_result/screenshot/events
    // forever, looking exactly like "retry did nothing". Now polls the
    // job the same way the main Approve/Retry flow already does
    // (pollGrnJob) and pushes the real, final result straight into the
    // open dialog + the list once it's actually done.
    setConfirmedRetryBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${doc._id}/retry-goods-receipt`);
      toast.success("Retry started - checking SAP now, this can take a couple of minutes");
      const result = await pollGrnJob(data.job_id);
      if (confirmedDetail?._id === doc._id) setConfirmedDetail(result);
      if (result.sap_sync_status === "posted") {
        toast.success("Goods Receipt posted to SAP");
      } else {
        toast.warning("Still not posted", { description: summarizeGrResult(result.sap_gr_result), duration: 8000 });
        openDiagnosticsIfFailed(result.sap_gr_result);
      }
      loadConfirmed();
    } catch (err) {
      toast.error("Retry failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setConfirmedRetryBusy(false);
    }
  };

  // Sep 13 2026, user's explicit ask ("align it and choose best for
  // users interest") - the top-level lookup detail panel already hid
  // "Retry" behind "Reset Retry" once sap_sync_status hits "failed"
  // (MAX_GR_RETRIES_BEFORE_FAILED reached, same unresolved SAP error
  // reproducing every time), but this Confirmed-detail dialog still
  // showed a plain Retry button in that exact same state - letting
  // staff keep hammering a known-broken SAP-side issue instead of
  // waiting on SAP Admin confirmation first. Mirrors resetRetry, but
  // targets confirmedDetail/loadConfirmed instead of shipment.
  const resetConfirmedRetry = async (doc) => {
    if (!window.confirm("Only do this after your SAP Admin has confirmed the underlying SAP error is fixed. Reset retry count for this shipment?")) return;
    setConfirmedRetryBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${doc._id}/reset-retry`);
      if (confirmedDetail?._id === doc._id) setConfirmedDetail(data);
      toast.success("Retry count reset - you can Retry the Goods Receipt again");
      loadConfirmed();
    } catch (err) {
      toast.error("Reset failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setConfirmedRetryBusy(false);
    }
  };

  // Sep 18 2026, user's explicit ask (GRN S000001, Site P3) - the
  // warehouse-move Retry button already existed for the Pending
  // Shipments modal (retryMovement above) but was never wired into this
  // Confirmed GRNs detail modal, so a GRN whose Goods Receipt posted
  // fine but whose warehouse move failed had NO way to retry it once it
  // showed up here - only the (unrelated) "Retry Goods Receipt" flow.
  const retryConfirmedMovement = async (doc) => {
    setConfirmedRetryBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${doc._id}/retry-movement`);
      if (confirmedDetail?._id === doc._id) setConfirmedDetail(data);
      if (data.sap_movement_status === "posted") {
        toast.success(`Stock moved to ${data.site_id}/${data.warehouse_id}`);
      } else {
        toast.warning("Movement still pending", { description: summarizeMovementResult(data.sap_movement_result), duration: 8000 });
      }
      loadConfirmed();
    } catch (err) {
      toast.error("Retry failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setConfirmedRetryBusy(false);
    }
  };

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
  const hasManuallyConfirmedInbound = (s) => (s.sap_gr_result?.per_po || []).some((p) => p.manually_confirmed && p.inbound_delivery_id);

  // Sep 11 2026, user's explicit ask: a few live GRNs actually posted in SAP
  // but our own Playwright automation never captured the real Inbound
  // Delivery ID (blank forever otherwise) - lets staff pull it straight
  // from SAP on demand, mirroring OpenPurchaseOrdersPage's "Refresh from
  // SAP" job-poll pattern (a single live query here takes 30-100s+).
  const [fetchingInboundFor, setFetchingInboundFor] = useState(null);
  const fetchInboundDelivery = async (s) => {
    setFetchingInboundFor(s._id);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${s._id}/fetch-inbound-delivery`);
      const jobId = data.job_id;
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const { data: poll } = await axios.get(`${API}/admin/grn/fetch-inbound-delivery/poll/${jobId}`);
        if (poll.status === "done") {
          const ids = inboundDeliveryIds(poll.result.shipment);
          toast.success(`Fetched from SAP: Inbound Delivery # ${ids.join(", ")}`);
          loadConfirmed();
          if (confirmedDetail?._id === s._id) setConfirmedDetail(poll.result.shipment);
          return;
        }
        if (poll.status === "failed") {
          toast.error("Could not fetch from SAP", { description: poll.error || "Unknown error" });
          return;
        }
      }
      toast.error("This is taking longer than expected - try again shortly");
    } catch (err) {
      toast.error("Could not start SAP fetch", { description: err?.response?.data?.detail || err.message });
    } finally {
      setFetchingInboundFor(null);
    }
  };

  // Sep 10 2026, user's explicit ask: tabbed Pending/Confirmed instead of
  // stacked tables, each with its own "PO number or Vendor code" filter.
  const [activeTab, setActiveTab] = useState("pending");
  const [pendingFilter, setPendingFilter] = useState("");
  const [confirmedFilter, setConfirmedFilter] = useState("");
  const matchesPoOrVendor = (s, term) => {
    if (!term.trim()) return true;
    const needle = term.trim().toLowerCase();
    const haystack = [s.vendor_code, s.company_name, ...s.items.map((it) => it.po_number)].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(needle);
  };
  const filteredPending = pending.filter((s) => matchesPoOrVendor(s, pendingFilter));
  const filteredConfirmed = confirmed.filter((s) => matchesPoOrVendor(s, confirmedFilter));

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
        // Sep 11 2026, user's explicit ask: default Warehouse should be the
        // site's RM (Raw Material) location, not QC - matched by warehouse_id
        // suffix "RM" for most sites (P1-RM, P2-RM, P4-RM...). P3 has no
        // "-RM" id at all (its RM location is zone-coded, "P3-Z1-01-A" named
        // "P3-RM-Zone-1-01-A") - falls back to matching "RM" in the name so
        // this rule generalizes to any site without a hardcoded site check.
        const rmById = whs.find((w) => (w.warehouse_id || "").split("-").pop() === "RM");
        const rmByName = whs.find((w) => /(^|[-\s])RM([-\s]|$)/i.test(w.warehouse_name || ""));
        const rm = rmById || rmByName;
        if (rm) setWarehouseId(rm.warehouse_id);
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
    if (!supplierDocNum.trim() || !billDate) {
      toast.error("Supplier Invoice Number and Bill Date are required before approving");
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
        toast.warning("Goods Receipt posted - warehouse movement still pending", { description: summarizeMovementResult(result.sap_movement_result), duration: 8000 });
      } else if (result.sap_sync_status === "awaiting_manual_gr") {
        toast.success("SAP Notification created - complete the GR in SAP, then click 'Re-check SAP'", { duration: 8000 });
      } else {
        toast.error("Approved internally - SAP posting failed, use Retry Goods Receipt below", { description: summarizeGrResult(result.sap_gr_result), duration: 8000 });
        openDiagnosticsIfFailed(result.sap_gr_result);
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
        toast.warning("Still pending", { description: summarizeGrResult(result.sap_gr_result), duration: 8000 });
        openDiagnosticsIfFailed(result.sap_gr_result);
      }
    } catch (err) {
      toast.error("Retry failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
      setJobProgress(null);
    }
  };

  const resetRetry = async () => {
    if (!window.confirm("Only do this after your SAP Admin has confirmed the underlying SAP error is fixed. Reset retry count for this shipment?")) return;
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/reset-retry`);
      setShipment(data);
      toast.success("Retry count reset - you can Retry the Goods Receipt again");
    } catch (err) {
      toast.error("Reset failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
    }
  };

  const verifySapStatus = async () => {
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/verify-sap-status`);
      const fixed = (data.gr_verification_result || []).filter((c) => c.corrected_to);
      setShipment(data);
      if (fixed.length > 0) {
        toast.success(`SAP disagreed with ${fixed.length} PO(s) shown as Posted - corrected to Failed, Retry is now available`);
      } else {
        toast.success("SAP confirms every PO on this shipment is genuinely posted");
      }
    } catch (err) {
      toast.error("Could not verify against SAP", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
    }
  };

  // Sep 17 2026, Manual GRN feature - "Re-check SAP" button, used once
  // staff have posted the actual Goods Receipt themselves in SAP.
  const recheckManualGr = async () => {
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/admin/grn/${shipment._id}/recheck-manual-gr`);
      setShipment(data);
      if (data.recheck_result === "matched") {
        toast.success("Quantities matched - Goods Receipt confirmed" + (data.sap_movement_status === "posted" ? ` + stock moved to ${data.site_id}/${data.warehouse_id}` : ""));
      } else if (data.recheck_result === "mismatch") {
        toast.error(`Quantity mismatch found on ${(data.manual_gr_mismatch_items || []).length} line(s) - you're blocked from approving new GRNs until this is resolved`, { duration: 10000 });
      }
      loadPending();
      loadConfirmed();
    } catch (err) {
      toast.error("Re-check failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBusy(false);
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
        toast.warning("Movement still pending", { description: summarizeMovementResult(data.sap_movement_result), duration: 8000 });
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
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
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
        <p className="text-sm text-[#475467]">Enter the shipment code from the delivery paperwork, physically match the goods and supplier invoice, then approve.</p>

        {/* Sep 17 2026, Manual GRN feature - self-service toggle between
            the Playwright-automated Goods Receipt and posting it manually
            in SAP, plus the admin "Blocked Users" override entry point.
            Sep 16 2026, user's explicit ask - Manual GRN is now the ONLY
            path for Supplier GRN; toggle kept visible but disabled (not
            removed) in case Playwright is ever wanted back. */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-3 flex flex-wrap items-center justify-between gap-3 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)]">
          <div className="flex items-center gap-3">
            <Switch
              checked={true}
              disabled={true}
              data-testid="grn-manual-mode-toggle"
            />
            <div className="leading-tight">
              <div className="text-sm font-semibold text-[#1D2939]">Manual GRN mode</div>
              <div className="text-xs text-[#667085]">Approvals only create the SAP Notification - you post the Goods Receipt yourself in SAP</div>
            </div>
          </div>
          {isGrnAdmin && (
            <Button size="sm" variant="outline" onClick={openBlockedUsers} className="h-8 rounded-sm text-xs" data-testid="grn-open-blocked-users-button">
              <LockKey size={14} className="mr-1" /> Blocked Users
            </Button>
          )}
        </div>

        {user?.grn_blocked_shipment && (
          <div className="text-sm px-3 py-2.5 rounded-sm border border-[#E02424]/30 bg-[#E02424]/5 text-[#B91C1C] flex items-center gap-2" data-testid="grn-user-blocked-banner">
            <WarningCircle size={16} weight="fill" />
            <span>You are blocked from approving new GRNs - shipment <strong>{user.grn_blocked_shipment}</strong> has an unresolved quantity mismatch. {user.grn_blocked_reason}</span>
          </div>
        )}

        <div className="bg-white border border-[#D0D5DD] rounded-sm p-3 flex items-center gap-3 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)]">
          <Input
            placeholder="e.g. S000003"
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && lookup()}
            className="h-8 font-data uppercase w-64 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
            maxLength={10}
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
                {shipment.created_on_behalf_by && (
                  <div className="text-xs text-[#B54708] mt-0.5" data-testid="grn-created-on-behalf-flag">Created on behalf by {shipment.created_on_behalf_by}</div>
                )}
              </div>
              <Badge className={resolveStatusBadge(shipment).className} data-testid="grn-status-badge">{resolveStatusBadge(shipment).label}</Badge>
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
                  {shipment.sap_gr_result?.per_po && (
                    <th className="border border-[#D0D5DD] p-1.5 text-left">SAP Inbound Delivery #</th>
                  )}
                  {shipment.sap_gr_result?.per_po && (
                    <th className="border border-[#D0D5DD] p-1.5 text-left">PO Status</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {shipment.items.map((it, i) => {
                  const key = `${it.po_number}::${it.item_number}`;
                  const effectiveQty = Number(actualQtys[key] ?? it.actual_qty ?? it.ship_qty ?? 0);
                  const lineValue = it.unit_price != null ? effectiveQty * it.unit_price : null;
                  // Sep 14 2026 fix (user's real report: "diagnostics not
                  // showing full details" - the single "View Diagnostics"
                  // button only ever surfaced whichever ONE PO had an
                  // issue, with zero way to check any OTHER PO's own
                  // status on the same multi-PO shipment) - resolve each
                  // line's own PO's result directly, per-row.
                  const poResult = shipment.sap_gr_result?.per_po?.find((p) => p.po_number === it.po_number);
                  return (
                    <tr key={i} className="bg-white odd:bg-[#F9FAFB]">
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">
                        {it.po_number}
                        {it.sap_po_number && (
                          <div className="text-[10px] text-[#475467] font-sans" data-testid={`grn-item-printed-po-number-${i}`}>Printed PO #: {it.sap_po_number}</div>
                        )}
                      </td>
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
                      {shipment.sap_gr_result?.per_po && (
                        <td className="border border-[#D0D5DD] px-2 py-1 font-data" data-testid={`grn-item-inbound-delivery-id-${i}`}>
                          {poResult?.inbound_delivery_id || "-"}
                        </td>
                      )}
                      {shipment.sap_gr_result?.per_po && (
                        <td className="border border-[#D0D5DD] px-2 py-1 whitespace-nowrap" data-testid={`grn-item-po-status-${i}`}>
                          {poResult?.status === "notification_created" ? (
                            <span className="text-[11px] font-semibold text-[#3538CD]">Notification Created</span>
                          ) : poResult ? (
                            <button
                              type="button"
                              className={`text-[11px] font-semibold hover:underline ${poResult.status !== "posted" ? (poResult.status === "skipped" ? "text-[#B54708]" : "text-[#B42318]") : poDiagnostics(shipment, poResult).movement_issues.length > 0 ? "text-[#B54708]" : "text-[#027A48]"}`}
                              onClick={() => setDiagnosticsModal(poDiagnostics(shipment, poResult))}
                              data-testid={`grn-item-diagnostics-link-${i}`}
                            >
                              {poResult.status === "posted" ? (poDiagnostics(shipment, poResult).movement_issues.length > 0 ? "Posted (movement failed)" : "Posted") : poResult.status === "skipped" ? "Skipped" : "Failed"} - Diagnostics
                            </button>
                          ) : (
                            <span className="text-[11px] text-[#98A2B3]">Not attempted yet</span>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
                {isActionable && (
                  <tr><td colSpan={shipment.sap_gr_result?.per_po ? 10 : 8} className="border border-[#D0D5DD] px-2 py-1 text-xs text-[#475467]">Actual Qty defaults to Ship Qty - adjust only if the physical count differs. PO Price/Line Value are for reference only, from SAP's last cached rate.</td></tr>
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
                    {jobProgress.kind === "manual_grn_notification" ? `Elapsed: ${jobElapsed}s` : (() => {
                      const remaining = GRN_SECONDS_PER_STEP * jobProgress.progress_total - jobElapsed;
                      return remaining > 0 ? `~${remaining}s left` : "Almost there...";
                    })()}
                  </span>
                </div>
                <Progress
                  value={
                    jobProgress.kind === "manual_grn_notification"
                      ? Math.min(92, jobElapsed * 15)
                      : Math.min(100, Math.round((jobProgress.progress_current / Math.max(1, jobProgress.progress_total)) * 100))
                  }
                  className="h-1.5"
                  data-testid="grn-job-progress-bar"
                />
                {jobProgress.kind !== "manual_grn_notification" && (
                  <div className="text-[11px] text-[#98A2B3] font-data">Elapsed: {jobElapsed}s</div>
                )}
              </div>
            )}

            {isActionable && (
              <div className="mt-5 border-t border-[#D0D5DD] pt-4 space-y-3">
                <div className="grid sm:grid-cols-4 gap-3">
                  <div>
                    <Label className="text-xs text-[#475467]">Supplier Invoice Number <span className="text-[#B42318]">*</span></Label>
                    <Input
                      placeholder="e.g. INV-4521"
                      value={supplierDocNum}
                      onChange={(e) => setSupplierDocNum(e.target.value.slice(0, 20))}
                      maxLength={20}
                      className="rounded-sm border-[#D0D5DD] mt-1"
                      data-testid="grn-supplier-doc-num-input"
                    />
                  </div>
                  <div>
                    <Label className="text-xs text-[#475467]">Bill Date <span className="text-[#B42318]">*</span></Label>
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
                  <Button
                    onClick={approve}
                    disabled={busy || siteAccessBlocked || !!user?.grn_blocked_shipment || !supplierDocNum.trim() || !billDate}
                    title={user?.grn_blocked_shipment ? "Blocked - resolve your open GRN quantity mismatch first" : (!supplierDocNum.trim() || !billDate) ? "Enter the Supplier Invoice Number and Bill Date first" : undefined}
                    className="h-8 rounded-sm bg-[#027A48] hover:bg-[#02623A] text-white px-4 text-[13px] font-bold transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    data-testid="grn-approve-button"
                  >
                    <CheckCircle size={14} className="mr-1" /> {busy ? "Creating..." : "Create SAP Notification"}
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
                     style={shipment.sap_sync_status === "posted" || shipment.sap_sync_status === "partial" ? { color: "#0B7A56", background: "rgba(16,185,129,0.1)", borderColor: "rgba(16,185,129,0.3)" } : shipment.sap_sync_status === "failed" || shipment.sap_sync_status === "manual_mismatch" ? { color: "#B42318", background: "rgba(180,35,24,0.08)", borderColor: "rgba(180,35,24,0.3)" } : shipment.sap_sync_status === "awaiting_manual_gr" ? { color: "#3538CD", background: "rgba(53,56,205,0.08)", borderColor: "rgba(53,56,205,0.3)" } : { color: "#B45309", background: "rgba(227,160,8,0.1)", borderColor: "rgba(227,160,8,0.3)" }}>
                  <span className="flex items-center gap-2">
                    {shipment.sap_sync_status === "posted" || shipment.sap_sync_status === "partial" ? <CheckCircle size={16} /> : shipment.sap_sync_status === "manual_mismatch" ? <WarningCircle size={16} weight="fill" /> : <PlugsConnected size={16} />}
                    {shipment.sap_sync_status === "posted"
                      ? "Goods Receipt posted to SAP"
                      : shipment.sap_sync_status === "partial"
                      ? (
                        <span className="flex items-center gap-2">
                          <Badge className="bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6]" data-testid="grn-sap-partial-badge">Partially Posted</Badge>
                          {`${(shipment.sap_gr_result?.per_po || []).filter((p) => p.status === "posted").length} of ${(shipment.sap_gr_result?.per_po || []).length} PO(s) posted - the rest were permanently skipped (see per-PO diagnostics below), nothing left to retry`}
                        </span>
                      )
                      : shipment.sap_sync_status === "awaiting_manual_gr"
                      ? (
                        <span className="flex items-center gap-2">
                          <Badge className="bg-[#EFF4FF] text-[#3538CD] border border-[#C7D7FE]" data-testid="grn-sap-awaiting-manual-badge">Awaiting Manual GR in SAP</Badge>
                          Complete the GR in SAP, then click Re-check SAP
                        </span>
                      )
                      : shipment.sap_sync_status === "manual_mismatch"
                      ? (
                        <span className="flex items-center gap-2">
                          <Badge className="bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]" data-testid="grn-sap-manual-mismatch-badge">Qty Mismatch - Approver Blocked</Badge>
                          {`SAP's confirmed quantity does not match ${(shipment.manual_gr_mismatch_items || []).length} line(s) - correct it in SAP and Re-check`}
                        </span>
                      )
                      : shipment.sap_sync_status === "skipped"
                      ? (shipment.sap_gr_result?.per_po?.[0]?.error || "PO not found in SAP - check it's released")
                      : shipment.sap_sync_status === "failed"
                      ? (
                        <span className="flex items-center gap-2">
                          <Badge className="bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]" data-testid="grn-sap-failed-badge">SAP Sync Failed</Badge>
                          {shipment.sap_gr_result?.per_po?.find((p) => p.error)?.error || "Repeated attempts failed - needs SAP Admin attention"}
                        </span>
                      )
                      : (
                        <span className="flex items-center gap-2">
                          <Badge className="bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89]" data-testid="grn-sap-in-process-badge">In Process</Badge>
                          {shipment.sap_gr_result?.per_po?.find((p) => p.error)?.error || "Posting to SAP - this can take a few minutes"}
                        </span>
                      )}
                  </span>
                  {shipment.sap_sync_status === "partial" ? null : shipment.sap_sync_status === "awaiting_manual_gr" || shipment.sap_sync_status === "manual_mismatch" ? (
                    <Button size="sm" variant="outline" onClick={recheckManualGr} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-recheck-manual-gr-button">
                      <ArrowsClockwise size={12} className="mr-1" /> Re-check SAP
                    </Button>
                  ) : shipment.sap_sync_status === "skipped" ? (
                    <Button size="sm" variant="outline" onClick={retryGoodsReceipt} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-retry-goods-receipt-button">
                      <ArrowsClockwise size={12} className="mr-1" /> Retry
                    </Button>
                  ) : shipment.sap_sync_status === "failed" ? (
                    shipment.grn_mode === "manual" ? (
                      <Button size="sm" variant="outline" onClick={retryGoodsReceipt} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-retry-goods-receipt-button">
                        <ArrowsClockwise size={12} className="mr-1" /> Retry
                      </Button>
                    ) : (
                      <Button size="sm" variant="outline" onClick={resetRetry} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-reset-retry-button" title="Only use once your SAP Admin confirms the underlying SAP error is fixed">
                        <ArrowsClockwise size={12} className="mr-1" /> Reset Retry
                      </Button>
                    )
                  ) : shipment.sap_sync_status !== "posted" && (
                    retryUnlockInMin(shipment.approved_at) > 0 ? (
                      <span className="text-xs text-[#98A2B3] shrink-0" data-testid="grn-retry-cooldown">Retry available in {retryUnlockInMin(shipment.approved_at)}m</span>
                    ) : (
                      <Button size="sm" variant="outline" onClick={retryGoodsReceipt} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-retry-goods-receipt-button">
                        <ArrowsClockwise size={12} className="mr-1" /> Retry
                      </Button>
                    )
                  )}
                  {shipment.sap_sync_status === "posted" && (
                    <Button size="sm" variant="outline" onClick={verifySapStatus} disabled={busy} className="rounded-sm h-7 text-xs" data-testid="grn-verify-sap-status-button" title="Double-check with SAP that every PO here was genuinely posted">
                      <ArrowsClockwise size={12} className="mr-1" /> Verify with SAP
                    </Button>
                  )}
                </div>
                {shipment.sap_sync_status === "manual_mismatch" && (shipment.manual_gr_mismatch_items || []).length > 0 && (
                  <table className="border-collapse w-full text-xs border border-[#FECDCA] rounded-sm overflow-hidden" data-testid="grn-manual-mismatch-table">
                    <thead className="bg-[#FEF3F2] text-[#B42318] font-bold uppercase tracking-wide">
                      <tr>
                        <th className="border border-[#FECDCA] p-1.5 text-left">PO Number</th>
                        <th className="border border-[#FECDCA] p-1.5 text-left">Item</th>
                        <th className="border border-[#FECDCA] p-1.5 text-left">Product</th>
                        <th className="border border-[#FECDCA] p-1.5 text-right">Shipped/Confirmed Qty</th>
                        <th className="border border-[#FECDCA] p-1.5 text-right">SAP Confirmed Qty</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shipment.manual_gr_mismatch_items.map((m, i) => (
                        <tr key={i} className="bg-white odd:bg-[#FEF3F2]/40" data-testid={`grn-manual-mismatch-row-${i}`}>
                          <td className="border border-[#FECDCA] px-2 py-1 font-data">{m.po_number}</td>
                          <td className="border border-[#FECDCA] px-2 py-1 font-data">{m.item_number}</td>
                          <td className="border border-[#FECDCA] px-2 py-1">{m.product_id}</td>
                          <td className="border border-[#FECDCA] px-2 py-1 text-right font-data">{m.shipped_qty ?? "\u2014"}</td>
                          <td className="border border-[#FECDCA] px-2 py-1 text-right font-data">{m.sap_confirmed_qty ?? m.reason ?? "\u2014"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {(shipment.sap_sync_status !== "posted" && shipment.sap_sync_status !== "partial") && shipment.sap_gr_result?.per_po?.some((p) => p.screenshot_path) && (
                  <div className="px-3">
                    <button
                      type="button"
                      className="text-xs text-[#7A1E1E] font-bold hover:underline"
                      data-testid="grn-view-failure-screenshot-button"
                      onClick={() => {
                        const failed = shipment.sap_gr_result.per_po.find((p) => p.screenshot_path);
                        setScreenshotModal(failed);
                      }}
                    >
                      View exact SAP screen at time of failure ({describeFailedStep(shipment.sap_gr_result.per_po.find((p) => p.screenshot_path)?.failed_step)})
                    </button>
                  </div>
                )}
                {shipment.sap_gr_result?.per_po?.some((p) => (p.status !== "posted" && p.status !== "notification_created" && p.events?.length) || p.skipped_items?.length) && (
                  <div className="px-3">
                    <button
                      type="button"
                      className="text-xs text-[#475467] font-bold hover:underline"
                      data-testid="grn-view-diagnostics-button"
                      onClick={() => setDiagnosticsModal(poDiagnostics(shipment, shipment.sap_gr_result.per_po.find((p) => (p.status !== "posted" && p.status !== "notification_created" && p.events?.length) || p.skipped_items?.length)))}
                    >
                      View Diagnostics
                    </button>
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
                {/* Sep 12 2026, user's explicit ask ("after success grn
                    why an error occurred") - the badge above only ever
                    said "pending", never WHY. The real SAP error per
                    item was already being captured (sap_movement_result.
                    per_item[].error) but only ever surfaced in a toast
                    at the moment Retry was clicked - invisible on a
                    plain page load/lookup like this screenshot. */}
                {shipment.sap_movement_status !== "posted" && shipment.sap_movement_result?.per_item?.some((p) => p.error) && (
                  <div className="text-xs text-[#B54708] bg-[#FFFAEB] border border-[#FEDF89] rounded-sm px-3 py-2 space-y-1" data-testid="grn-movement-error-detail">
                    {shipment.sap_movement_result.per_item.filter((p) => p.error).map((p, i) => (
                      <div key={i} data-testid={`grn-movement-error-${i}`}><strong>{p.product_id}:</strong> {p.error}</div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {shipment.status === "rejected" && shipment.rejection_reason && (
              <div className="mt-4 text-sm text-[#B91C1C]" data-testid="grn-rejection-reason">Reason: {shipment.rejection_reason}</div>
            )}
          </div>
        )}

        <div className="flex gap-1 bg-white border border-[#D0D5DD] rounded-sm p-1 w-fit mt-6" data-testid="grn-tab-strip">
          <button type="button" onClick={() => setActiveTab("pending")} className={`px-4 py-1.5 text-xs font-bold rounded-sm transition-colors ${activeTab === "pending" ? "bg-[#004B87] text-white" : "text-[#344054] hover:bg-[#F2F4F7]"}`} data-testid="grn-tab-pending">
            Pending Shipments
          </button>
          <button type="button" onClick={() => setActiveTab("confirmed")} className={`px-4 py-1.5 text-xs font-bold rounded-sm transition-colors ${activeTab === "confirmed" ? "bg-[#004B87] text-white" : "text-[#344054] hover:bg-[#F2F4F7]"}`} data-testid="grn-tab-confirmed">
            Confirmed GRNs
          </button>
        </div>

        {activeTab === "pending" && (
          <>
            <div className="flex items-center gap-3 mt-3">
              <Input
                placeholder="Search PO Number or Vendor Code..."
                value={pendingFilter}
                onChange={(e) => setPendingFilter(e.target.value)}
                className="h-8 w-72 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                data-testid="grn-pending-filter-input"
              />
            </div>
            <div className="mt-3 bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto">
              <table className="w-full text-[13px] border-collapse">
                <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                  <tr>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Code</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Vendor</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">PO Numbers</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Printed PO #</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredPending.length === 0 && (
                    <tr><td colSpan={5} className="border border-[#D0D5DD] px-3 py-6 text-center text-[#475467]" data-testid="grn-pending-empty">{pending.length === 0 ? "No shipments awaiting GRN approval." : "No shipments match your search."}</td></tr>
                  )}
                  {filteredPending.map((s) => (
                    <tr key={s._id} className="cursor-pointer bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" onClick={() => lookup(s._id)} data-testid={`grn-pending-row-${s._id}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data font-bold text-[#004B87]">{s._id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1">{s.company_name}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data" data-testid={`grn-pending-printed-po-${s._id}`}>{[...new Set(s.items.map((it) => it.sap_po_number).filter(Boolean))].join(", ") || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{new Date(s.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        {activeTab === "confirmed" && (
          <>
            <p className="text-xs text-[#475467] mt-3">Click a row for the full receipt trail - supplier bill number, SAP inbound delivery #, and item-level qty/unit/warehouse.</p>
            <div className="flex items-center gap-3 mt-2">
              <Input
                placeholder="Search PO Number or Vendor Code..."
                value={confirmedFilter}
                onChange={(e) => setConfirmedFilter(e.target.value)}
                className="h-8 w-72 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                data-testid="grn-confirmed-filter-input"
              />
            </div>
            <div className="mt-3 bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto">
              <table className="w-full text-[13px] border-collapse">
                <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                  <tr>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Code</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Vendor</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">PO Numbers</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Printed PO #</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Supplier Invoice No</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">SAP Inbound Delivery #</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">SAP Reference</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">SAP Status</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Approved</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredConfirmed.length === 0 && (
                    <tr><td colSpan={9} className="border border-[#D0D5DD] px-3 py-6 text-center text-[#475467]" data-testid="grn-confirmed-empty">{confirmed.length === 0 ? "No confirmed GRNs yet." : "No confirmed GRNs match your search."}</td></tr>
                  )}
                  {filteredConfirmed.map((s) => (
                    <tr key={s._id} className="cursor-pointer bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" onClick={() => setConfirmedDetail(s)} data-testid={`grn-confirmed-row-${s._id}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data font-bold text-[#004B87]">{s._id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1">{s.company_name}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data" data-testid={`grn-confirmed-printed-po-${s._id}`}>{[...new Set(s.items.map((it) => it.sap_po_number).filter(Boolean))].join(", ") || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">{s.supplier_doc_num || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data" data-testid={`grn-confirmed-inbound-id-${s._id}`}>
                        {inboundDeliveryIds(s).length > 0 ? (
                          <span className="inline-flex items-center gap-1.5">
                            {inboundDeliveryIds(s).join(", ")}
                            {hasManuallyConfirmedInbound(s) && (
                              <Badge className="bg-[#EFF4FF] text-[#3538CD] border border-[#C7D7FE] text-[10px]" data-testid={`grn-confirmed-manual-tag-${s._id}`}>Manually confirmed from SAP</Badge>
                            )}
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-2">
                            <span>{"\u2014"}</span>
                            <button
                              type="button"
                              className="text-[11px] text-[#004B87] font-bold hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
                              disabled={fetchingInboundFor === s._id}
                              data-testid={`grn-fetch-inbound-delivery-button-${s._id}`}
                              onClick={(e) => { e.stopPropagation(); fetchInboundDelivery(s); }}
                            >
                              {fetchingInboundFor === s._id ? "Fetching from SAP..." : "Fetch from SAP"}
                            </button>
                          </span>
                        )}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data text-[#475467]" data-testid={`grn-confirmed-sap-reference-${s._id}`}>
                        {Object.keys(s.manual_gr_notification_ids || {}).length > 0
                          ? [...new Set(Object.values(s.manual_gr_notification_ids))].join(", ")
                          : "\u2014"}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1" data-testid={`grn-confirmed-sap-status-${s._id}`}>
                        {s.sap_sync_status === "posted" ? (
                          <Badge className="bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6]">Posted</Badge>
                        ) : s.sap_sync_status === "failed" ? (
                          <Badge className="bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]">Sync Failed</Badge>
                        ) : s.sap_sync_status === "awaiting_manual_gr" ? (
                          <Badge className="bg-[#EFF4FF] text-[#3538CD] border border-[#C7D7FE]">Awaiting Manual GR</Badge>
                        ) : s.sap_sync_status === "manual_mismatch" ? (
                          <Badge className="bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]">Qty Mismatch</Badge>
                        ) : (
                          <Badge className="bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89]">In Process</Badge>
                        )}
                        {s.sap_movement_result?.per_item?.length > 0 && (
                          <div className="mt-1" data-testid={`grn-confirmed-movement-${s._id}`}>
                            <MovementPopover items={s.sap_movement_result.per_item} warehouseLabel={s.warehouse_id} docCode={s._id}>
                              <span className={`text-[11px] ${s.sap_movement_status === "posted" ? "text-[#027A48]" : "text-[#B54708]"}`}>
                                {s.sap_movement_status === "posted" ? `Moved to ${s.warehouse_id}` : "Warehouse move pending"}
                              </span>
                            </MovementPopover>
                          </div>
                        )}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.approved_at ? new Date(s.approved_at).toLocaleString() : "\u2014"}</td>
                    </tr>
                  ))}
                </tbody>

              </table>
            </div>
          </>
        )}
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

      <Dialog open={blockedUsersOpen} onOpenChange={setBlockedUsersOpen}>
        <DialogContent className="rounded-sm max-w-xl" data-testid="grn-blocked-users-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Blocked GRN Users</DialogTitle>
            <DialogDescription>Users blocked from approving new GRNs due to an unresolved Manual GRN quantity mismatch. Overriding requires a reason and is logged for audit.</DialogDescription>
          </DialogHeader>
          {blockedUsers.length === 0 ? (
            <div className="text-sm text-[#667085] py-4" data-testid="grn-blocked-users-empty">No users are currently blocked.</div>
          ) : (
            <div className="space-y-3 max-h-[60vh] overflow-auto">
              {blockedUsers.map((u) => (
                <div key={u._id} className="border border-[#D0D5DD] rounded-sm p-3 space-y-2" data-testid={`grn-blocked-user-row-${u._id}`}>
                  <div className="text-sm font-semibold text-[#1D2939]">{u.name || u.email}</div>
                  <div className="text-xs text-[#B42318]">Shipment {u.grn_blocked_shipment}: {u.grn_blocked_reason}</div>
                  <Textarea
                    placeholder="Override reason (required, logged for audit)"
                    value={overrideReasons[u._id] || ""}
                    onChange={(e) => setOverrideReasons((prev) => ({ ...prev, [u._id]: e.target.value }))}
                    className="rounded-sm border-[#D0D5DD] text-sm"
                    data-testid={`grn-override-reason-input-${u._id}`}
                  />
                  <Button
                    size="sm"
                    onClick={() => overrideBlock(u._id)}
                    disabled={overrideBusyId === u._id}
                    className="rounded-sm bg-[#B54708] hover:bg-[#93370D] text-white text-xs"
                    data-testid={`grn-override-block-button-${u._id}`}
                  >
                    {overrideBusyId === u._id ? "Overriding..." : "Override & Unblock User"}
                  </Button>
                </div>
              ))}
            </div>
          )}
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
                {[...new Set(confirmedDetail.items.map((it) => it.sap_po_number).filter(Boolean))].length > 0 && (
                  <div><span className="text-[#475467]">Printed PO #(s):</span> <span className="font-data font-semibold" data-testid="grn-confirmed-detail-printed-po-number">{[...new Set(confirmedDetail.items.map((it) => it.sap_po_number).filter(Boolean))].join(", ")}</span></div>
                )}
                <div><span className="text-[#475467]">Supplier Invoice No:</span> <span className="font-data font-semibold">{confirmedDetail.supplier_doc_num || "\u2014"}</span></div>
                <div><span className="text-[#475467]">Bill Date:</span> <span className="font-data font-semibold">{confirmedDetail.bill_date || "\u2014"}</span></div>
                {confirmedDetail.sap_gr_result?.sap_username && (
                  <div><span className="text-[#475467]">SAP User Used:</span> <span className="font-data font-semibold" data-testid="grn-confirmed-detail-sap-username">{confirmedDetail.sap_gr_result.sap_username}</span></div>
                )}
                <div><span className="text-[#475467]">SAP Inbound Delivery #:</span>{" "}
                  {confirmedDetail.sap_sync_status === "posted" ? (
                    <span className="font-data font-semibold" data-testid="grn-confirmed-detail-inbound-id">
                      {inboundDeliveryIds(confirmedDetail).join(", ") || "\u2014"}
                      {hasManuallyConfirmedInbound(confirmedDetail) && (
                        <Badge className="bg-[#EFF4FF] text-[#3538CD] border border-[#C7D7FE] text-[10px] ml-1.5" data-testid="grn-confirmed-detail-manual-tag">Manually confirmed from SAP</Badge>
                      )}
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5">
                      {confirmedDetail.sap_sync_status === "failed" ? (
                        <Badge className="bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]" data-testid="grn-confirmed-detail-failed-badge">Sync Failed</Badge>
                      ) : (
                        <Badge className="bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89]" data-testid="grn-confirmed-detail-in-process-badge">In Process</Badge>
                      )}
                      {confirmedRetryBusy ? (
                        <span className="text-xs text-[#B54708] font-semibold" data-testid="grn-confirmed-detail-retrying-label">Retrying - checking SAP now...</span>
                      ) : confirmedDetail.sap_sync_status === "failed" ? (
                        <Button size="sm" variant="outline" className="h-6 px-2 text-[11px]" disabled={confirmedRetryBusy} onClick={() => resetConfirmedRetry(confirmedDetail)} title="Only use once your SAP Admin confirms the underlying SAP error is fixed" data-testid="grn-confirmed-detail-reset-retry-button">
                          <ArrowsClockwise size={11} className="mr-1" /> Reset Retry
                        </Button>
                      ) : retryUnlockInMin(confirmedDetail.approved_at) > 0 ? (
                        <span className="text-xs text-[#98A2B3]" data-testid="grn-confirmed-detail-retry-cooldown">Retry available in {retryUnlockInMin(confirmedDetail.approved_at)}m</span>
                      ) : (
                        <Button size="sm" variant="outline" className="h-6 px-2 text-[11px]" disabled={confirmedRetryBusy} onClick={() => retryConfirmedGoodsReceipt(confirmedDetail)} data-testid="grn-confirmed-detail-retry-button">
                          <ArrowsClockwise size={11} className="mr-1" /> Retry
                        </Button>
                      )}
                    </span>
                  )}
                </div>
                <div><span className="text-[#475467]">Site:</span> <span className="font-data font-semibold">{confirmedDetail.site_id}</span></div>
                <div><span className="text-[#475467]">Approved By:</span> <span className="font-semibold">{confirmedDetail.approved_by} · {confirmedDetail.approved_at ? new Date(confirmedDetail.approved_at).toLocaleString() : "\u2014"}</span></div>
              </div>
              {confirmedDetail.sap_movement_status && confirmedDetail.sap_movement_status !== "not_applicable" && (
                <div className="text-sm px-3 py-2 rounded-sm flex items-center justify-between gap-2 border" data-testid="grn-confirmed-detail-sap-movement-status"
                     style={confirmedDetail.sap_movement_status === "posted" ? { color: "#0B7A56", background: "rgba(16,185,129,0.1)", borderColor: "rgba(16,185,129,0.3)" } : { color: "#B45309", background: "rgba(227,160,8,0.1)", borderColor: "rgba(227,160,8,0.3)" }}>
                  <span className="flex items-center gap-2">
                    {confirmedDetail.sap_movement_status === "posted" ? <CheckCircle size={16} /> : <PlugsConnected size={16} />}
                    {confirmedDetail.sap_movement_status === "posted"
                      ? `Stock moved to ${confirmedDetail.site_id}/${confirmedDetail.warehouse_id}`
                      : `Warehouse movement pending${confirmedDetail.warehouse_id ? ` (target ${confirmedDetail.site_id}/${confirmedDetail.warehouse_id})` : ""}`}
                  </span>
                  {confirmedDetail.sap_movement_status !== "posted" && (
                    confirmedRetryBusy ? (
                      <span className="text-xs text-[#B54708] font-semibold" data-testid="grn-confirmed-detail-movement-retrying-label">Retrying...</span>
                    ) : (
                      <Button size="sm" variant="outline" onClick={() => retryConfirmedMovement(confirmedDetail)} disabled={confirmedRetryBusy} className="rounded-sm h-7 text-xs" data-testid="grn-confirmed-detail-retry-movement-button">
                        <ArrowsClockwise size={12} className="mr-1" /> Retry
                      </Button>
                    )
                  )}
                </div>
              )}
              {confirmedDetail.sap_movement_status && confirmedDetail.sap_movement_status !== "posted" && confirmedDetail.sap_movement_result?.per_item?.some((p) => p.error) && (
                <div className="text-xs text-[#B54708] bg-[#FFFAEB] border border-[#FEDF89] rounded-sm px-3 py-2 space-y-1" data-testid="grn-confirmed-detail-movement-error-detail">
                  {confirmedDetail.sap_movement_result.per_item.filter((p) => p.error).map((p, i) => (
                    <div key={i} data-testid={`grn-confirmed-detail-movement-error-${i}`}><strong>{p.product_id}:</strong> {p.error}</div>
                  ))}
                </div>
              )}
              {confirmedDetail.sap_sync_status !== "posted" && confirmedDetail.sap_gr_result?.per_po?.some((p) => p.screenshot_path) && (
                <div>
                  <button
                    type="button"
                    className="text-xs text-[#7A1E1E] font-bold hover:underline"
                    data-testid="grn-confirmed-detail-view-failure-screenshot-button"
                    onClick={() => {
                      const failed = confirmedDetail.sap_gr_result.per_po.find((p) => p.screenshot_path);
                      setScreenshotModal(failed);
                    }}
                  >
                    View exact SAP screen at time of failure ({describeFailedStep(confirmedDetail.sap_gr_result.per_po.find((p) => p.screenshot_path)?.failed_step)})
                  </button>
                </div>
              )}
              {confirmedDetail.sap_gr_result?.per_po?.some((p) => (p.status !== "posted" && p.events?.length) || p.skipped_items?.length) && (
                <div>
                  <button
                    type="button"
                    className="text-xs text-[#475467] font-bold hover:underline"
                    data-testid="grn-confirmed-detail-view-diagnostics-button"
                    onClick={() => setDiagnosticsModal(poDiagnostics(confirmedDetail, confirmedDetail.sap_gr_result.per_po.find((p) => (p.status !== "posted" && p.events?.length) || p.skipped_items?.length)))}
                  >
                    View Diagnostics
                  </button>
                </div>
              )}
              <table className="border-collapse w-full text-[13px] mt-2 border border-[#D0D5DD] rounded-sm overflow-hidden">
                <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                  <tr>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">PO Number</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Item Code</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Description</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-right">Qty</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Unit</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-left">Warehouse</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-right">PO Price</th>
                    <th className="border border-[#D0D5DD] p-1.5 text-right">Line Value</th>
                    {confirmedDetail.sap_gr_result?.per_po && (
                      <th className="border border-[#D0D5DD] p-1.5 text-left">PO Status</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {confirmedDetail.items.map((it, i) => {
                    const qty = it.actual_qty ?? it.ship_qty;
                    const lineValue = it.unit_price != null ? qty * it.unit_price : null;
                    const poResult = confirmedDetail.sap_gr_result?.per_po?.find((p) => p.po_number === it.po_number);
                    return (
                      <tr key={i} className="bg-white odd:bg-[#F9FAFB]" data-testid={`grn-confirmed-detail-item-${i}`}>
                        <td className="border border-[#D0D5DD] px-2 py-1 font-data" data-testid={`grn-confirmed-detail-po-number-${i}`}>{it.po_number}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 font-data font-semibold">{it.product_id || "\u2014"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1">{it.description}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data">{qty}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 font-data">{it.unit_of_measure}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 font-data">{confirmedDetail.site_id}/{confirmedDetail.warehouse_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data text-[#475467]" data-testid={`grn-confirmed-detail-po-price-${i}`}>
                          {it.unit_price != null ? `${it.currency || ""} ${it.unit_price.toFixed(2)}` : "\u2014"}
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data font-semibold text-[#1D2939]" data-testid={`grn-confirmed-detail-line-value-${i}`}>
                          {lineValue != null ? `${it.currency || ""} ${lineValue.toFixed(2)}` : "\u2014"}
                        </td>
                        {confirmedDetail.sap_gr_result?.per_po && (
                          <td className="border border-[#D0D5DD] px-2 py-1 whitespace-nowrap" data-testid={`grn-confirmed-detail-po-status-${i}`}>
                            {poResult?.status === "notification_created" ? (
                              <span className="text-[11px] font-semibold text-[#3538CD]">Notification Created</span>
                            ) : poResult ? (
                              <button
                                type="button"
                                className={`text-[11px] font-semibold hover:underline ${poResult.status !== "posted" ? (poResult.status === "skipped" ? "text-[#B54708]" : "text-[#B42318]") : poDiagnostics(confirmedDetail, poResult).movement_issues.length > 0 ? "text-[#B54708]" : "text-[#027A48]"}`}
                                onClick={() => setDiagnosticsModal(poDiagnostics(confirmedDetail, poResult))}
                                data-testid={`grn-confirmed-detail-diagnostics-link-${i}`}
                              >
                                {poResult.status === "posted" ? (poDiagnostics(confirmedDetail, poResult).movement_issues.length > 0 ? "Posted (movement failed)" : "Posted") : poResult.status === "skipped" ? "Skipped" : "Failed"} - Diagnostics
                              </button>
                            ) : (
                              <span className="text-[11px] text-[#98A2B3]">Not attempted yet</span>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={!!screenshotModal} onOpenChange={(o) => !o && setScreenshotModal(null)}>
        <DialogContent className="rounded-sm max-w-3xl" data-testid="grn-failure-screenshot-dialog">
          {screenshotModal && (
            <>
              <DialogHeader>
                <DialogTitle className="font-heading text-[#7A1E1E]">PO {screenshotModal.po_number} - failed while {describeFailedStep(screenshotModal.failed_step)}</DialogTitle>
                <DialogDescription className="font-data text-xs">{screenshotModal.error}</DialogDescription>
              </DialogHeader>
              {screenshotModal.screenshot_path ? (
                <img
                  src={`${API}/admin/grn/failure-screenshot?path=${encodeURIComponent(screenshotModal.screenshot_path)}`}
                  alt="SAP screen at time of failure"
                  className="w-full border border-[#D0D5DD] rounded-sm"
                  data-testid="grn-failure-screenshot-image"
                />
              ) : (
                <p className="text-sm text-[#667085]">No screenshot could be captured for this attempt.</p>
              )}
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={!!diagnosticsModal} onOpenChange={(o) => !o && setDiagnosticsModal(null)}>
        <DialogContent className="rounded-sm max-w-xl" data-testid="grn-diagnostics-dialog">
          {diagnosticsModal && (
            <>
              <DialogHeader>
                <DialogTitle className="font-heading text-[#344054]">PO {diagnosticsModal.po_number} - Diagnostics</DialogTitle>
                <DialogDescription className="font-data text-xs">
                  {diagnosticsModal.status === "skipped" ? (
                    <span className="text-[#B54708] font-semibold">Skipped - never sent to SAP</span>
                  ) : diagnosticsModal.status === "posted" ? (
                    diagnosticsModal.skipped_items?.length > 0 ? (
                      <span className="text-[#B54708] font-semibold">Posted to SAP - but item(s) below were dropped from this PO</span>
                    ) : diagnosticsModal.movement_issues?.length > 0 ? (
                      <span className="text-[#B54708] font-semibold">Posted to SAP - but the warehouse movement into place failed</span>
                    ) : (
                      <span className="text-[#027A48] font-semibold">Posted to SAP successfully - no issues</span>
                    )
                  ) : (
                    <>Failed while: <strong className="text-[#7A1E1E]">{describeFailedStep(diagnosticsModal.failed_step)}</strong></>
                  )}
                </DialogDescription>
              </DialogHeader>
              {diagnosticsModal.error && (
                <div className="text-sm text-[#7A1E1E] bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-2" data-testid="grn-diagnostics-sap-error">
                  <strong>{diagnosticsModal.status === "skipped" ? "Reason:" : "SAP Error:"}</strong> {diagnosticsModal.error}
                </div>
              )}
              {diagnosticsModal.skipped_items?.length > 0 && (
                <div className="text-sm text-[#93370D] bg-[#FFFAEB] border border-[#FEC84B] rounded-sm p-2" data-testid="grn-diagnostics-skipped-items">
                  <strong>Dropped item(s) - not received in SAP:</strong>
                  <ul className="mt-1 space-y-1">
                    {diagnosticsModal.skipped_items.map((s, i) => (
                      <li key={i} data-testid={`grn-diagnostics-skipped-item-${i}`}>{s.item_number}: {s.reason}</li>
                    ))}
                  </ul>
                </div>
              )}
              {diagnosticsModal.movement_issues?.length > 0 && (
                <div className="text-sm text-[#7A1E1E] bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-2" data-testid="grn-diagnostics-movement-issues">
                  <strong>Goods Receipt posted fine - warehouse movement into place failed:</strong>
                  <ul className="mt-1 space-y-1">
                    {diagnosticsModal.movement_issues.map((m, i) => (
                      <li key={i} data-testid={`grn-diagnostics-movement-issue-${i}`}>{m.product_id}: {m.error}</li>
                    ))}
                  </ul>
                </div>
              )}
              <div>
                <p className="text-xs font-heading font-bold uppercase tracking-wide text-[#667085] mb-1.5">
                  {diagnosticsModal.status === "skipped" ? "What happened before it was skipped" : diagnosticsModal.status === "posted" ? "Events completed for this PO" : "Events completed successfully before the failure"}
                </p>
                {diagnosticsModal.events?.length ? (
                  <ol className="text-sm space-y-1.5" data-testid="grn-diagnostics-events-list">
                    {diagnosticsModal.events.map((e, i) => (
                      <li key={i} className="flex gap-2" data-testid={`grn-diagnostics-event-${i}`}>
                        <span className="text-[#12B76A] font-bold shrink-0">{i + 1}.</span>
                        <span className="text-[#344054]">{e}</span>
                      </li>
                    ))}
                  </ol>
                ) : diagnosticsModal.status === "posted" ? (
                  <p className="text-sm text-[#98A2B3]">No additional diagnostic events were recorded for this PO.</p>
                ) : (
                  <p className="text-sm text-[#98A2B3]">No events were logged before this failure - it likely crashed immediately.</p>
                )}
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
