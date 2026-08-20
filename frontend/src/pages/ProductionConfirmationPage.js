import { useState, useEffect, useCallback } from "react";
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
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const ACTOR_NAME_STORAGE_KEY = "productionConfirmationActorName";

const useActorName = () => {
  const [name, setName] = useState(() => localStorage.getItem(ACTOR_NAME_STORAGE_KEY) || "");
  useEffect(() => localStorage.setItem(ACTOR_NAME_STORAGE_KEY, name), [name]);
  return [name, setName];
};

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
  const [finished, setFinished] = useState(false);
  const [saving, setSaving] = useState(false);
  const [availability, setAvailability] = useState(null);
  const [checkingAvailability, setCheckingAvailability] = useState(false);

  useEffect(() => {
    if (row) {
      setConfirmedQty(row.open_quantity ?? "");
      setConfirmedScrap("0");
      setScrapCalc(null);
      setReason("none");
      // Matches updateConfirmedQty's smart default: qty defaults to the
      // full Open Quantity, so "finished" defaults to checked too - it
      // only unchecks itself if the qty is later reduced to a partial.
      setFinished((row.open_quantity ?? "") !== "");
      setAvailability(null);
      if (row.main_output_product) {
        axios.get(`${API}/production-confirmation/scrap-calc/${encodeURIComponent(row.main_output_product)}`)
          .then(({ data }) => setScrapCalc(data))
          .catch(() => setScrapCalc(null));
      }
    }
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
      axios.get(`${API}/production-confirmation/component-availability`, {
        params: { main_output_product: row.main_output_product, confirmed_quantity: Number(confirmedQty), site_id: row.site_id },
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

  // Weight-based by-product quantity - fully independent of the manual
  // Confirmed Scrap (rejection) field above.
  const qtyNum = Number(confirmedQty);
  const byproductQty = scrapCalc?.available && confirmedQty !== "" && !Number.isNaN(qtyNum)
    ? Math.round(scrapCalc.scrap_per_unit_kg * qtyNum * 1e6) / 1e6
    : null;

  // Smart default: auto-check "finished" only when this confirmation
  // covers the FULL remaining Open Quantity (i.e. actually completes it) -
  // partial confirmations stay unchecked, since checking it locks the lot
  // permanently (confirmed live: a locked lot's by-product can never be
  // corrected again).
  const updateConfirmedQty = (value) => {
    setConfirmedQty(value);
    const num = Number(value);
    setFinished(value !== "" && !Number.isNaN(num) && num === row.open_quantity);
  };

  const qtyExceedsOpen = confirmedQty !== "" && !Number.isNaN(qtyNum) && qtyNum > row.open_quantity;

  const submit = async () => {
    if (!actorName.trim()) {
      toast.error("Enter your name first (top-right of the page)");
      return;
    }
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
    setSaving(true);
    try {
      const { data } = await axios.post(`${API}/production-confirmation/confirm`, {
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
        actor: actorName.trim(),
      });
      if (data.success) {
        toast.success(`Confirmation posted to SAP for Lot ${row.production_lot_id}`);
        if (data.byproduct_confirmation) {
          if (data.byproduct_confirmation.success) {
            toast.success(`By-product ${byproductMatch?.product_id || "quantity"} (${byproductQty ?? 0} ${byproductMatch?.unit_code || ""}) posted to SAP`);
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
        onConfirmed(row);
      } else {
        toast.error(`SAP reported an issue: ${data.logs?.map((l) => l.note).join("; ") || "see history for details"}`);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to post confirmation to SAP");
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
          <div className="text-xs text-[#667085] bg-[#F9FAFB] border border-[#EAECF0] rounded-sm px-3 py-2">
            Planned: <strong className="text-[#1D2939]">{formatQty(row.planned_quantity)}</strong> {row.unit_code} ·
            {" "}Open: <strong className="text-[#1D2939]">{formatQty(row.open_quantity)}</strong> {row.unit_code}
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Confirmed Output Quantity</Label>
            <Input type="number" value={confirmedQty} onChange={(e) => updateConfirmedQty(e.target.value)} data-testid="confirm-qty-input" />
            {qtyExceedsOpen && (
              <p className="text-xs text-[#B54708] mt-1" data-testid="confirm-qty-exceeds-open-warning">
                Note: {confirmedQty} exceeds the Open Quantity ({row.open_quantity} {row.unit_code}) - double-check before posting if this isn't intentional over-production.
              </p>
            )}
          </div>

          {checkingAvailability && <p className="text-xs text-[#98A2B3]">Checking component stock...</p>}
          {availability?.checked && (
            <div className="border border-[#EAECF0] rounded-sm overflow-hidden" data-testid="component-availability-panel">
              <div className="bg-[#F9FAFB] px-3 py-1.5 text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                Component Stock Check {row.site_id ? `(Site ${row.site_id})` : ""}
              </div>
              <div className="max-h-32 overflow-y-auto divide-y divide-[#EAECF0]">
                {availability.components.map((c) => (
                  <div key={c.product_id} className="flex items-center justify-between px-3 py-1 text-xs" data-testid={`component-row-${c.product_id}`}>
                    <span className="text-[#344054] truncate mr-2">{c.product_id}{c.description ? ` - ${c.description}` : ""}</span>
                    <span className={`shrink-0 tabular-nums ${c.sufficient ? "text-[#027A48]" : c.available_qty === null ? "text-[#98A2B3]" : "text-[#B42318] font-bold"}`}>
                      {c.available_qty === null ? "no stock data" : `${formatQty(c.available_qty)} / ${formatQty(c.required_qty)} ${c.unit_of_measure || ""}`}
                      {c.sufficient ? " ✓" : c.available_qty !== null ? " ✗" : ""}
                    </span>
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
                    ? <span className="text-[#027A48]" data-testid="byproduct-match-found"> · will post {byproductQty ?? 0} {byproductMatch.unit_code} of {byproductMatch.product_id} to SAP</span>
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
            <Checkbox id="finished-cb" checked={finished} onCheckedChange={setFinished} data-testid="confirm-finished-checkbox" />
            <Label htmlFor="finished-cb" className="text-sm text-[#344054] cursor-pointer">Mark this task as finished</Label>
          </div>
          <p className="text-[11px] text-[#98A2B3]">Component/input quantities are NOT sent - SAP's backflush auto-consumes BOM inputs from the confirmed output.</p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} data-testid="confirm-cancel-button">Cancel</Button>
          <Button onClick={submit} disabled={saving} data-testid="confirm-submit-button">
            {saving ? "Posting to SAP..." : "Post Confirmation"}
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
                  {["When", "By", "Lot", "Product", "Qty", "Scrap", "Finished", "Result", "WIP Clearing"].map((h) => (
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
                      {e.success ? <Badge className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Success</Badge> : <Badge className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1">
                      {!e.wip_clearing ? "—" : e.wip_clearing.success ? <Badge className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Cleared</Badge> : <Badge className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>}
                    </td>
                  </tr>
                ))}
                {entries.length === 0 && <tr><td colSpan={9} className="text-center py-6 text-[#98A2B3] border border-[#D0D5DD]">No confirmations submitted yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
};

// -------------------- Create Production Order tab --------------------
const CreateOrderTab = ({ actorName }) => {
  const [materialId, setMaterialId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [unitCode, setUnitCode] = useState("EA");
  const [requestedEndDate, setRequestedEndDate] = useState("");
  const [creating, setCreating] = useState(false);
  const [createPhase, setCreatePhase] = useState("");
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [releaseOrderId, setReleaseOrderId] = useState("");
  const [releasing, setReleasing] = useState(false);
  const [history, setHistory] = useState([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [sosOptions, setSosOptions] = useState([]);
  const [sosLoading, setSosLoading] = useState(false);
  const [sosChecked, setSosChecked] = useState(false);
  const [selectedSosKey, setSelectedSosKey] = useState("");
  const [siteAutoFilled, setSiteAutoFilled] = useState(false);

  const PHASE_LABELS = {
    running: "Starting...",
    creating_proposal: "Creating Proposal in SAP...",
    waiting_for_order: "Waiting for SAP to convert Proposal to Order...",
    releasing_order: "Releasing Order in SAP...",
  };

  const selectedSosOption = sosOptions[Number(selectedSosKey)];

  const chooseSosOption = (key) => {
    setSelectedSosKey(key);
    const option = sosOptions[Number(key)];
    if (option) { setSiteId(option.site_id); setSiteAutoFilled(true); } // model determines site in SAP, not the other way around
  };

  const checkSourceOfSupply = async () => {
    const id = materialId.trim();
    if (!id) return;
    setSosLoading(true);
    setSosOptions([]);
    setSelectedSosKey("");
    try {
      const { data } = await axios.get(`${API}/production-confirmation/source-of-supply-options/${encodeURIComponent(id)}`);
      const options = data.options || [];
      setSosOptions(options);
      const preferred = options.findIndex((o) => o.is_active);
      const idx = preferred >= 0 ? preferred : (options.length > 0 ? 0 : -1);
      if (idx >= 0) {
        setSelectedSosKey(String(idx));
        setSiteId(options[idx].site_id); // model determines site - always overwritten here, never an independent pre-filter
        setSiteAutoFilled(true);
      } else if (siteAutoFilled) {
        setSiteId(""); // clear a stale auto-filled site from a previous material - but never clobber a site the user typed themselves
        setSiteAutoFilled(false);
      }
    } catch (e) {
      // Non-fatal - proceeding without an explicit choice just leaves SAP's own default in place
      setSosOptions([]);
    } finally {
      setSosLoading(false);
      setSosChecked(true);
    }
  };
  const POLL_INTERVAL_MS = 4000;
  const MAX_POLL_MS = 22 * 60 * 1000; // job itself gives up after 20 min, add a small buffer

  const loadHistory = useCallback(() => {
    setLoadingHistory(true);
    axios.get(`${API}/production-confirmation/proposal-history`).then(({ data }) => setHistory(data.entries)).catch(() => toast.error("Failed to load Proposal/Release history")).finally(() => setLoadingHistory(false));
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const createProposal = async () => {
    if (!actorName.trim()) {
      toast.error("Enter your name first (top-right of the page)");
      return;
    }
    if (!materialId.trim() || !siteId.trim() || !quantity) {
      toast.error("Product, Site and Quantity are required");
      return;
    }
    setCreating(true);
    setElapsedSeconds(0);
    setCreatePhase("running");
    const startedAt = Date.now();
    try {
      const { data } = await axios.post(`${API}/production-confirmation/create-and-release-order`, {
        material_id: materialId.trim(),
        site_id: siteId.trim().toUpperCase(),
        quantity: Number(quantity),
        unit_code: unitCode.trim().toUpperCase() || "EA",
        availability_datetime: requestedEndDate ? new Date(requestedEndDate).toISOString() : null,
        actor: actorName.trim(),
        logistic_relationship_uuid: selectedSosOption ? selectedSosOption.logistic_relationship_uuid : null,
      });
      const jobId = data.job_id;

      // Runs fully in the background on the server (no HTTP request held
      // open) since SAP's own conversion + indexing can take minutes -
      // poll a status endpoint instead, same pattern as Purchasing Plan.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        setElapsedSeconds(Math.round((Date.now() - startedAt) / 1000));
        const { data: job } = await axios.get(`${API}/production-confirmation/create-and-release-order/status/${jobId}`);
        setCreatePhase(job.status);
        if (job.status === "done") {
          const result = job.result;
          if (result.production_order_id && result.released) {
            toast.success(`Production Order ${result.production_order_id} created and released in SAP - ready for confirmation`);
          } else if (result.production_order_id) {
            toast.error(`Order ${result.production_order_id} was created but Release failed - use the fallback form below to retry`);
          } else {
            toast.error(result.note || "Proposal created but the Order hasn't appeared yet - it keeps retrying automatically, check history shortly");
          }
          setMaterialId(""); setQuantity("1"); setRequestedEndDate("");
          setSosOptions([]); setSosChecked(false); setSelectedSosKey(""); setSiteAutoFilled(false);
          loadHistory();
          break;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "Failed to create Production Order in SAP");
        }
        if (Date.now() - startedAt > MAX_POLL_MS) {
          throw new Error("This is taking unusually long - it's still running in the background, check the history below shortly");
        }
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || "Failed to create Production Order in SAP");
    } finally {
      setCreating(false);
      setCreatePhase("");
    }
  };

  const releaseOrder = async () => {
    if (!actorName.trim()) {
      toast.error("Enter your name first (top-right of the page)");
      return;
    }
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
          <p className="text-xs text-[#667085]">Creates and releases a Production Order in SAP - fully automated, retries in the background until SAP converts it. Checks component stock first and blocks with a clear reason if a raw material is out of stock (no orphaned Proposals).</p>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Product ID</Label>
            <Input
              value={materialId}
              onChange={(e) => { setMaterialId(e.target.value); setSosChecked(false); setSosOptions([]); }}
              onBlur={checkSourceOfSupply}
              placeholder="e.g. MAZ42117272-TA"
              data-testid="create-proposal-product-input"
            />
          </div>
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
              <p className="text-xs text-[#B54708] mt-1">
                {sosOptions.length > 1
                  ? `This material has ${sosOptions.length} valid Production Model/Site combinations - pick one, it sets the Site for you.`
                  : "Picking this confirms the Production Model - and its Site - SAP will use."}
              </p>
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs font-bold text-[#344054]">Site</Label>
              <Input value={siteId} onChange={(e) => { setSiteId(e.target.value.toUpperCase()); setSiteAutoFilled(false); }} placeholder="e.g. P2" data-testid="create-proposal-site-input" />
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">UoM</Label>
              <Input value={unitCode} onChange={(e) => setUnitCode(e.target.value)} placeholder="EA" data-testid="create-proposal-uom-input" />
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
          <Button onClick={createProposal} disabled={creating} className="w-full" data-testid="create-proposal-submit-button">
            {creating ? `${PHASE_LABELS[createPhase] || "Working..."} (${elapsedSeconds}s)` : "Create Production Order"}
          </Button>
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

      <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto">
        <table className="w-full text-[12px] border-collapse" data-testid="proposal-history-table">
          <thead>
            <tr>
              {["When", "By", "Action", "Product/Order", "Site", "Qty", "Result"].map((h) => (
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
                <td className="border border-[#D0D5DD] px-2 py-1">{h.site_id || "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums">{h.quantity ?? "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1">
                  {h.type !== "proposal_created"
                    ? h.success ? <Badge className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Released</Badge> : <Badge className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Failed</Badge>
                    : h.production_order_id
                      ? h.released ? <Badge className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Released</Badge> : <Badge className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Release Failed</Badge>
                      : <Badge className="bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]">Created</Badge>}
                </td>
              </tr>
            ))}
            {!loadingHistory && history.length === 0 && <tr><td colSpan={7} className="text-center py-6 text-[#98A2B3] border border-[#D0D5DD]">No Production Orders created yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
};

// -------------------- Main page --------------------
export default function ProductionConfirmationPage() {
  const [actorName, setActorName] = useActorName();
  const [statusFilter, setStatusFilter] = useState("open");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [manualLotId, setManualLotId] = useState("");
  const [confirmRow, setConfirmRow] = useState(null);
  const [reasons, setReasons] = useState([]);
  const [showReasons, setShowReasons] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const loadReasons = useCallback(() => {
    axios.get(`${API}/production-confirmation/deviation-reasons`).then(({ data }) => setReasons(data.reasons)).catch(() => {});
  }, []);

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
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="font-sans text-[11px] text-white/70 hidden md:inline">Your name:</span>
          <input
            type="text"
            placeholder="Your name..."
            value={actorName}
            onChange={(e) => setActorName(e.target.value)}
            className="h-7 w-20 sm:w-36 px-2.5 text-[13px] rounded-full border border-white/25 bg-white/15 text-white placeholder:text-white/50 focus:outline-none focus:border-white/70 focus:bg-white/25"
            data-testid="actor-name-input"
          />
        </div>
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4">
        <Tabs defaultValue="confirm" className="space-y-4">
          <TabsList data-testid="page-tabs">
            <TabsTrigger value="create" data-testid="tab-create-order">Create Production Order</TabsTrigger>
            <TabsTrigger value="confirm" data-testid="tab-confirm-production">Production Confirmation</TabsTrigger>
          </TabsList>

          <TabsContent value="create">
            <CreateOrderTab actorName={actorName} />
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
          <StatCard icon={ListChecks} label="Reporting Points Shown" value={rows.length} testId="stat-reporting-points" />
          <StatCard icon={CheckCircle} label="Distinct Lots" value={new Set(rows.map((r) => r.production_lot_id)).size} testId="stat-distinct-lots" />
        </div>

        {loading ? (
          <div className="space-y-2">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full rounded-sm" />)}</div>
        ) : (
          <div className="border border-[#D0D5DD] rounded-sm overflow-auto bg-white">
            <table className="w-full text-[13px] border-collapse" data-testid="production-lots-table">
              <thead>
                <tr>
                  {["Lot ID", "Output Product", "Site", "Status", "Reporting Point", "Planned", "Confirmed So Far", "Open", "UOM", "Finished", ""].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={rowKey(r)} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`lot-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium text-[#101828]">{r.production_lot_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.main_output_product || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.site_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Badge className={`${STATUS_TONE[r.life_cycle_status_label] || "bg-slate-100 text-slate-600 border-slate-200"} border`}>{r.life_cycle_status_label}</Badge>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.reporting_point_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.planned_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.total_confirmed_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-bold text-[#B54708]">{formatQty(r.open_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.unit_code || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.confirmation_finished ? "Yes" : "No"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Button size="sm" disabled={loading} onClick={() => setConfirmRow(r)} data-testid={`confirm-button-${i}`}>Confirm</Button>
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && authError && (
                  <tr><td colSpan={11} className="text-center py-8 text-[#B54708] bg-[#FFFAEB] border border-[#D0D5DD]" data-testid="blocked-state">Blocked by SAP authorization - see banner above.</td></tr>
                )}
                {rows.length === 0 && !authError && !loadError && (
                  <tr><td colSpan={11} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="empty-state">No open production lots found.</td></tr>
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
