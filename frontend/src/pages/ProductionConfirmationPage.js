import { useState, useEffect, useCallback, useMemo, useRef, Fragment } from "react";
import * as XLSX from "xlsx";
import "@/App.css";
import axios from "axios";
import {
  Shield,
  ArrowClockwise,
  MagnifyingGlass,
  CheckCircle,
  WarningCircle,
  ClockCounterClockwise,
  ListChecks,
  Gear,
  Trash,
  Plus,
  CircleNotch,
  Circle,
  X,
  CaretRight,
  CaretDown,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { MyStockRequestsTab } from "@/components/MyStockRequestsTab";
import { useAuth } from "@/contexts/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const rowKey = (r) => `${r.production_lot_id}::${r.confirmation_group_uuid}::${r.reporting_point_uuid}`;

const StatCard = ({ icon: Icon, label, value, testId }) => (
  <div className="bg-white border border-[#D0D5DD] rounded-sm p-3 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] flex flex-col gap-1.5" data-testid={testId}>
    <div className="flex items-center gap-1.5 text-[#475467]">
      <Icon size={14} weight="bold" />
      <span className="font-heading text-xs font-bold uppercase tracking-wider">{label}</span>
    </div>
    <span className="font-sans text-2xl font-bold tabular-nums text-[#1D2939]">{value}</span>
  </div>
);

const formatQty = (v) => (v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 2 }));

// SAP's own BOM data returns "MASS" as the raw unit label (it's actually
// a dimension/QuantityTypeCode, not a real unit - the Goods Movement bug
// fix this session confirmed the real SAP unit code is "KGM"/kilogram).
// Showing the raw "MASS" text to planners reads as a typo/bug - display
// the real, human-friendly unit instead. Internal values/keys (unit_code
// sent back to the API, etc.) are untouched - this is a display-only map.
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");

// Aug 2026, matches production_confirmation_service.is_usable_stock_status()
// - flags Quality Inspection/Blocked stock in this planner-facing view too
// (same underlying store_requests data as the Store Approval screen).
const RESTRICTED_STOCK_STATUSES = new Set(["inspection", "quality inspection", "blocked", "restricted-use", "restricted", "in transit"]);
const isRestrictedStatus = (status) => RESTRICTED_STOCK_STATUSES.has((status || "").trim().toLowerCase());
// Aug 2026 bug fix: SAP's CRESTRICTED_IND flag is a SEPARATE field from
// stock_status - a row can read stock_status "Not Assigned" (normally
// usable) while still being Restricted Use. See the matching backend
// docstring on is_usable_stock_status for the real incident this was
// traced to.
const isLocationRestricted = (loc) => isRestrictedStatus(loc?.stock_status) || !!loc?.restricted;

// User's explicit ask: today posting/WIP-clearing/by-product outcomes
// only ever show as a toast at confirm-time, then vanish - this renders
// the LAST confirmation's outcome persistently on the row itself.
// Aug 27 2026 (user's explicit ask): a failed "WIP Cleared" chip now
// carries its own inline "Retry" so a stalled step can be fixed right
// here instead of only being visible/actionable elsewhere. Same for a
// failed "FG Moved" chip (SFG -> FG Goods Movement retry).
const LastConfirmationBadges = ({ data, productionLotId, siteId, mainOutputProduct, unitCode, actorName, onRetried }) => {
  const [retryingWip, setRetryingWip] = useState(false);
  const [retryingFg, setRetryingFg] = useState(false);
  if (!data) return <span className="text-[#98A2B3] text-xs">—</span>;
  const chip = (ok, label) => (
    <span className={`flex items-center gap-1 text-[10px] px-1 py-0.5 rounded-sm border w-fit ${
      ok ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]" : "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]"
    }`}>
      {ok ? <CheckCircle size={10} weight="fill" /> : <WarningCircle size={10} weight="fill" />}
      {label}
    </span>
  );
  const retryWip = async () => {
    setRetryingWip(true);
    try {
      const { data: res } = await axios.post(`${API}/production-confirmation/retry-wip-clearing`, {
        production_lot_id: productionLotId, site_id: siteId, actor: actorName.trim(),
      });
      if (res.wip_clearing?.success) toast.success(`WIP Clearing Run succeeded for Lot ${productionLotId}`);
      else toast.error(`WIP Clearing Run failed again: ${res.wip_clearing?.log || "see SAP for details"}`);
      onRetried?.(productionLotId, { wip_clearing: res.wip_clearing });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to retry WIP Clearing");
    } finally {
      setRetryingWip(false);
    }
  };
  const retryFg = async () => {
    setRetryingFg(true);
    try {
      const { data: res } = await axios.post(`${API}/production-confirmation/retry-fg-movement`, {
        production_lot_id: productionLotId, site_id: siteId, main_output_product: mainOutputProduct,
        confirmed_quantity: data.confirmed_quantity, unit_code: unitCode, actor: actorName.trim(),
      });
      if (res.fg_movement?.ok) toast.success(`Moved to ${siteId}-FG for Lot ${productionLotId}`);
      else toast.error(`FG Movement failed again: ${res.fg_movement?.error || "see SAP for details"}`);
      onRetried?.(productionLotId, { fg_movement: res.fg_movement });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to retry FG Movement");
    } finally {
      setRetryingFg(false);
    }
  };
  return (
    <div className="space-y-0.5" data-testid="last-confirmation-badges">
      {chip(!!data.success, "Posted")}
      {data.wip_clearing != null && (
        <div className="flex items-center gap-1">
          {chip(!!data.wip_clearing.success, "WIP Cleared")}
          {!data.wip_clearing.success && siteId && (
            <button
              type="button"
              disabled={retryingWip}
              onClick={retryWip}
              className="text-[10px] underline text-[#175CD3] hover:text-[#0E4B99] disabled:opacity-50"
              data-testid="retry-wip-clearing-button"
            >
              {retryingWip ? "Retrying..." : "Retry"}
            </button>
          )}
        </div>
      )}
      {data.byproduct_confirmation != null && chip(!!data.byproduct_confirmation.success, "By-product")}
      {data.fg_movement != null && (
        <div className="flex items-center gap-1">
          {chip(!!data.fg_movement.ok, "FG Moved")}
          {!data.fg_movement.ok && siteId && mainOutputProduct && (
            <button
              type="button"
              disabled={retryingFg}
              onClick={retryFg}
              className="text-[10px] underline text-[#175CD3] hover:text-[#0E4B99] disabled:opacity-50"
              data-testid="retry-fg-movement-button"
            >
              {retryingFg ? "Retrying..." : "Retry"}
            </button>
          )}
        </div>
      )}
    </div>
  );
};

// Aug 27 2026, user's explicit ask: clear step-by-step progress while
// confirming, instead of one opaque "Posting to SAP..." label for the
// whole duration.
const CONFIRM_PHASE_LABELS = {
  checking_category: "Checking item category (Finished Goods vs Sub-Assembly)...",
  moving_to_fg: "Moving stock to FG warehouse...",
};



const STATUS_TONE = {
  Released: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]",
  Started: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]",
  Finished: "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]",
};

// -------------------- Confirm dialog --------------------
const ConfirmDialog = ({ row, actorName, onClose, onConfirmed, reasons }) => {
  const [confirmedQty, setConfirmedQty] = useState(row ? (row.open_quantity ?? "") : "");
  const [confirmedScrap, setConfirmedScrap] = useState("0");
  const [scrapCalc, setScrapCalc] = useState(null);
  const [reason, setReason] = useState("none");
  const [finished, setFinished] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingElapsed, setSavingElapsed] = useState(0);
  const [savingPhase, setSavingPhase] = useState(null);
  const [availability, setAvailability] = useState(null);
  const [checkingAvailability, setCheckingAvailability] = useState(false);
  const [confirmError, setConfirmError] = useState(null);
  // testing_agent iteration_105: abort the poll loop below if this dialog
  // is closed/switched to a different row mid-poll - the background SAP
  // job itself keeps running regardless (nothing is lost), this just
  // stops updating THIS closed instance's state on a stale poll.
  const pollAbortRef = useRef(false);

  useEffect(() => {
    if (!saving) {
      setSavingElapsed(0);
      return;
    }
    const interval = setInterval(() => setSavingElapsed((s) => s + 1), 1000);
    return () => clearInterval(interval);
  }, [saving]);

  useEffect(() => {
    if (row) {
      setConfirmedQty(row.open_quantity ?? "");
      setConfirmedScrap("0");
      setScrapCalc(null);
      setReason("none");
      // Aug 2026 (user's explicit rule): "finished" is now ALWAYS true and
      // locked - every confirmation closes the lot for good and always
      // triggers a WIP Clearing Run. No more multi-stage partial
      // confirmations against the same lot.
      setFinished(true);
      setAvailability(null);
      setConfirmError(null);
      pollAbortRef.current = false;
      if (row.main_output_product) {
        axios.post(`${API}/production-confirmation/scrap-calc/${encodeURIComponent(row.main_output_product)}`, {
          material_inputs: row.material_inputs || null,
        })
          .then(({ data }) => setScrapCalc(data))
          .catch(() => setScrapCalc(null));
      }
    }
    return () => {
      pollAbortRef.current = true;
    };
  }, [row]);

  // NOTE: Confirmed Scrap is a manually-entered REJECTED QUANTITY (defective
  // units the operator is reporting), unrelated to the physical by-product
  // material weight below - it is written to SAP's own ConfirmedScrap field.
  // It must NEVER auto-fill from the weight-based scrap calc.

  useEffect(() => {
    if (!row || confirmedQty === "" || Number.isNaN(Number(confirmedQty))) {
      setAvailability(null);
      return;
    }
    const timer = setTimeout(() => {
      setCheckingAvailability(true);
      axios.post(`${API}/production-confirmation/component-availability`, {
        main_output_product: row.main_output_product, confirmed_quantity: Number(confirmedQty), site_id: row.site_id,
        material_inputs: row.material_inputs || null,
      }).then(({ data }) => setAvailability(data)).catch(() => setAvailability(null)).finally(() => setCheckingAvailability(false));
    }, 400);
    return () => clearTimeout(timer);
  }, [row, confirmedQty]);

  if (!row) return null;

  // Match on the LOT's real Output Products grid, not our own AI-guessed
  // scrap-family code - confirmed live that SAP's actual by-product line
  // (e.g. IRON-SCR) doesn't always match our classifier's expected code
  // (e.g. CR-SCRAP guessed from the RM description). Any output line that
  // isn't the main product IS the by-product to confirm.
  const byproductMatch = row.material_outputs?.find((mo) => mo.product_id !== row.main_output_product) || null;

  // If no output line exists AT ALL for the expected by-product, we can
  // still CREATE one from scratch (SAP supports ActionCode="01" on a
  // brand-new MaterialOutput) - reuse the main output's own target
  // logistics area as the destination, since a by-product almost always
  // shares the main output's site/storage area.
  const mainOutputRow = row.material_outputs?.find((mo) => mo.product_id === row.main_output_product) || null;
  const canAutoCreateByproduct = !byproductMatch && !!mainOutputRow?.target_logistics_area_id;

  // Weight-based by-product quantity - fully independent of the manual
  // Confirmed Scrap (rejection) field above.
  const qtyNum = Number(confirmedQty);
  const byproductQty = scrapCalc?.available && confirmedQty !== "" && !Number.isNaN(qtyNum)
    ? Math.round(scrapCalc.scrap_per_unit_kg * qtyNum * 1e6) / 1e6
    : null;

  // Aug 2026 (user's explicit rule): "finished" is now a mandatory, locked
  // true on every confirmation regardless of quantity entered - no smart
  // default toggling anymore.
  const updateConfirmedQty = (value) => {
    setConfirmedQty(value);
  };

  const qtyExceedsOpen = confirmedQty !== "" && !Number.isNaN(qtyNum) && qtyNum > row.open_quantity;

  const submit = async () => {
    const qty = confirmedQty === "" ? null : Number(confirmedQty);
    const scrap = confirmedScrap === "" ? null : Number(confirmedScrap);
    if (qty !== null && (Number.isNaN(qty) || qty < 0)) {
      toast.error("Confirmed Output Quantity must be a valid, non-negative number");
      return;
    }
    if (scrap !== null && (Number.isNaN(scrap) || scrap < 0)) {
      toast.error("Confirmed Scrap must be a valid, non-negative number");
      return;
    }
    // Aug 27 2026 fix (user's explicit ask - a real confirmation went
    // through with no by-product ever posted): block submission
    // up-front whenever a by-product IS expected (an RM component was
    // found) but can't actually be posted right now - either the weight
    // data is missing, or there's nowhere to post it to - instead of
    // silently letting the confirmation through with by-product fields
    // all null. Skipped only when scrapCalc genuinely found no RM
    // component at all (a real assembly-only item with no by-product).
    if (scrapCalc?.rm_product_id && !scrapCalc.available) {
      toast.error(`Cannot confirm: by-product is expected but its weight can't be calculated yet (${scrapCalc.reason}). Fix this first so the by-product is never skipped.`);
      return;
    }
    if (scrapCalc?.available && !byproductMatch && !canAutoCreateByproduct) {
      toast.error("Cannot confirm: a by-product is expected but there's no Output Products line to post it to and no target storage area to create one - contact support before confirming this lot.");
      return;
    }
    setSaving(true);
    setSavingPhase(null);
    setConfirmError(null);
    try {
      const { data: jobData } = await axios.post(`${API}/production-confirmation/confirm`, {
        production_lot_id: row.production_lot_id,
        production_lot_uuid: row.production_lot_uuid,
        confirmation_group_uuid: row.confirmation_group_uuid,
        reporting_point_uuid: row.reporting_point_uuid,
        reporting_point_id: row.reporting_point_id,
        main_output_product: row.main_output_product,
        unit_code: row.unit_code,
        production_task_id: row.production_task_id,
        production_task_uuid: row.production_task_uuid,
        confirmed_quantity: qty,
        confirmed_scrap: scrap,
        deviation_reason_code: reason === "none" ? null : reason,
        confirmation_finished: finished,
        site_id: row.site_id,
        byproduct_material_output_uuid: byproductMatch?.material_output_uuid || null,
        byproduct_confirmed_quantity: byproductMatch ? byproductQty : null,
        byproduct_unit_code: byproductMatch?.unit_code || null,
        new_byproduct_product_id: canAutoCreateByproduct && byproductQty > 0 ? scrapCalc.scrap_family.expected_byproduct_code : null,
        new_byproduct_target_logistics_area_id: canAutoCreateByproduct && byproductQty > 0 ? mainOutputRow.target_logistics_area_id : null,
        new_byproduct_confirmed_quantity: canAutoCreateByproduct && byproductQty > 0 ? byproductQty : null,
        new_byproduct_unit_code: canAutoCreateByproduct && byproductQty > 0 ? "KGM" : null,
        material_inputs: row.material_inputs || null,
        actor: actorName.trim(),
      });
      // Runs as a background job (Aug 2026 fix) - posting to SAP can take
      // long enough (by-product + main + finish task + WIP clearing, up
      // to 4 sequential SOAP calls) to blow past the platform's ingress
      // timeout and surface as a raw, unhelpful Cloudflare error instead
      // of a real SAP message (reproduced live on Lot 70222 with short
      // stock). Polling here instead means that can never happen again.
      // testing_agent iteration_105: bounded to a 3-min deadline (well
      // above the realistic worst case for up to 4 sequential SOAP
      // calls) and retries a single transient status-GET failure instead
      // of immediately reporting a false failure - the background job
      // keeps running server-side either way, nothing is lost.
      const deadline = Date.now() + 3 * 60 * 1000;
      let job = null;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 3000));
        if (pollAbortRef.current) return;
        try {
          const { data } = await axios.get(`${API}/production-confirmation/confirm/status/${jobData.job_id}`);
          job = data;
          // Aug 27 2026 (user's explicit ask): "show progress with steps
          // clearly" while the backend is live-checking category/moving
          // stock to FG, instead of one opaque "Posting to SAP..." label.
          setSavingPhase(job.status);
        } catch {
          continue; // transient network hiccup - just retry on the next tick
        }
        if (job.status === "done" || job.status === "failed") break;
        job = null;
      }
      if (pollAbortRef.current) return;
      if (!job) {
        setConfirmError(`Still waiting on SAP for Lot ${row.production_lot_id} after 3 minutes - it may still complete in the background. Check History shortly before retrying to avoid a duplicate confirmation.`);
        return;
      }
      if (job.status === "failed") {
        // Persistent on-screen banner instead of a toast (Aug 2026 user
        // feedback: "the error is a toast, instead show a message on
        // screen user will close so it does not disappear") - a real SAP
        // rejection/timeout message here is important enough that it
        // must stay visible until the user dismisses it themselves.
        setConfirmError(job.error || "Failed to post confirmation to SAP");
        return;
      }
      const data = job.result;
      if (data.success) {
        toast.success(`Confirmation posted to SAP for Lot ${row.production_lot_id}`);
        if (data.byproduct_confirmation) {
          if (data.byproduct_confirmation.success) {
            toast.success(`By-product ${byproductMatch?.product_id || "quantity"} (${byproductQty ?? 0} ${formatUnit(byproductMatch?.unit_code)}) posted to SAP`);
          } else {
            toast.error(`By-product quantity failed to post: ${data.byproduct_confirmation.logs?.map((l) => l.note).join("; ") || "see history for details"}`);
          }
        }
        if (finished && data.wip_clearing) {
          if (data.wip_clearing.success) {
            toast.success(`WIP Clearing Run triggered for Lot ${row.production_lot_id}`);
          } else {
            toast.error(`WIP Clearing Run failed: ${data.wip_clearing.log || "see history for details"}`);
          }
        }
        if (data.fg_movement) {
          if (data.fg_movement.ok) {
            toast.success(`Finished Goods moved to ${row.site_id}-FG for Lot ${row.production_lot_id}`);
          } else {
            toast.error(`FG Movement failed: ${data.fg_movement.error || "see history for details"}`);
          }
        }
        onConfirmed(row);
      } else {
        // clarified_error (Aug 2026, testing_agent iteration_105): a
        // normal SAP business rejection (no exception) now gets the same
        // readable guidance as an exception/timeout path instead of raw
        // SAP log text like "Modification failed".
        setConfirmError(data.clarified_error || `SAP reported an issue: ${data.logs?.map((l) => l.note).join("; ") || "see history for details"}`);
      }
    } catch (e) {
      if (!pollAbortRef.current) setConfirmError(e.response?.data?.detail || "Failed to post confirmation to SAP");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={!!row} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md" data-testid="confirm-production-dialog">
        <DialogHeader>
          <DialogTitle>Confirm Production Task</DialogTitle>
          <DialogDescription>
            Lot {row.production_lot_id} · {row.main_output_product || "—"} · Reporting Point {row.reporting_point_id || "—"}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-1">
          {confirmError && (
            <Alert variant="destructive" className="relative pr-9 rounded-sm border-[#F04438]/40 bg-[#FEF3F2]" data-testid="confirm-error-banner">
              <WarningCircle size={16} />
              <AlertTitle className="font-heading text-sm">Confirmation failed</AlertTitle>
              <AlertDescription className="font-sans text-[13px] break-words">{confirmError}</AlertDescription>
              <button
                type="button"
                onClick={() => setConfirmError(null)}
                className="absolute top-3 right-3 text-[#B42318]/70 hover:text-[#B42318]"
                data-testid="confirm-error-banner-close"
                aria-label="Dismiss error"
              >
                <X size={16} />
              </button>
            </Alert>
          )}
          <div className="text-xs text-[#667085] bg-[#F9FAFB] border border-[#EAECF0] rounded-sm px-3 py-2">
            Planned: <strong className="text-[#1D2939]">{formatQty(row.planned_quantity)}</strong> {formatUnit(row.unit_code)} ·
            {" "}Open: <strong className="text-[#1D2939]">{formatQty(row.open_quantity)}</strong> {formatUnit(row.unit_code)}
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Confirmed Output Quantity</Label>
            <Input type="number" value={confirmedQty} onChange={(e) => updateConfirmedQty(e.target.value)} data-testid="confirm-qty-input" />
            {qtyExceedsOpen && (
              <p className="text-xs text-[#B54708] mt-1" data-testid="confirm-qty-exceeds-open-warning">
                Note: {confirmedQty} exceeds the Open Quantity ({row.open_quantity} {formatUnit(row.unit_code)}) - double-check before posting if this isn't intentional over-production.
              </p>
            )}
          </div>

          {checkingAvailability && <p className="text-xs text-[#98A2B3]">Checking component stock...</p>}
          {availability?.checked && (
            <div className="border border-[#EAECF0] rounded-sm overflow-hidden" data-testid="component-availability-panel">
              <div className="bg-[#F9FAFB] px-3 py-1.5 text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                Component Stock Check {row.site_id ? `(Site ${row.site_id})` : ""}
              </div>
              <div className="max-h-48 overflow-y-auto divide-y divide-[#EAECF0]">
                {availability.components.map((c) => (
                  <div key={c.product_id} className="px-3 py-1.5 text-xs" data-testid={`component-row-${c.product_id}`}>
                    <div className="flex items-center justify-between">
                      <span className="text-[#344054] truncate mr-2">{c.product_id}{c.description ? ` - ${c.description}` : ""}</span>
                      <span className={`shrink-0 tabular-nums ${c.sufficient ? "text-[#027A48]" : c.available_qty === null ? "text-[#98A2B3]" : "text-[#B42318] font-bold"}`}>
                        {c.available_qty === null ? "no stock data" : `${formatQty(c.available_qty)} / ${formatQty(c.required_qty)} ${formatUnit(c.unit_of_measure)}`}
                        {c.sufficient ? " ✓" : c.available_qty !== null ? " ✗" : ""}
                      </span>
                    </div>
                    {/* Per-warehouse/stock-status breakdown at this site - user's
                        explicit ask (Aug 2026): show SITE + Warehouse (e.g. RM/
                        SFG/FG) + Stock Status, not just a single aggregate qty. */}
                    {c.locations && c.locations.length > 0 && (
                      <div className="mt-0.5 pl-2 text-[11px] text-[#667085] space-y-0.5" data-testid={`component-locations-${c.product_id}`}>
                        {c.locations.map((loc, li) => (
                          <div key={li}>
                            {row.site_id}{loc.warehouse ? ` / ${loc.warehouse}` : ""}{loc.stock_status ? ` (${loc.stock_status})` : ""}: {formatQty(loc.qty)} {formatUnit(c.unit_of_measure)}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
                {availability.components.length === 0 && <div className="px-3 py-1.5 text-xs text-[#98A2B3]">No active components in cached BOM.</div>}
              </div>
              {availability.components.some((c) => !c.sufficient && c.available_qty !== null) && (
                <div className="bg-[#FFFAEB] px-3 py-1.5 text-[11px] text-[#B54708]">Some components may be short - SAP's backflush could reject this confirmation.</div>
              )}
            </div>
          )}
          {availability && !availability.checked && (
            <p className="text-[11px] text-[#98A2B3]">{availability.reason}</p>
          )}

          {scrapCalc?.available && (
            <div className="text-xs text-[#344054] bg-[#F9FAFB] border border-[#EAECF0] rounded-sm px-3 py-2" data-testid="scrap-calc-info">
              <div className="font-bold font-heading uppercase tracking-wide text-[10px] text-[#667085] mb-1">Auto-calculated from {scrapCalc.rm_product_id}{scrapCalc.rm_description ? ` (${scrapCalc.rm_description})` : ""}</div>
              Gross Weight: <strong data-testid="scrap-calc-gross">{scrapCalc.gross_weight_kg}</strong> kg ·
              {" "}Net Weight: <strong data-testid="scrap-calc-net">{scrapCalc.net_weight_kg}</strong> kg ·
              {" "}Scrap/unit: <strong data-testid="scrap-calc-per-unit">{scrapCalc.scrap_per_unit_kg}</strong> kg
              {scrapCalc.scrap_family && (
                <div className="mt-1 text-[#667085]" data-testid="scrap-calc-family">
                  Expected by-product: {scrapCalc.scrap_family.family} ({scrapCalc.scrap_family.expected_byproduct_code})
                  {byproductMatch
                    ? <span className="text-[#027A48]" data-testid="byproduct-match-found"> · will post {byproductQty ?? 0} {formatUnit(byproductMatch.unit_code)} of {byproductMatch.product_id} to SAP</span>
                    : canAutoCreateByproduct
                      ? <span className="text-[#0E7C86]" data-testid="byproduct-auto-create"> · no output line exists yet - will auto-create it and post {byproductQty ?? 0} KGM to SAP</span>
                      : <span className="text-[#B54708]" data-testid="byproduct-match-missing"> · no by-product output line found on this lot - only Confirmed Quantity will be posted</span>}
                </div>
              )}
            </div>
          )}
          {scrapCalc && !scrapCalc.available && (
            <p className="text-[11px] text-[#667085]" data-testid="scrap-calc-unavailable">Scrap auto-calc unavailable: {scrapCalc.reason}</p>
          )}
          <div>
            <Label className="text-xs font-bold text-[#344054]">Confirmed Scrap (Rejected Qty)</Label>
            <Input type="number" value={confirmedScrap} onChange={(e) => setConfirmedScrap(e.target.value)} data-testid="confirm-scrap-input" />
            <p className="text-[11px] text-[#98A2B3] mt-0.5">Manually enter rejected/defective units, if any. Unrelated to the by-product weight below.</p>
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Deviation Reason</Label>
            <Select value={reason} onValueChange={setReason}>
              <SelectTrigger data-testid="confirm-deviation-reason-select"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                {reasons.map((r) => (
                  <SelectItem key={r.code} value={r.code}>{r.code} - {r.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="finished-cb" checked={finished} disabled data-testid="confirm-finished-checkbox" />
            <Label htmlFor="finished-cb" className="text-sm text-[#344054]">Mark this task as finished (mandatory - every confirmation closes this lot)</Label>
          </div>
          <p className="text-[11px] text-[#98A2B3]">Component/input quantities are NOT sent - SAP's backflush auto-consumes BOM inputs from the confirmed output.</p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} data-testid="confirm-cancel-button">Cancel</Button>
          <Button onClick={submit} disabled={saving || checkingAvailability} data-testid="confirm-submit-button">
            {saving ? `${CONFIRM_PHASE_LABELS[savingPhase] || "Posting to SAP"} (${savingElapsed}s)...` : checkingAvailability ? "Checking stock..." : "Post Confirmation"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

// -------------------- Manage Reasons dialog --------------------
const ManageReasonsDialog = ({ open, onClose, reasons, onChanged }) => {
  const [code, setCode] = useState("");
  const [label, setLabel] = useState("");

  const add = async () => {
    if (!code.trim() || !label.trim()) return;
    const isUpdate = reasons.some((r) => r.code === code.trim());
    try {
      const { data } = await axios.post(`${API}/production-confirmation/deviation-reasons`, { code: code.trim(), label: label.trim() });
      onChanged(data.reasons);
      setCode("");
      setLabel("");
      toast.success(isUpdate ? `Updated reason ${code.trim()}` : `Added reason ${code.trim()}`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to save deviation reason");
    }
  };

  const remove = async (c) => {
    try {
      const { data } = await axios.delete(`${API}/production-confirmation/deviation-reasons/${c}`);
      onChanged(data.reasons);
      toast.success(`Removed reason ${c}`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to delete deviation reason");
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" data-testid="manage-reasons-dialog">
        <DialogHeader>
          <DialogTitle>Manage Deviation Reasons</DialogTitle>
          <DialogDescription>Seeded with SAP's standard code list - adjust to match this tenant's exact fine-tuning if different.</DialogDescription>
        </DialogHeader>
        <div className="max-h-56 overflow-y-auto space-y-1">
          {reasons.map((r) => (
            <div key={r.code} className="flex items-center justify-between px-2 py-1.5 rounded-sm bg-[#F9FAFB] border border-[#EAECF0]" data-testid={`reason-row-${r.code}`}>
              <span className="text-sm text-[#344054]"><strong>{r.code}</strong> - {r.label}</span>
              <button onClick={() => remove(r.code)} className="text-[#B42318] hover:text-[#912018]" data-testid={`reason-delete-${r.code}`}>
                <Trash size={14} weight="bold" />
              </button>
            </div>
          ))}
        </div>
        <div className="flex gap-2 pt-2 border-t border-[#EAECF0]">
          <Input placeholder="Code (e.g. 009)" value={code} onChange={(e) => setCode(e.target.value)} className="w-28" data-testid="reason-code-input" />
          <Input placeholder="Label" value={label} onChange={(e) => setLabel(e.target.value)} data-testid="reason-label-input" />
          <Button onClick={add} data-testid="reason-add-button"><Plus size={14} /></Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};

// -------------------- History dialog --------------------
const HistoryDialog = ({ open, onClose }) => {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    axios.get(`${API}/production-confirmation/history`).then(({ data }) => setEntries(data.entries)).finally(() => setLoading(false));
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-auto" data-testid="confirmation-history-dialog">
        <DialogHeader>
          <DialogTitle>Confirmation History</DialogTitle>
          <DialogDescription>Every confirmation submitted from this page, most recent first.</DialogDescription>
        </DialogHeader>
        {loading ? <Skeleton className="h-40 w-full" /> : (
          <div className="border border-[#D0D5DD] rounded-sm overflow-auto">
            <table className="w-full text-[12px] border-collapse" data-testid="confirmation-history-table">
              <thead>
                <tr>
                  {["When", "By", "Lot", "Product", "Qty", "Scrap", "Finished", "Result", "WIP Clearing", "FG Movement"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`history-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1">{new Date(e.at).toLocaleString("en-IN")}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1">{e.actor}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1">{e.production_lot_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1">{e.main_output_product || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums">{formatQty(e.confirmed_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums">{formatQty(e.confirmed_scrap)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1">{e.confirmation_finished ? "Yes" : "No"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1">
                      {e.success ? <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Success</Badge> : <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1">
                      {!e.wip_clearing ? "—" : e.wip_clearing.success ? <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Cleared</Badge> : <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1">
                      {!e.fg_movement ? "—" : e.fg_movement.ok ? <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Moved</Badge> : <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>}
                    </td>
                  </tr>
                ))}
                {entries.length === 0 && <tr><td colSpan={10} className="text-center py-6 text-[#98A2B3] border border-[#D0D5DD]">No confirmations submitted yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
};

// -------------------- Create Production Order tab --------------------
const ACTIVE_JOBS_STORAGE_KEY = "productionConfirmationActiveJobs";
const POLL_INTERVAL_MS = 4000;

// These 2 statuses PAUSE the automated pipeline for a human action (store
// issuing stock, or the requester deciding on a partial issue).
const PAUSED_STATUSES = ["waiting_store_approval", "partial_pending_planner"];

const PHASE_LABELS = {
  running: "Submitting...",
  checking_stock: "Checking Stock",
  creating_proposal: "Creating Proposal",
  waiting_for_order: "Posting to SAP",
  releasing_order: "Releasing Order",
  waiting_store_approval: "Waiting for Store",
  partial_pending_planner: "Awaiting Your Decision",
};

// Linear happy-path order of the automated pipeline (excludes the 2
// PAUSED_STATUSES above, which branch off to their own Badge+actions UI,
// and excludes "done"/"failed"/"cancelled" which remove the row entirely
// via a toast the instant they're seen - see pollJob). Some of these
// steps (creating_proposal in particular, often also releasing_order when
// SAP self-releases) can complete in well under one poll interval, so a
// user watching only the CURRENT status would rarely if ever see them -
// this tracker instead marks every step up to the current one as done
// (checkmarked) so a fast step still visibly registers as completed
// rather than seeming to have never happened.
const ORDER_STEP_KEYS = ["checking_stock", "creating_proposal", "waiting_for_order", "releasing_order"];

const OrderStepTracker = ({ status, elapsedSeconds }) => {
  // Any status not yet in ORDER_STEP_KEYS (e.g. "running", seeded right
  // after submit or restored from localStorage before the first poll
  // resolves) is treated as step 0 "current" rather than indexOf's -1
  // (which would render every step gray/pending with no spinner at all -
  // testing_agent iteration_101 caught this as a ~4s cosmetic gap).
  const rawIndex = ORDER_STEP_KEYS.indexOf(status);
  const currentIndex = rawIndex === -1 ? 0 : rawIndex;
  return (
    <div className="flex items-center gap-1 flex-wrap" data-testid="order-step-tracker">
      {ORDER_STEP_KEYS.map((key, idx) => {
        const isDone = currentIndex > idx;
        const isCurrent = currentIndex === idx;
        return (
          <span key={key} className="flex items-center gap-1">
            <span
              className={`flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-sm border whitespace-nowrap ${
                isDone ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]"
                : isCurrent ? "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]"
                : "bg-[#F9FAFB] text-[#98A2B3] border-[#EAECF0]"
              }`}
              data-testid={`order-step-${key}`}
              data-step-state={isDone ? "done" : isCurrent ? "current" : "pending"}
            >
              {isDone ? <CheckCircle size={11} weight="fill" /> : isCurrent ? <CircleNotch size={11} className="animate-spin" /> : <Circle size={11} />}
              {PHASE_LABELS[key]}
            </span>
            {idx < ORDER_STEP_KEYS.length - 1 && <span className="text-[#D0D5DD]">{"\u2192"}</span>}
          </span>
        );
      })}
      <span className="text-[11px] text-[#98A2B3] tabular-nums ml-1" data-testid="order-step-elapsed">{elapsedSeconds}s</span>
    </div>
  );
};

const CreateOrderTab = ({ actorName }) => {
  const [materialId, setMaterialId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [unitCode, setUnitCode] = useState("EA");
  const [requestedEndDate, setRequestedEndDate] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [releaseOrderId, setReleaseOrderId] = useState("");
  const [releasing, setReleasing] = useState(false);
  const [history, setHistory] = useState([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [sosOptions, setSosOptions] = useState([]);
  const [sosLoading, setSosLoading] = useState(false);
  const [sosChecked, setSosChecked] = useState(false);
  const [selectedSosKey, setSelectedSosKey] = useState("");
  const [siteAutoFilled, setSiteAutoFilled] = useState(false);
  const [unitCodeAutoFilled, setUnitCodeAutoFilled] = useState(false);
  const [materialUuid, setMaterialUuid] = useState(null);
  const [productSuggestions, setProductSuggestions] = useState([]);
  const [showProductSuggestions, setShowProductSuggestions] = useState(false);
  // Holistic (every site/warehouse, not just this order's site) BOM
  // Component Stock Status panel (Aug 2026, user's explicit ask) - shown
  // on-demand via the "Check Stock" button next to Create Production
  // Order (not automatic), splitting this card's space in half with the
  // form once open. Required/Shortfall are computed client-side from
  // `bom_qty_per_unit` x whatever Quantity is currently typed, so
  // adjusting Quantity updates the shortage live with no extra API call.
  const [showBomPanel, setShowBomPanel] = useState(false);
  const [bomStockStatus, setBomStockStatus] = useState(null);
  const [bomStockLoading, setBomStockLoading] = useState(false);
  // "Refresh Live SFG Stock" (Aug 2026, user's explicit ask): once a short
  // sub-assembly's own production order is confirmed, pull its updated
  // SFG warehouse stock right away instead of waiting for the scheduled
  // inventory_cache refresh, then retry order creation.
  const [refreshingSfgStock, setRefreshingSfgStock] = useState(false);
  const [refreshSfgElapsed, setRefreshSfgElapsed] = useState(0);
  const [refreshSfgStatus, setRefreshSfgStatus] = useState(null);
  // Every in-flight create-and-release job this browser session knows
  // about, tracked in the "Active Orders" table below - NOT tied to the
  // form, so submitting one order never blocks starting another while the
  // first is still running/paused in the background.
  // Lazily hydrated straight from localStorage during the initial render
  // (not via a post-mount effect) - fixes a real bug where React 18
  // StrictMode's dev-only double-invoke of effects on mount raced the old
  // "restore, then persist" effect pair and clobbered localStorage with
  // "[]" before the restored job ever reached state, silently wiping an
  // in-flight order's tracking (and its Stop button) on every refresh.
  const [activeJobs, setActiveJobs] = useState(() => {
    let stored = [];
    try { stored = JSON.parse(localStorage.getItem(ACTIVE_JOBS_STORAGE_KEY) || "[]"); } catch { stored = []; }
    return stored.map((s) => ({
      ...s, status: "running", elapsedSeconds: Math.round((Date.now() - s.startedAt) / 1000),
      storeRequest: null, deciding: false, expanded: false,
    }));
  });
  const productInputWrapperRef = useRef(null);
  const suggestionDebounceRef = useRef(null);
  const suggestionRequestRef = useRef(null);
  const pollingJobIdsRef = useRef(new Set());
  const [resumingJobId, setResumingJobId] = useState(null);

  const COMMON_UOM_CODES = ["EA", "KGM", "MTR", "LTR", "PC", "SET", "BOX", "TO"];

  useEffect(() => {
    if (suggestionDebounceRef.current) clearTimeout(suggestionDebounceRef.current);
    const q = materialId.trim();
    if (q.length < 2) {
      setProductSuggestions([]);
      return;
    }
    suggestionDebounceRef.current = setTimeout(async () => {
      const requestId = q;
      suggestionRequestRef.current = requestId;
      try {
        const { data } = await axios.get(`${API}/products/search`, { params: { q, limit: 10 } });
        if (suggestionRequestRef.current === requestId) setProductSuggestions(data);
      } catch {
        if (suggestionRequestRef.current === requestId) setProductSuggestions([]);
      }
    }, 250);
    return () => clearTimeout(suggestionDebounceRef.current);
  }, [materialId]);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (productInputWrapperRef.current && !productInputWrapperRef.current.contains(e.target)) {
        setShowProductSuggestions(false);
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  // selectedSosKey === "" means "nothing picked yet" - Number("") is 0,
  // which would silently resolve to sosOptions[0] and defeat the entire
  // point of forcing an explicit choice when there are 2+ options (a real
  // bug found by testing: a manually-typed Site was enough to submit an
  // order bound to option[0]'s Production Model with nothing ever picked).
  const selectedSosOption = selectedSosKey === "" ? null : sosOptions[Number(selectedSosKey)];

  const chooseSosOption = (key) => {
    setSelectedSosKey(key);
    const option = sosOptions[Number(key)];
    if (option) { setSiteId(option.site_id); setSiteAutoFilled(true); } // model determines site in SAP, not the other way around
    setBomStockStatus(null); // different model may mean a different real BOM - stale panel data would be misleading
  };

  const fetchBomStock = async (live) => {
    const product = materialId.trim();
    if (!product) return;
    setBomStockLoading(true);
    try {
      const { data } = await axios.get(`${API}/production-confirmation/bom-stock-status`, {
        params: {
          main_output_product: product,
          production_model_uuid: selectedSosOption ? selectedSosOption.production_model_uuid : undefined,
          live: !!live,
        },
      });
      setBomStockStatus(data);
    } catch {
      toast.error(live ? "Failed to check live BOM component stock - try again in a moment" : "Failed to load BOM component stock");
    } finally {
      setBomStockLoading(false);
    }
  };

  const openBomStockPanel = () => {
    setShowBomPanel(true);
    setBomStockSearch("");
    setBomStockShortageOnly(false);
    setExpandedBomComponents(new Set());
    if (!bomStockStatus) fetchBomStock(false);
  };

  // Flattened, ready-to-render rows (one per component-location, or one
  // placeholder row for a component with no stock anywhere) - shared by
  // Filterable/searchable/sortable, grouped-by-component list (Aug 2026,
  // user's explicit ask) - shared by both the modal's <table> and the
  // Excel/CSV export below so exporting always matches what's on screen.
  // "Not Assigned" is SAP's default/USABLE stock status, so per the
  // user's explicit ask it's hidden rather than shown as noise - only a
  // real exception status (Inspection, Blocked, etc.) is surfaced.
  const [bomStockSearch, setBomStockSearch] = useState("");
  const [bomStockShortageOnly, setBomStockShortageOnly] = useState(false);
  const [expandedBomComponents, setExpandedBomComponents] = useState(new Set());

  const toggleBomComponentExpand = (productId) => {
    setExpandedBomComponents((prev) => {
      const next = new Set(prev);
      if (next.has(productId)) next.delete(productId); else next.add(productId);
      return next;
    });
  };

  const bomStockComponents = useMemo(() => {
    if (!bomStockStatus?.checked) return [];
    const q = bomStockSearch.trim().toLowerCase();
    let list = bomStockStatus.components.map((c) => {
      const requiredQty = Math.round(c.bom_qty_per_unit * (Number(quantity) || 0) * 1e4) / 1e4;
      const shortfall = Math.max(0, Math.round((requiredQty - c.total_usable_qty) * 1e4) / 1e4);
      return { ...c, requiredQty, shortfall, sufficient: requiredQty <= 0 || shortfall <= 0 };
    });
    if (q) list = list.filter((c) => c.product_id.toLowerCase().includes(q) || (c.description || "").toLowerCase().includes(q));
    if (bomStockShortageOnly) list = list.filter((c) => !c.sufficient);
    return [...list].sort((a, b) => {
      if (a.sufficient !== b.sufficient) return a.sufficient ? 1 : -1; // shortages first
      return a.product_id.localeCompare(b.product_id);
    });
  }, [bomStockStatus, quantity, bomStockSearch, bomStockShortageOnly]);

  const downloadBomStockExcel = () => {
    if (bomStockComponents.length === 0) return;
    const rows = [["Component", "Description", "Available", "Required", "Shortfall", "Unit", "Site", "Warehouse", "Stock Status", "Location Qty"]];
    bomStockComponents.forEach((c) => {
      const locs = c.locations.length > 0 ? c.locations : [{ site: "", warehouse: "", stock_status: "", qty: "" }];
      locs.forEach((loc) => {
        const isDefaultStatus = (loc.stock_status || "").trim().toLowerCase() === "not assigned";
        rows.push([
          c.product_id, c.description || "", c.total_usable_qty, c.requiredQty, c.shortfall, c.unit_of_measure || "",
          loc.site || "", loc.warehouse || "", isDefaultStatus ? "" : (loc.stock_status || ""), loc.qty ?? "",
        ]);
      });
    });
    const sheet = XLSX.utils.aoa_to_sheet(rows);
    sheet["!cols"] = [{ wch: 16 }, { wch: 28 }, { wch: 12 }, { wch: 10 }, { wch: 10 }, { wch: 6 }, { wch: 24 }, { wch: 24 }, { wch: 14 }, { wch: 12 }];
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, sheet, "BOM Stock");
    XLSX.writeFile(workbook, `bom-stock-${materialId.trim() || "component"}-x${quantity || 0}.xlsx`);
  };

  const [lastCheckedId, setLastCheckedId] = useState(null);
  const [sosCheckFailed, setSosCheckFailed] = useState(false);

  const checkSourceOfSupply = async (idOverride) => {
    const id = (idOverride ?? materialId).trim();
    if (!id || (id === lastCheckedId && !sosCheckFailed)) return;  // avoid redundant re-lookups (e.g. blur right before a submit click) that would reset materialUuid/site mid-flow - unless the last attempt failed and this is a retry
    setMaterialId(id);
    setLastCheckedId(id);
    setShowProductSuggestions(false);
    setSosLoading(true);
    setSosCheckFailed(false);
    setSosOptions([]);
    setSelectedSosKey("");
    setMaterialUuid(null);
    try {
      const { data } = await axios.get(`${API}/production-confirmation/source-of-supply-options/${encodeURIComponent(id)}`);
      setMaterialUuid(data.material_uuid || null);
      const options = data.options || [];
      setSosOptions(options);
      // Only auto-pick when there's exactly ONE valid Model/Site combo -
      // no real ambiguity there. With 2+ valid options, force the user to
      // explicitly choose (previously defaulted to the first "Active" one
      // alphabetically, which could silently pick a model whose BOM has a
      // stock shortage while a perfectly fine alternative sat unused in
      // the dropdown - user's explicit call, no more auto-pick when there's
      // a real choice to make).
      if (options.length === 1) {
        setSelectedSosKey("0");
        setSiteId(options[0].site_id); // model determines site - always overwritten here, never an independent pre-filter
        setSiteAutoFilled(true);
      } else if (siteAutoFilled) {
        setSiteId(""); // clear a stale auto-filled site from a previous material - but never clobber a site the user typed themselves
        setSiteAutoFilled(false);
      }
      // Base UoM (SAP's real base unit for this material, exposed Aug
      // 2026) - lock the picker to it, same UX pattern as Site locking
      // from the chosen Production Model, so the previously-free UoM
      // choice can no longer silently mismatch SAP's own unit.
      if (data.base_uom) {
        setUnitCode(data.base_uom);
        setUnitCodeAutoFilled(true);
      } else if (unitCodeAutoFilled) {
        setUnitCode("EA");
        setUnitCodeAutoFilled(false);
      }
    } catch (e) {
      // A transient network/5xx error is NOT the same as "material doesn't
      // exist" - don't show the red not-recognized warning or block submit
      // for a real product just because one lookup attempt failed.
      setSosOptions([]);
      setSosCheckFailed(true);
    } finally {
      setSosLoading(false);
      setSosChecked(true);
    }
  };

  const loadHistory = useCallback(() => {
    setLoadingHistory(true);
    axios.get(`${API}/production-confirmation/proposal-history`).then(({ data }) => setHistory(data.entries)).catch(() => toast.error("Failed to load Proposal/Release history")).finally(() => setLoadingHistory(false));
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const removeActiveJob = (jobId) => setActiveJobs((prev) => prev.filter((j) => j.job_id !== jobId));
  const toggleJobExpand = (jobId) => setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, expanded: !j.expanded } : j)));

  // "Resume" action (Aug 27 2026, user's explicit ask) on a failed order
  // job that already has a known SAP Proposal ID - hands that ID + the
  // original form snapshot back to the backend, which resumes the
  // Proposal -> Order -> Release pipeline under a fresh job_id instead of
  // requiring a trip to the SAP UI to finish it by hand.
  const resumeFailedJob = async (job) => {
    setResumingJobId(job.job_id);
    try {
      const { data } = await axios.post(`${API}/production-confirmation/create-and-release-order/${job.job_id}/resume`, {
        actor: actorName.trim(),
      });
      removeActiveJob(job.job_id);
      setActiveJobs((prev) => [...prev, {
        job_id: data.job_id, material_id: job.material_id, site_id: job.site_id,
        quantity: job.quantity, unit_code: job.unit_code, status: "waiting_for_order",
        startedAt: Date.now(), elapsedSeconds: 0,
      }]);
      pollJob(data.job_id);
      toast.success(`Resuming from Proposal ${data.production_proposal_id} - no need to use the SAP UI`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to resume order creation");
    } finally {
      setResumingJobId(null);
    }
  };

  // Runs entirely independently per job - multiple can be in flight at
  // once, each polling its own status on its own timer, none of them
  // blocking the form above from starting yet another order.
  const pollJob = useCallback((jobId) => {
    if (pollingJobIdsRef.current.has(jobId)) return;
    pollingJobIdsRef.current.add(jobId);
    const tickTimer = setInterval(() => {
      setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, elapsedSeconds: Math.round((Date.now() - j.startedAt) / 1000) } : j)));
    }, 1000);
    let lastStoreReqKey = null;
    (async () => {
      try {
        // eslint-disable-next-line no-constant-condition
        while (true) {
          await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
          let job;
          try {
            ({ data: job } = await axios.get(`${API}/production-confirmation/create-and-release-order/status/${jobId}`));
          } catch (e) {
            if (e.response?.status === 404) {
              // Real bug reproduced live (Aug 2026): a genuinely-gone job
              // (e.g. a stale localStorage entry from a much older
              // session whose Mongo doc no longer exists) used to be
              // treated the same as a transient network hiccup below,
              // so this loop retried FOREVER and the row's spinner never
              // stopped, no matter what the user clicked. A 404 here is
              // permanent, not transient - stop tracking it for good.
              toast.error(`Lost track of this order (job no longer exists) - it may already be done; check History`);
              removeActiveJob(jobId);
              break;
            }
            continue; // transient network hiccup - keep tracking this job, try again next round
          }
          if (PAUSED_STATUSES.includes(job.status)) {
            const key = `${job.store_request_id}:${job.status}`;
            if (job.store_request_id && lastStoreReqKey !== key) {
              try {
                const { data: reqDoc } = await axios.get(`${API}/production-confirmation/store-requests/by-job/${jobId}`);
                lastStoreReqKey = key;
                setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, status: job.status, storeRequest: reqDoc } : j)));
              } catch {
                setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, status: job.status } : j)));
              }
            } else {
              setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, status: job.status } : j)));
            }
            continue;
          }
          setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, status: job.status } : j)));
          if (job.status === "done") {
            const result = job.result;
            if (result.production_order_id && result.released) {
              toast.success(`Production Order ${result.production_order_id} created and released in SAP - ready for confirmation`);
            } else if (result.production_order_id) {
              toast.error(`Order ${result.production_order_id} was created but Release failed - use the fallback form to retry`);
            } else {
              toast.error(result.note || "Proposal created but the Order hasn't appeared yet - it keeps retrying automatically, check history shortly");
            }
            removeActiveJob(jobId);
            loadHistory();
            break;
          }
          if (job.status === "cancelled") {
            toast.error(job.result?.note || "Order creation was cancelled after the store approval decision");
            removeActiveJob(jobId);
            loadHistory();
            break;
          }
          if (job.status === "failed") {
            // SFG-shortage block (Aug 2026, user's explicit ask): keep this
            // row visible with a persistent, dismissible detail table
            // instead of a toast that vanishes in a few seconds - a real
            // "which component, how short" needs to stay on screen until
            // the planner has acted on it.
            if (job.result?.reason === "sfg_shortage") {
              setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, status: "failed", failure: job.result, error: job.error } : j)));
              break;
            }
            // Aug 27 2026 fix (user's explicit ask - "if an error happens
            // during production order creation ... it would be ideal to
            // have a recovery on app frontend"): any OTHER mid-pipeline
            // failure used to just toast + vanish, forcing a trip to the
            // SAP UI to find and finish a Proposal that may already
            // exist there. Now it stays on screen with the real reason
            // and a "Resume" action when a Proposal ID is known.
            setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, status: "failed", failure: job.result, error: job.error } : j)));
            break;
          }
        }
      } finally {
        clearInterval(tickTimer);
        pollingJobIdsRef.current.delete(jobId);
      }
    })();
  }, [loadHistory]);

  // Persist the (small) seed info for every active job so a page refresh
  // doesn't lose track of orders still running in the background. Safe to
  // run on every render including the very first (it just re-writes back
  // the same data `activeJobs` was hydrated from above) - no skip-guard
  // needed now that hydration itself happens synchronously in useState.
  useEffect(() => {
    const seed = activeJobs.map(({ job_id, material_id, site_id, quantity, unit_code, startedAt }) => ({ job_id, material_id, site_id, quantity, unit_code, startedAt }));
    localStorage.setItem(ACTIVE_JOBS_STORAGE_KEY, JSON.stringify(seed));
  }, [activeJobs]);

  // Kick off polling once for whatever was hydrated above - pollJob's own
  // pollingJobIdsRef guard makes this safe even under StrictMode's dev
  // double-invoke of mount effects.
  useEffect(() => {
    activeJobs.forEach((j) => pollJob(j.job_id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!refreshingSfgStock) {
      setRefreshSfgElapsed(0);
      return;
    }
    const interval = setInterval(() => setRefreshSfgElapsed((s) => s + 1), 1000);
    return () => clearInterval(interval);
  }, [refreshingSfgStock]);

  const refreshLiveSfgStock = async (siteOverride) => {
    const site = (siteOverride || siteId).trim();
    if (!site) {
      toast.error("Enter a Site first");
      return;
    }
    setRefreshingSfgStock(true);
    setRefreshSfgStatus(`Pulling live SFG stock for ${site} from SAP...`);
    try {
      const { data } = await axios.post(`${API}/production-confirmation/refresh-live-sfg-stock`, null, { params: { site_id: site } });
      let job = null;
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const res = await axios.get(`${API}/production-confirmation/refresh-live-sfg-stock/${data.job_id}`);
        job = res.data;
        if (job.status === "done" || job.status === "failed") break;
      }
      if (job?.status === "done") {
        toast.success(`Live SFG stock refreshed for ${site} - retry creating the order now`);
      } else {
        toast.error(job?.error || "Failed to refresh live SFG stock");
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || "Failed to refresh live SFG stock");
    } finally {
      setRefreshingSfgStock(false);
      setRefreshSfgStatus(null);
    }
  };

  const createProposal = async () => {
    if (sosOptions.length > 1 && !selectedSosOption) {
      toast.error("This material has multiple valid Production Models - pick one from the Source of Supply list before creating the order");
      return;
    }
    if (!materialId.trim() || !siteId.trim() || !quantity) {
      toast.error("Product, Site and Quantity are required");
      return;
    }
    if (sosLoading) {
      toast.error("Still checking this Product ID with SAP - wait a moment and try again");
      return;
    }
    if (sosChecked && !materialUuid && !sosCheckFailed) {
      toast.error("Product ID not recognized in SAP - pick one from the suggestions or check the spelling");
      return;
    }
    setSubmitting(true);
    try {
      const payloadMaterialId = materialId.trim();
      const payloadSiteId = siteId.trim().toUpperCase();
      const payloadQuantity = Number(quantity);
      const payloadUnitCode = unitCode.trim().toUpperCase() || "EA";
      const { data } = await axios.post(`${API}/production-confirmation/create-and-release-order`, {
        material_id: payloadMaterialId,
        site_id: payloadSiteId,
        quantity: payloadQuantity,
        unit_code: payloadUnitCode,
        availability_datetime: requestedEndDate ? new Date(requestedEndDate).toISOString() : null,
        actor: actorName.trim(),
        logistic_relationship_uuid: selectedSosOption ? selectedSosOption.logistic_relationship_uuid : null,
        production_model_uuid: selectedSosOption ? selectedSosOption.production_model_uuid : null,
        production_model_id: selectedSosOption ? selectedSosOption.production_model_id : null,
      });
      const jobId = data.job_id;
      setActiveJobs((prev) => [{
        job_id: jobId, material_id: payloadMaterialId, site_id: payloadSiteId, quantity: payloadQuantity, unit_code: payloadUnitCode,
        status: "checking_stock", startedAt: Date.now(), elapsedSeconds: 0, storeRequest: null, deciding: false, expanded: false,
      }, ...prev]);
      pollJob(jobId);
      toast.success("Order request submitted - track its progress in the Active Orders table below.");
      // Free up the form immediately so another order can be submitted
      // right away - it no longer waits for this one to finish/pause.
      setMaterialId(""); setQuantity("1"); setRequestedEndDate(""); setSiteId("");
      setSosOptions([]); setSosChecked(false); setSelectedSosKey(""); setSiteAutoFilled(false);
      setUnitCode("EA"); setUnitCodeAutoFilled(false); setMaterialUuid(null); setLastCheckedId(null);
      setShowBomPanel(false); setBomStockStatus(null);
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || "Failed to create Production Order in SAP");
    } finally {
      setSubmitting(false);
    }
  };

  const decideStoreRequest = async (jobId, requestId, decision) => {
    if (!requestId) return;
    setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, deciding: true } : j)));
    try {
      await axios.post(`${API}/production-confirmation/store-requests/${requestId}/decision`, {
        decision, actor: actorName.trim(),
      });
      toast.success(decision === "approve" ? "Approved - resuming automated order creation" : "Rejected - order creation cancelled");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to record your decision");
    } finally {
      setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, deciding: false } : j)));
    }
  };

  // "Stop the rotating wheel" (Aug 2026): stops THIS app's own polling for
  // the resulting Order - it can never delete/cancel the SAP Proposal
  // already created (no such SAP API exists), so that stays exactly as
  // it is, un-converted, in SAP. pollJob's existing "cancelled" handling
  // toasts + removes the row once the backend confirms the stop.
  const stopTrackingJob = async (jobId) => {
    setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, stopping: true } : j)));
    try {
      await axios.post(`${API}/production-confirmation/create-and-release-order/${jobId}/cancel`);
      // The backend only checks this flag at specific checkpoints (stock
      // check, proposal creation, the order/release poll loop) - give it
      // a reasonable window to land before re-enabling the button, so a
      // Stop clicked at an awkward moment doesn't leave "Stopping..."
      // disabled forever with no feedback (pollJob's own "cancelled"
      // handling removes the row well before this if it lands sooner).
      setTimeout(() => {
        setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId && j.stopping ? { ...j, stopping: false } : j)));
      }, 30000);
    } catch (e) {
      if (e.response?.status === 404) {
        // Same stale-job case as pollJob's 404 handling above - nothing
        // to actually stop, so just drop it from the list instead of
        // leaving the row spinning forever on a job that's already gone.
        toast.error("This order no longer exists - removing it from the list");
        removeActiveJob(jobId);
        return;
      }
      toast.error(e.response?.data?.detail || "Failed to stop this order run");
      setActiveJobs((prev) => prev.map((j) => (j.job_id === jobId ? { ...j, stopping: false } : j)));
    }
  };

  const releaseOrder = async () => {
    if (!releaseOrderId.trim()) {
      toast.error("Enter the Production Order ID to release");
      return;
    }
    setReleasing(true);
    try {
      await axios.post(`${API}/production-confirmation/release-order`, {
        production_order_id: releaseOrderId.trim(),
        actor: actorName.trim(),
      });
      toast.success(`Production Order ${releaseOrderId.trim()} released in SAP`);
      setReleaseOrderId("");
      loadHistory();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to release Production Order in SAP");
    } finally {
      setReleasing(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3" data-testid="create-proposal-card">
          <h3 className="font-heading text-sm font-bold text-[#1D2939] uppercase tracking-wide">Create Production Order</h3>
          <p className="text-xs text-[#667085]">Creates and releases a Production Order in SAP - fully automated, retries in the background until SAP converts it. Checks component stock first; submitting an order tracks it in the Active Orders table below so this form stays free to submit another right away.</p>
          <div ref={productInputWrapperRef} className="relative">
            <Label className="text-xs font-bold text-[#344054]">Product ID</Label>
            <Input
              value={materialId}
              onChange={(e) => { setMaterialId(e.target.value); setSosChecked(false); setSosOptions([]); setMaterialUuid(null); setLastCheckedId(null); setShowProductSuggestions(true); setBomStockStatus(null); }}
              onFocus={() => setShowProductSuggestions(true)}
              onBlur={() => checkSourceOfSupply()}
              placeholder="e.g. MAZ42117272-TA"
              data-testid="create-proposal-product-input"
              autoComplete="off"
            />
            {showProductSuggestions && productSuggestions.length > 0 && (
              <div className="absolute z-10 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-md max-h-56 overflow-y-auto" data-testid="product-suggestions-dropdown">
                {productSuggestions.map((s) => (
                  <button
                    key={s.product_id}
                    type="button"
                    className="w-full text-left px-3 py-2 text-sm hover:bg-[#F9FAFB] border-b border-[#EAECF0] last:border-0"
                    onMouseDown={(e) => { e.preventDefault(); checkSourceOfSupply(s.product_id); }}
                    data-testid={`product-suggestion-${s.product_id}`}
                  >
                    <div className="font-medium text-[#1D2939]">{s.product_id}</div>
                    {s.description && <div className="text-xs text-[#667085]">{s.description}</div>}
                  </button>
                ))}
              </div>
            )}
            {sosChecked && !sosLoading && !materialUuid && !sosCheckFailed && (
              <p className="text-xs text-[#B42318] mt-1" data-testid="product-not-recognized-warning">
                Product ID not recognized in SAP - pick a suggestion above or check the spelling.
              </p>
            )}
            {sosCheckFailed && (
              <p className="text-xs text-[#B54708] mt-1" data-testid="product-lookup-failed-warning">
                Could not verify this Product ID with SAP just now (network hiccup) - you can still proceed, or blur the field again to retry.
              </p>
            )}
          </div>
          <div className="min-h-[26px]">
            {sosLoading && <p className="text-xs text-[#667085]" data-testid="sos-loading-text">Checking available Production Models...</p>}
            {sosChecked && sosOptions.length > 0 && (
              <div data-testid="source-of-supply-picker">
                <Label className="text-xs font-bold text-[#344054]">Source of Supply (Production Model)</Label>
                <Select value={selectedSosKey} onValueChange={chooseSosOption}>
                  <SelectTrigger data-testid="source-of-supply-select-trigger">
                    <SelectValue placeholder="Choose a Production Model" />
                  </SelectTrigger>
                  <SelectContent>
                    {sosOptions.map((o, idx) => (
                      <SelectItem key={`${o.production_model_id}-${o.site_id}`} value={String(idx)} data-testid={`source-of-supply-option-${o.production_model_id}-${o.site_id}`}>
                        {o.production_model_id}{o.description ? ` — ${o.description}` : ""} (Site {o.site_id}){o.is_active ? "" : " (Obsolete)"}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p
                  className={`text-xs mt-1 ${sosOptions.length > 1 && !selectedSosOption ? "font-bold text-[#B54708]" : "text-[#667085]"}`}
                  data-testid={sosOptions.length > 1 && !selectedSosOption ? "sos-choice-required-warning" : undefined}
                >
                  {sosOptions.length > 1
                    ? selectedSosOption
                      ? `Using ${selectedSosOption.production_model_id} (Site ${selectedSosOption.site_id}).`
                      : `This material has ${sosOptions.length} valid Production Model/Site combinations - you must pick one before creating the order (it sets the Site for you; not auto-picked since one option may be short on stock while another isn't).`
                    : "Picking this confirms the Production Model - and its Site - SAP will use."}
                </p>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs font-bold text-[#344054]">Site</Label>
              <Input
                value={siteId}
                onChange={(e) => { setSiteId(e.target.value.toUpperCase()); setSiteAutoFilled(false); }}
                placeholder="e.g. P2"
                disabled={siteAutoFilled}
                data-testid="create-proposal-site-input"
              />
              {siteAutoFilled && <p className="text-[11px] text-[#667085] mt-0.5">Locked - set by the chosen Production Model above</p>}
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={refreshingSfgStock || !siteId.trim()}
                onClick={() => refreshLiveSfgStock()}
                className="mt-1.5 h-7 text-[11px]"
                data-testid="refresh-live-sfg-stock-button"
              >
                <ArrowClockwise size={12} className={`mr-1 ${refreshingSfgStock ? "animate-spin" : ""}`} />
                {refreshingSfgStock ? `Refreshing (${refreshSfgElapsed}s)...` : "Refresh Live SFG Stock"}
              </Button>
              {refreshingSfgStock && refreshSfgStatus && (
                <p className="text-[11px] text-[#667085] mt-0.5" data-testid="refresh-live-sfg-stock-status">{refreshSfgStatus}</p>
              )}
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">UoM</Label>
              <Select value={unitCode} onValueChange={setUnitCode} disabled={unitCodeAutoFilled}>
                <SelectTrigger data-testid="create-proposal-uom-select-trigger">
                  <SelectValue placeholder="EA" />
                </SelectTrigger>
                <SelectContent>
                  {(COMMON_UOM_CODES.includes(unitCode) ? COMMON_UOM_CODES : [unitCode, ...COMMON_UOM_CODES]).map((code) => (
                    <SelectItem key={code} value={code} data-testid={`create-proposal-uom-option-${code}`}>{code}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {unitCodeAutoFilled ? (
                <p className="text-[11px] text-[#667085] mt-0.5" data-testid="uom-locked-helper">Locked - SAP's base unit for this material</p>
              ) : unitCode !== "EA" && (
                <p className="text-[11px] text-[#B54708] mt-0.5" data-testid="uom-not-ea-warning">
                  Not verified against SAP's base unit for this material yet - double-check it's correct before submitting.
                </p>
              )}
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <Label className="text-xs font-bold text-[#344054]">Quantity</Label>
              <Input type="number" value={quantity} onChange={(e) => setQuantity(e.target.value)} data-testid="create-proposal-qty-input" />
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Requested End Date</Label>
              <Input type="date" value={requestedEndDate} onChange={(e) => setRequestedEndDate(e.target.value)} data-testid="create-proposal-date-input" />
            </div>
          </div>
          <div className="flex gap-2">
            <Button onClick={createProposal} disabled={submitting} className="flex-1" data-testid="create-proposal-submit-button">
              {submitting ? "Submitting..." : "Create Production Order"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!materialId.trim()}
              onClick={openBomStockPanel}
              data-testid="toggle-bom-stock-panel-button"
            >
              Check Stock
            </Button>
          </div>
        </div>

        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3" data-testid="release-order-card">
          <h3 className="font-heading text-sm font-bold text-[#1D2939] uppercase tracking-wide">Fallback: Release Order Manually</h3>
          <p className="text-xs text-[#667085]">Only needed if the automatic flow above times out. Enter a Production Order ID (once visible in SAP) to release it.</p>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Production Order ID</Label>
            <Input value={releaseOrderId} onChange={(e) => setReleaseOrderId(e.target.value)} placeholder="e.g. 69843" data-testid="release-order-id-input" />
          </div>
          <Button onClick={releaseOrder} disabled={releasing} className="w-full" data-testid="release-order-submit-button">
            {releasing ? "Releasing in SAP..." : "Release Production Order"}
          </Button>
        </div>
      </div>

      <Dialog open={showBomPanel} onOpenChange={(open) => setShowBomPanel(open)}>
        <DialogContent className="max-w-4xl max-h-[85vh] overflow-hidden flex flex-col" data-testid="bom-stock-status-panel">
          <DialogHeader>
            <DialogTitle>BOM Component Stock (x{quantity || 0} {formatUnit(unitCode)})</DialogTitle>
            <DialogDescription>
              {materialId.trim()} - every site/warehouse this BOM's components sit in, scaled to the Quantity you entered.
            </DialogDescription>
          </DialogHeader>
          <div className="flex items-center justify-between gap-2 flex-wrap">
            {bomStockStatus && (
              <span className="text-xs text-[#667085]" data-testid="bom-stock-status-source">
                Source: {bomStockStatus.source === "live" ? "Live SAP" : "Cached snapshot"}{bomStockStatus.fetched_at ? ` · as of ${new Date(bomStockStatus.fetched_at).toLocaleString("en-IN")}` : ""}
              </span>
            )}
            <div className="flex gap-2 ml-auto">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={bomStockLoading || !materialId.trim()}
                onClick={() => fetchBomStock(true)}
                data-testid="bom-stock-status-refresh-button"
              >
                <ArrowClockwise size={12} className={`mr-1.5 ${bomStockLoading ? "animate-spin" : ""}`} />
                {bomStockLoading ? "Checking..." : "Check Live Stock"}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={bomStockComponents.length === 0}
                onClick={downloadBomStockExcel}
                data-testid="bom-stock-status-download-button"
              >
                Download Excel
              </Button>
            </div>
          </div>

          {bomStockLoading && !bomStockStatus && (
            <p className="text-sm text-[#667085]" data-testid="bom-stock-status-loading">Checking BOM component stock...</p>
          )}

          {bomStockStatus?.checked && (
            <>
              <div className="flex items-center gap-3 flex-wrap">
                <div className="relative flex-1 min-w-[200px]">
                  <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                  <Input
                    value={bomStockSearch}
                    onChange={(e) => setBomStockSearch(e.target.value)}
                    placeholder="Search by component ID or description..."
                    className="h-8 pl-7 text-xs"
                    data-testid="bom-stock-search-input"
                  />
                </div>
                <label className="flex items-center gap-1.5 text-xs font-medium text-[#344054] cursor-pointer whitespace-nowrap">
                  <input
                    type="checkbox"
                    checked={bomStockShortageOnly}
                    onChange={(e) => setBomStockShortageOnly(e.target.checked)}
                    data-testid="bom-stock-shortage-only-toggle"
                  />
                  Shortages only
                </label>
              </div>

              <div className="flex-1 overflow-auto border border-[#EAECF0] rounded-sm">
                <table className="w-full text-xs border-collapse" data-testid="bom-stock-status-table">
                  <thead className="sticky top-0">
                    <tr>
                      {["Component", "Description", "Available", "Required", "Shortfall", "Site", "Warehouse", "Stock Status", "Location Qty"].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {bomStockComponents.map((c) => {
                      const expanded = expandedBomComponents.has(c.product_id);
                      const hasLocations = c.locations.length > 0;
                      return (
                        <Fragment key={c.product_id}>
                          <tr className={c.sufficient ? "bg-white" : "bg-[#FEF3F2]"} data-testid={`bom-stock-component-row-${c.product_id}`}>
                            <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">
                              <button
                                type="button"
                                className="flex items-center gap-1 disabled:cursor-default"
                                disabled={!hasLocations}
                                onClick={() => toggleBomComponentExpand(c.product_id)}
                                data-testid={`bom-stock-expand-${c.product_id}`}
                              >
                                {hasLocations ? (expanded ? <CaretDown size={11} className="shrink-0" /> : <CaretRight size={11} className="shrink-0" />) : <span className="inline-block w-[11px]" />}
                                {c.product_id}
                              </button>
                            </td>
                            <td className="border border-[#D0D5DD] px-2 py-1.5">{c.description || "—"}</td>
                            <td className={`border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums ${c.sufficient ? "text-[#027A48]" : "text-[#B42318] font-bold"}`}>
                              {formatQty(c.total_usable_qty)} {formatUnit(c.unit_of_measure)}
                            </td>
                            <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(c.requiredQty)} {formatUnit(c.unit_of_measure)}</td>
                            <td className={`border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums ${!c.sufficient ? "text-[#B42318] font-bold" : ""}`}>
                              {c.sufficient ? "—" : `${formatQty(c.shortfall)} ${formatUnit(c.unit_of_measure)}`}
                            </td>
                            <td colSpan={4} className="border border-[#D0D5DD] px-2 py-1.5 text-[#667085]">
                              {hasLocations ? `${c.locations.length} location${c.locations.length > 1 ? "s" : ""} - click to ${expanded ? "collapse" : "expand"}` : "No stock found anywhere"}
                            </td>
                          </tr>
                          {expanded && c.locations.map((loc, li) => {
                            const isDefaultStatus = (loc.stock_status || "").trim().toLowerCase() === "not assigned";
                            return (
                              <tr key={li} className="bg-[#F9FAFB]" data-testid={`bom-stock-location-row-${c.product_id}-${li}`}>
                                <td className="border border-[#D0D5DD] px-2 py-1.5" colSpan={5}></td>
                                <td className="border border-[#D0D5DD] px-2 py-1.5">{loc.site || "—"}</td>
                                <td className="border border-[#D0D5DD] px-2 py-1.5">{loc.warehouse || "—"}</td>
                                <td className="border border-[#D0D5DD] px-2 py-1.5">{isDefaultStatus ? "—" : (loc.stock_status || "—")}</td>
                                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(loc.qty)} {formatUnit(c.unit_of_measure)}</td>
                              </tr>
                            );
                          })}
                        </Fragment>
                      );
                    })}
                    {bomStockComponents.length === 0 && (
                      <tr><td colSpan={9} className="text-center py-6 text-[#98A2B3] border border-[#D0D5DD]">
                        {bomStockSearch.trim() || bomStockShortageOnly ? "No components match your search/filter." : "No active components in this BOM."}
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {bomStockStatus && !bomStockStatus.checked && (
            <p className="text-sm text-[#98A2B3]" data-testid="bom-stock-status-unavailable">{bomStockStatus.reason}</p>
          )}
        </DialogContent>
      </Dialog>

      <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto" data-testid="active-orders-card">
        <div className="px-3 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB]">
          <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">Active Orders ({activeJobs.length})</h3>
        </div>
        <table className="w-full text-[12px] border-collapse" data-testid="active-orders-table">
          <thead>
            <tr>
              {["Started", "Material", "Site", "Qty", "Status", "Actions"].map((h) => (
                <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {activeJobs.map((j, i) => {
              const isPaused = PAUSED_STATUSES.includes(j.status);
              return (
                <Fragment key={j.job_id}>
                  <tr className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`active-order-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{new Date(j.startedAt).toLocaleTimeString("en-IN")}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{j.material_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{j.site_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(j.quantity)} {formatUnit(j.unit_code)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5" data-testid={`active-order-status-${i}`}>
                      {isPaused ? (
                        <>
                          <Badge variant="outline" className={`border ${
                            j.status === "partial_pending_planner" ? "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]"
                            : "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]"
                          }`}>
                            {PHASE_LABELS[j.status] || j.status}
                          </Badge>
                        </>
                      ) : j.status === "failed" ? (
                        <Badge variant="outline" className="border bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">
                          {j.failure?.reason === "sfg_shortage" ? "SFG Shortage - Blocked" : "Failed"}
                        </Badge>
                      ) : (
                        <OrderStepTracker status={j.status} elapsedSeconds={j.elapsedSeconds} />
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      {j.status === "partial_pending_planner" ? (
                        <div className="flex flex-wrap gap-1.5">
                          <Button size="sm" variant="outline" onClick={() => toggleJobExpand(j.job_id)} data-testid={`active-order-view-button-${i}`}>{j.expanded ? "Hide" : "View"}</Button>
                          <Button size="sm" disabled={j.deciding} onClick={() => decideStoreRequest(j.job_id, j.storeRequest?._id, "approve")} data-testid={`store-approval-approve-button-${i}`}>Confirm</Button>
                          <Button size="sm" variant="outline" disabled={j.deciding} onClick={() => decideStoreRequest(j.job_id, j.storeRequest?._id, "reject")} data-testid={`store-approval-reject-button-${i}`}>Delete</Button>
                        </div>
                      ) : j.status === "waiting_store_approval" ? (
                        j.storeRequest ? (
                          <Button size="sm" variant="outline" onClick={() => toggleJobExpand(j.job_id)} data-testid={`active-order-view-button-${i}`}>{j.expanded ? "Hide" : "View"}</Button>
                        ) : <span className="text-[11px] text-[#98A2B3]">Loading...</span>
                      ) : j.status === "failed" ? (
                        <div className="flex flex-wrap gap-1.5">
                          {j.failure?.reason !== "sfg_shortage" && j.failure?.production_proposal_id && (
                            <Button
                              size="sm"
                              disabled={resumingJobId === j.job_id}
                              onClick={() => resumeFailedJob(j)}
                              data-testid={`active-order-resume-button-${i}`}
                            >
                              {resumingJobId === j.job_id ? "Resuming..." : "Resume"}
                            </Button>
                          )}
                          <Button size="sm" variant="outline" onClick={() => removeActiveJob(j.job_id)} data-testid={`active-order-dismiss-button-${i}`}>Dismiss</Button>
                        </div>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={j.stopping}
                          onClick={() => stopTrackingJob(j.job_id)}
                          title="Stops this app's own tracking only - if a SAP Proposal was already created, it stays in SAP un-converted, it is not deleted"
                          data-testid={`active-order-stop-button-${i}`}
                        >
                          {j.stopping ? "Stopping..." : "Stop"}
                        </Button>
                      )}
                    </td>
                  </tr>
                  {j.status === "failed" && j.failure?.reason === "sfg_shortage" && (
                    <tr data-testid={`active-order-sfg-shortage-${i}`}>
                      <td colSpan={6} className="border border-[#D0D5DD] px-2 py-2 bg-[#FEF3F2]">
                        <p className="text-[11px] text-[#B42318] mb-1.5">
                          Blocked before anything was created in SAP - {j.failure.short_components.length} sub-assembly component(s) short at {j.failure.site_id}.
                          These are produced in-house, not stocked by the Store.
                        </p>
                        <table className="w-full text-[11px] border-collapse bg-white">
                          <thead>
                            <tr>
                              {["Component", "Required by Production", `Available in ${j.failure.site_id}-SFG`, "Short By"].map((h) => (
                                <th key={h} className="border border-[#FECDCA] px-2 py-1 text-left font-bold text-[#B42318]">{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {j.failure.short_components.map((c) => (
                              <tr key={c.product_id} data-testid={`active-order-sfg-shortage-row-${i}-${c.product_id}`}>
                                <td className="border border-[#FECDCA] px-2 py-1">{c.product_id}{c.description ? ` - ${c.description}` : ""}</td>
                                <td className="border border-[#FECDCA] px-2 py-1 text-right tabular-nums">{formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}</td>
                                <td className="border border-[#FECDCA] px-2 py-1 text-right tabular-nums">{c.available_qty == null ? "unknown" : `${formatQty(c.available_qty)} ${formatUnit(c.unit_of_measure)}`}</td>
                                <td className="border border-[#FECDCA] px-2 py-1 text-right tabular-nums font-bold text-[#B42318]">{formatQty(Math.max(0, c.required_qty - (c.available_qty || 0)))} {formatUnit(c.unit_of_measure)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        <p className="text-[11px] text-[#93370D] mt-1.5">
                          Create/confirm a production order for these sub-assemblies first, then:
                        </p>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={refreshingSfgStock}
                          onClick={() => refreshLiveSfgStock(j.failure.site_id)}
                          className="mt-1 h-7 text-[11px]"
                          data-testid={`active-order-sfg-refresh-button-${i}`}
                        >
                          <ArrowClockwise size={12} className={`mr-1 ${refreshingSfgStock ? "animate-spin" : ""}`} />
                          {refreshingSfgStock ? `Refreshing (${refreshSfgElapsed}s)...` : "Refresh Live SFG Stock & Retry"}
                        </Button>
                      </td>
                    </tr>
                  )}
                  {j.status === "failed" && j.failure && j.failure.reason !== "sfg_shortage" && (
                    <tr data-testid={`active-order-pipeline-error-${i}`}>
                      <td colSpan={6} className="border border-[#D0D5DD] px-2 py-2 bg-[#FEF3F2]">
                        <p className="text-[11px] text-[#B42318] mb-1" data-testid={`active-order-pipeline-error-reason-${i}`}>
                          <strong>Why this failed:</strong> {j.error || "SAP reported an unexpected error"}
                        </p>
                        {j.failure.production_proposal_id ? (
                          <p className="text-[11px] text-[#93370D]">
                            Proposal <strong>{j.failure.production_proposal_id}</strong>
                            {j.failure.production_order_id ? <> and Order <strong>{j.failure.production_order_id}</strong></> : null} already exist in SAP -
                            click <strong>Resume</strong> to let the app pick up from here automatically (no need to open the SAP UI).
                          </p>
                        ) : (
                          <p className="text-[11px] text-[#93370D]">
                            Nothing was created in SAP yet before this failed - fix the issue above and create a new order.
                          </p>
                        )}
                      </td>
                    </tr>
                  )}
                  {j.expanded && j.storeRequest && (
                    <tr data-testid={`active-order-details-${i}`}>
                      <td colSpan={6} className="border border-[#D0D5DD] px-2 py-2 bg-[#FFFAEB]">
                        <p className="text-[11px] text-[#93370D] mb-1.5">
                          Proposal <strong>{j.storeRequest.production_proposal_id}</strong> was created in SAP - {j.storeRequest.components.length} component(s) short at {j.storeRequest.site_id}.
                        </p>
                        <table className="w-full text-[11px] border-collapse bg-white">
                          <thead>
                            <tr>
                              {["Component", "Required by Production", `In Stock at Site ${j.storeRequest.site_id} (By Warehouse)`, ...(j.status === "partial_pending_planner" ? ["Issued", "Shortfall"] : [])].map((h) => (
                                <th key={h} className="border border-[#FEDF89] px-2 py-1 text-left font-bold text-[#93370D]">{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {j.storeRequest.components.map((c) => (
                              <tr key={c.product_id}>
                                <td className="border border-[#FEDF89] px-2 py-1 align-top">{c.product_id}{c.description ? ` - ${c.description}` : ""}</td>
                                <td className="border border-[#FEDF89] px-2 py-1 text-right tabular-nums align-top">{formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}</td>
                                <td className="border border-[#FEDF89] px-2 py-1 text-right tabular-nums align-top">
                                  {(c.locations && c.locations.length > 0) ? c.locations.map((loc, li) => {
                                    const restricted = isLocationRestricted(loc);
                                    return (
                                      <div key={li} className={restricted ? "text-[#B54708] font-bold" : ""}>
                                        {loc.warehouse || (loc.site ? loc.site.split("-").pop() : "Unknown Warehouse")}{loc.stock_status ? ` (${loc.stock_status})` : ""}{restricted ? " \u26A0 On Hold" : ""}: {formatQty(loc.qty)} {formatUnit(c.unit_of_measure)}
                                      </div>
                                    );
                                  }) : "no stock at this site"}
                                </td>
                                {j.status === "partial_pending_planner" && (
                                  <>
                                    <td className="border border-[#FEDF89] px-2 py-1 text-right tabular-nums align-top">{formatQty(c.issued_qty)} {formatUnit(c.unit_of_measure)}</td>
                                    <td className="border border-[#FEDF89] px-2 py-1 text-right tabular-nums font-bold text-[#B42318] align-top">{formatQty(c.shortfall)} {formatUnit(c.unit_of_measure)}</td>
                                  </>
                                )}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {j.status === "waiting_store_approval" && (
                          <p className="text-[11px] text-[#93370D] mt-1.5">
                            Share this Request ID with the store team on the Store Approval screen (<code>/storeapproval</code>): <strong data-testid={`store-approval-request-id-${i}`}>{j.storeRequest._id}</strong>
                          </p>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {activeJobs.length === 0 && (
              <tr><td colSpan={6} className="text-center py-6 text-[#98A2B3] border border-[#D0D5DD]" data-testid="active-orders-empty-state">No orders in progress - anything you create above will show up here.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto">
        <table className="w-full text-[12px] border-collapse" data-testid="proposal-history-table">
          <thead>
            <tr>
              {["When", "By", "Action", "Product/Order", "Production Model", "Site", "Qty", "Result"].map((h) => (
                <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {history.map((h, i) => (
              <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`proposal-history-row-${i}`}>
                <td className="border border-[#D0D5DD] px-2 py-1">{new Date(h.at).toLocaleString("en-IN")}</td>
                <td className="border border-[#D0D5DD] px-2 py-1">{h.actor}</td>
                <td className="border border-[#D0D5DD] px-2 py-1">
                  {h.type !== "proposal_created" ? "Order Released" : h.production_order_id ? "Proposal → Order" : "Proposal Created"}
                </td>
                <td className="border border-[#D0D5DD] px-2 py-1">
                  {h.type !== "proposal_created"
                    ? h.production_order_id
                    : h.production_order_id
                      ? `${h.material_id} (Proposal ${h.production_proposal_id} \u2192 Order ${h.production_order_id})`
                      : `${h.material_id} (Proposal ${h.production_proposal_id})`}
                </td>
                <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]" data-testid={`proposal-history-model-cell-${i}`}>{h.production_model_id || "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1">{h.site_id || "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums">{h.quantity ?? "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1">
                  {h.type !== "proposal_created"
                    ? h.success ? <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Released</Badge> : <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>
                    : h.production_order_id
                      ? h.released ? <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Released</Badge> : <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Release Failed</Badge>
                      : <Badge variant="outline" className="bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]">Created</Badge>}
                </td>
              </tr>
            ))}
            {!loadingHistory && history.length === 0 && <tr><td colSpan={8} className="text-center py-6 text-[#98A2B3] border border-[#D0D5DD]">No Production Orders created yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
};

// -------------------- Main page --------------------
export default function ProductionConfirmationPage() {
  // Aug 2026, user's explicit ask (same change as Store Approval): the
  // manual "Your name" box is gone - this page already requires Entra ID
  // login, so the acting person's name comes straight from their
  // signed-in session instead.
  const { user } = useAuth();
  const actorName = user?.name || user?.email || "";
  const [statusFilter, setStatusFilter] = useState("open");
  const [creatorFilter, setCreatorFilter] = useState("all");
  const [sortLatestFirst, setSortLatestFirst] = useState(false);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [manualLotId, setManualLotId] = useState("");
  const [confirmRow, setConfirmRow] = useState(null);
  const [reasons, setReasons] = useState([]);
  const [showReasons, setShowReasons] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [stockByRow, setStockByRow] = useState({});
  const [lastConfirmationByLot, setLastConfirmationByLot] = useState({});

  const loadReasons = useCallback(() => {
    axios.get(`${API}/production-confirmation/deviation-reasons`).then(({ data }) => setReasons(data.reasons)).catch(() => {});
  }, []);

  useEffect(() => {
    if (rows.length === 0) {
      setStockByRow({});
      return;
    }
    axios.post(`${API}/production-confirmation/component-availability-batch`, {
      rows: rows.map((r) => ({ main_output_product: r.main_output_product, quantity: r.open_quantity || 0, site_id: r.site_id, material_inputs: r.material_inputs || null })),
    }).then(({ data }) => {
      const map = {};
      rows.forEach((r, i) => { map[rowKey(r)] = data.results[i]; });
      setStockByRow(map);
    }).catch(() => {});
  }, [rows]);

  useEffect(() => {
    const lotIds = [...new Set(rows.map((r) => r.production_lot_id))];
    if (lotIds.length === 0) {
      setLastConfirmationByLot({});
      return;
    }
    axios.post(`${API}/production-confirmation/history/latest-batch`, { production_lot_ids: lotIds })
      .then(({ data }) => setLastConfirmationByLot(data))
      .catch(() => {});
  }, [rows]);

  const loadOpenLots = useCallback(async () => {
    setLoading(true);
    setAuthError(null);
    setLoadError(null);
    try {
      const { data } = await axios.get(`${API}/production-confirmation/open-lots`, { params: { status: statusFilter } });
      setRows(data.rows);
    } catch (e) {
      if (e.response?.status === 403) setAuthError(e.response.data.detail);
      else setLoadError(e.response?.data?.detail || "Failed to load production lots from SAP");
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => { loadReasons(); }, [loadReasons]);
  useEffect(() => { loadOpenLots(); }, [loadOpenLots]);

  const lookupManual = async () => {
    if (!manualLotId.trim()) return;
    setLoading(true);
    setAuthError(null);
    setLoadError(null);
    try {
      const { data } = await axios.get(`${API}/production-confirmation/lot/${manualLotId.trim()}`);
      setRows(data.rows);
      toast.success(`Loaded Production Lot ${manualLotId.trim()}`);
    } catch (e) {
      if (e.response?.status === 403) setAuthError(e.response.data.detail);
      else toast.error(e.response?.data?.detail || "Production lot not found");
    } finally {
      setLoading(false);
    }
  };

  const onConfirmed = (row) => {
    setConfirmRow(null);
    setRows((prev) => prev.filter((r) => rowKey(r) !== rowKey(row)));
  };

  // "Show mine" compares against the same actor string this app itself
  // writes on order creation (see createProposal's `actor: actorName.trim()`
  // below) - a case-insensitive match so a slightly different casing from
  // Entra ID doesn't silently hide a user's own orders.
  const visibleRows = useMemo(() => {
    let out = rows;
    if (creatorFilter === "mine") {
      const mine = actorName.trim().toLowerCase();
      out = out.filter((r) => (r.created_by || "").trim().toLowerCase() === mine);
    }
    if (sortLatestFirst) {
      out = [...out].sort((a, b) => {
        const at = a.order_created_at ? new Date(a.order_created_at).getTime() : -Infinity;
        const bt = b.order_created_at ? new Date(b.order_created_at).getTime() : -Infinity;
        return bt - at;
      });
    }
    return out;
  }, [rows, creatorFilter, sortLatestFirst, actorName]);

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />

      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Production Confirmation</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4">
        <Tabs defaultValue="create" className="space-y-4">
          <TabsList data-testid="page-tabs">
            <TabsTrigger value="create" data-testid="tab-create-order">Create Production Order</TabsTrigger>
            <TabsTrigger value="confirm" data-testid="tab-confirm-production">Production Confirmation</TabsTrigger>
            <TabsTrigger value="my-requests" data-testid="tab-my-requests">My Stock Requests</TabsTrigger>
          </TabsList>

          <TabsContent value="create">
            <CreateOrderTab actorName={actorName} />
          </TabsContent>

          <TabsContent value="my-requests">
            <MyStockRequestsTab actorName={actorName} />
          </TabsContent>

          <TabsContent value="confirm" className="space-y-4">
        {authError && (
          <Alert className="bg-[#FFFAEB] border-[#FEDF89]" data-testid="auth-error-banner">
            <WarningCircle size={16} className="text-[#B54708]" />
            <AlertTitle className="text-[#B54708]">SAP Authorization Required</AlertTitle>
            <AlertDescription className="text-[#B54708] text-sm">
              {authError} Ask your SAP admin to go to <strong>Application and User Management → Communication Arrangements</strong>,
              activate the <strong>"Query Production Lots"</strong> and <strong>"Manage Production Lots"</strong> Communication Scenarios,
              and authorize the technical user <strong>_EMERGENTBOM</strong> for them.
            </AlertDescription>
          </Alert>
        )}
        {loadError && !authError && (
          <Alert className="bg-[#FEF3F2] border-[#FECDCA]" data-testid="load-error-banner">
            <WarningCircle size={16} className="text-[#B42318]" />
            <AlertDescription className="text-[#B42318] text-sm">{loadError}</AlertDescription>
          </Alert>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-56 bg-white" data-testid="status-filter-select"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="open">Open (Released/Started)</SelectItem>
              <SelectItem value="all">All Statuses</SelectItem>
            </SelectContent>
          </Select>
          <Button variant="outline" onClick={loadOpenLots} data-testid="refresh-lots-button">
            <ArrowClockwise size={14} className="mr-1.5" /> Refresh
          </Button>
          <div className="w-px h-6 bg-[#D0D5DD] mx-1" />
          <Select value={creatorFilter} onValueChange={setCreatorFilter}>
            <SelectTrigger className="w-40 bg-white" data-testid="creator-filter-select"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all" data-testid="creator-filter-all">Show all</SelectItem>
              <SelectItem value="mine" data-testid="creator-filter-mine">Show mine</SelectItem>
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            onClick={() => setSortLatestFirst((s) => !s)}
            className={sortLatestFirst ? "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" : ""}
            data-testid="sort-latest-toggle-button"
          >
            <ClockCounterClockwise size={14} className="mr-1.5" /> Sort by latest {sortLatestFirst ? "✓" : ""}
          </Button>
          <div className="w-px h-6 bg-[#D0D5DD] mx-1" />
          <Input
            placeholder="Enter Production Lot ID..."
            value={manualLotId}
            onChange={(e) => setManualLotId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && lookupManual()}
            className="w-56 bg-white"
            data-testid="manual-lot-id-input"
          />
          <Button variant="outline" onClick={lookupManual} data-testid="manual-lookup-button">
            <MagnifyingGlass size={14} className="mr-1.5" /> Look Up
          </Button>
          <div className="flex-1" />
          <Button variant="outline" onClick={() => setShowHistory(true)} data-testid="open-history-button">
            <ClockCounterClockwise size={14} className="mr-1.5" /> History
          </Button>
          <Button variant="outline" onClick={() => setShowReasons(true)} data-testid="open-manage-reasons-button">
            <Gear size={14} className="mr-1.5" /> Manage Reasons
          </Button>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 max-w-md">
          <StatCard icon={ListChecks} label="Reporting Points Shown" value={visibleRows.length} testId="stat-reporting-points" />
          <StatCard icon={CheckCircle} label="Distinct Lots" value={new Set(visibleRows.map((r) => r.production_lot_id)).size} testId="stat-distinct-lots" />
        </div>

        {loading ? (
          <div className="space-y-2">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full rounded-sm" />)}</div>
        ) : (
          <div className="border border-[#D0D5DD] rounded-sm overflow-auto bg-white">
            <table className="w-full text-[13px] border-collapse" data-testid="production-lots-table">
              <thead>
                <tr>
                  {["Lot ID", "Output Product", "Site", "Status", "Reporting Point", "Planned", "Confirmed So Far", "Open", "UOM", "Finished", "Created By", "Production Model", "Stock", "Last Confirmation", ""].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((r, i) => {
                  const stock = stockByRow[rowKey(r)];
                  const lastConf = lastConfirmationByLot[r.production_lot_id];
                  return (
                  <tr key={rowKey(r)} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`lot-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium text-[#101828]">{r.production_lot_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.main_output_product || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.site_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Badge variant="outline" className={`${STATUS_TONE[r.life_cycle_status_label] || "bg-slate-100 text-slate-600 border-slate-200"} border`}>{r.life_cycle_status_label}</Badge>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.reporting_point_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.planned_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.total_confirmed_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-bold text-[#B54708]">{formatQty(r.open_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{formatUnit(r.unit_code) || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.confirmation_finished ? "Yes" : "No"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]" data-testid={`created-by-cell-${i}`}>{r.created_by || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]" data-testid={`production-model-cell-${i}`}>{r.production_model_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      {!stock ? (
                        <span className="text-[11px] text-[#98A2B3]" data-testid={`stock-badge-loading-${i}`}>…</span>
                      ) : !stock.checked ? (
                        <span className="text-[11px] text-[#98A2B3]" data-testid={`stock-badge-unknown-${i}`}>No BOM cached</span>
                      ) : stock.sufficient_all ? (
                        <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] border" data-testid={`stock-badge-ok-${i}`}>OK</Badge>
                      ) : (
                        <Popover>
                          <PopoverTrigger asChild>
                            <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA] border cursor-pointer" data-testid={`stock-badge-short-${i}`}>
                              Short ({stock.short_components.length})
                            </Badge>
                          </PopoverTrigger>
                          <PopoverContent className="w-auto max-w-sm bg-white border border-[#D0D5DD] text-[#1D2939] shadow-lg p-3 text-xs space-y-1" data-testid={`stock-tooltip-${i}`}>
                            <div className="font-bold font-heading uppercase tracking-wide text-[10px] text-[#667085] mb-1">Short Components</div>
                            {stock.short_components.map((c) => (
                              <div key={c.product_id}>
                                {c.product_id}: need {formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}, have {c.available_qty === null ? "no data" : `${formatQty(c.available_qty)} ${formatUnit(c.unit_of_measure)}`}
                              </div>
                            ))}
                          </PopoverContent>
                        </Popover>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5" data-testid={`last-confirmation-cell-${i}`}>
                      <LastConfirmationBadges
                        data={lastConf}
                        productionLotId={r.production_lot_id}
                        siteId={r.site_id}
                        mainOutputProduct={r.main_output_product}
                        unitCode={r.unit_code}
                        actorName={actorName}
                        onRetried={(lotId, patch) => setLastConfirmationByLot((prev) => (
                          prev[lotId] ? { ...prev, [lotId]: { ...prev[lotId], ...patch } } : prev
                        ))}
                      />
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Button
                        size="sm"
                        disabled={loading || r.confirmation_finished}
                        onClick={() => setConfirmRow(r)}
                        title={r.confirmation_finished ? "Already marked as finished - re-confirmation disabled to avoid a duplicate SAP posting" : undefined}
                        data-testid={`confirm-button-${i}`}
                      >
                        Confirm
                      </Button>
                    </td>
                  </tr>
                  );
                })}
                {rows.length === 0 && authError && (
                  <tr><td colSpan={14} className="text-center py-8 text-[#B54708] bg-[#FFFAEB] border border-[#D0D5DD]" data-testid="blocked-state">Blocked by SAP authorization - see banner above.</td></tr>
                )}
                {rows.length === 0 && !authError && !loadError && (
                  <tr><td colSpan={14} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="empty-state">No open production lots found.</td></tr>
                )}
                {rows.length > 0 && visibleRows.length === 0 && (
                  <tr><td colSpan={14} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="filtered-empty-state">No rows match "Show mine" - no open lots were created by you.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
          </TabsContent>
        </Tabs>
      </main>

      <ConfirmDialog row={confirmRow} actorName={actorName} onClose={() => setConfirmRow(null)} onConfirmed={onConfirmed} reasons={reasons} />
      <ManageReasonsDialog open={showReasons} onClose={() => setShowReasons(false)} reasons={reasons} onChanged={setReasons} />
      <HistoryDialog open={showHistory} onClose={() => setShowHistory(false)} />
    </div>
  );
}
