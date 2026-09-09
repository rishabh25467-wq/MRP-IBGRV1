import { useState, useEffect, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import axios from "axios";
import { ArrowClockwise, CheckCircle, XCircle, Printer, X } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/components/ui/sonner";
import { ReturnPrintSlip } from "@/components/ReturnPrintSlip";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");
const formatDate = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "\u2014");

const RETURN_STATUS_BADGE = {
  pending: { label: "Awaiting Store", tone: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]" },
  under_verification: { label: "Under Verification", tone: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" },
  resolved: { label: "Resolved", tone: "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]" },
  rejected: { label: "Rejected", tone: "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]" },
};

const StatusBadge = ({ status }) => (
  <Badge className={`${RETURN_STATUS_BADGE[status]?.tone || ""} border`} data-testid={`store-return-status-badge-${status}`}>
    {RETURN_STATUS_BADGE[status]?.label || status}
  </Badge>
);

// Sep 9 2026, Store side of the "Return to Store" workflow (paired with
// ReturnToStoreTab.jsx on Production). "Process" opens the full
// verification screen; "Confirm Return" fires the SAME SAP Goods
// Movement client store_approval_service uses for issuing (see
// store_return_service.run_confirm_movements) but with source/target
// warehouses reversed - the return only becomes "Resolved" once that SAP
// call actually succeeds; a failure keeps it retryable, never silently
// marked done.
export const StoreReturnsPanel = ({ siteFilter, storeActorName }) => {
  const [returns, setReturns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState(null);
  const [printTarget, setPrintTarget] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data } = await axios.get(`${API}/store-returns/pending`, { params: siteFilter !== "all" ? { site_id: siteFilter } : {} });
      setReturns(data.returns || []);
    } catch {
      toast.error("Failed to load Pending Store Return queue");
    } finally {
      setLoading(false);
    }
  }, [siteFilter]);

  useEffect(() => {
    load();
    const interval = setInterval(load, 8000);
    return () => clearInterval(interval);
  }, [load]);

  useEffect(() => {
    if (!printTarget) return;
    const t = setTimeout(() => window.print(), 50);
    return () => clearTimeout(t);
  }, [printTarget]);
  useEffect(() => {
    const clear = () => setPrintTarget(null);
    window.addEventListener("afterprint", clear);
    return () => window.removeEventListener("afterprint", clear);
  }, []);

  const openProcess = async (ret) => {
    try {
      const { data } = await axios.post(`${API}/store-returns/${ret._id}/process`, { actor: storeActorName });
      setProcessing(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to open this return for processing");
    }
  };

  return (
    <div className="space-y-2" data-testid="store-returns-panel">
      {printTarget && createPortal(<ReturnPrintSlip ret={printTarget} />, document.body)}
      <div className="flex items-center justify-between">
        <p className="text-xs text-[#667085]">Return to Store requests submitted by Production, waiting for Store action.</p>
        <Button variant="outline" size="sm" onClick={load} data-testid="store-returns-refresh-button">
          <ArrowClockwise size={13} className="mr-1.5" /> Refresh
        </Button>
      </div>
      <div className="overflow-x-auto bg-white border border-[#D0D5DD] rounded-sm">
        {loading ? (
          <p className="p-4 text-sm text-[#667085]">Loading...</p>
        ) : returns.length === 0 ? (
          <p className="p-4 text-sm text-[#667085]" data-testid="store-returns-empty-state">No pending store returns right now.</p>
        ) : (
          <table className="w-full text-xs border-collapse">
            <thead><tr>
              {["Return ID", "Original Request", "Return Type", "Requested At", "Material", "Qty", "Site", "Requester", "Reason", "Status", "Action"].map((h) => (
                <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {returns.map((r, i) => (
                <tr key={r._id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`store-return-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono font-bold">{r._id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{r.original_request_id || "\u2014"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.return_type === "against_request" ? "Against Request" : "Manual"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 whitespace-nowrap">{formatDate(r.created_at)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{(r.items || []).map((it) => it.product_id).join(", ")}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty((r.items || []).reduce((s, it) => s + (it.return_qty || 0), 0))}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.site_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.requester}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{(r.items || [])[0]?.reason_label}{(r.items || []).length > 1 ? " +" : ""}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5"><StatusBadge status={r.status} /></td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    <Button size="sm" className="h-6 px-2 text-[11px] bg-[#0E7C86] hover:bg-[#0B5F67]" onClick={() => openProcess(r)} data-testid={`store-return-process-button-${i}`}>
                      Process
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {processing && (
        <ProcessReturnDialog
          ret={processing} storeActorName={storeActorName}
          onClose={() => setProcessing(null)}
          onResolved={() => { setProcessing(null); load(); }}
          onPrint={(r) => setPrintTarget(r)}
        />
      )}
    </div>
  );
};

const ProcessReturnDialog = ({ ret, storeActorName, onClose, onResolved, onPrint }) => {
  const [current, setCurrent] = useState(ret);
  const [issueInfo, setIssueInfo] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const pollRef = useRef(null);

  useEffect(() => {
    if (current.return_type !== "against_request" || !current.original_request_id) return;
    axios.get(`${API}/store-returns/against-request/${current.original_request_id}`).then(({ data }) => setIssueInfo(data)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current.original_request_id]);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const confirmReturn = async () => {
    setConfirming(true);
    try {
      const { data } = await axios.post(`${API}/store-returns/${current._id}/confirm`, { actor: storeActorName });
      const jobId = data.job_id;
      pollRef.current = setInterval(async () => {
        try {
          const { data: job } = await axios.get(`${API}/store-returns/confirm-status/${jobId}`);
          if (job.status === "done") {
            clearInterval(pollRef.current);
            const { data: fresh } = await axios.get(`${API}/store-returns/${current._id}`);
            setCurrent(fresh);
            setConfirming(false);
            if (fresh.status === "resolved") {
              toast.success(`${fresh._id} posted to SAP and marked Resolved`);
              onResolved();
            } else {
              toast.error("SAP Goods Movement failed for one or more items - see details below, you can retry");
            }
          } else if (job.status === "failed") {
            clearInterval(pollRef.current);
            setConfirming(false);
            toast.error(job.error || "Confirm failed");
          }
        } catch {
          // transient - next tick retries
        }
      }, 1500);
    } catch (e) {
      setConfirming(false);
      toast.error(e.response?.data?.detail || "Failed to start confirm");
    }
  };

  const rejectReturn = async () => {
    if (!rejectReason.trim()) {
      toast.error("Rejection reason is required");
      return;
    }
    setRejecting(true);
    try {
      await axios.post(`${API}/store-returns/${current._id}/reject`, { actor: storeActorName, reason: rejectReason.trim() });
      toast.success(`${current._id} rejected`);
      onResolved();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to reject");
    } finally {
      setRejecting(false);
    }
  };

  const anyFailed = (current.items || []).some((it) => it.sap_goods_movement && !it.sap_goods_movement.ok);

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="rounded-sm max-w-4xl" data-testid="store-return-process-dialog">
        <DialogHeader>
          <DialogTitle className="font-heading flex items-center justify-between gap-2 pr-6">
            <span className="font-mono">{current._id}</span>
            <StatusBadge status={current.status} />
          </DialogTitle>
        </DialogHeader>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs bg-[#F9FAFB] border border-[#D0D5DD] rounded-sm p-3">
          <div><span className="text-[#667085] block">Return Type</span><span className="font-bold text-[#1D2939]">{current.return_type === "against_request" ? "Against Request" : "Manual"}</span></div>
          <div><span className="text-[#667085] block">Original Request</span><span className="font-bold text-[#1D2939] font-mono">{current.original_request_id || "\u2014"}</span></div>
          <div><span className="text-[#667085] block">Requester</span><span className="font-bold text-[#1D2939]">{current.requester}</span></div>
          <div><span className="text-[#667085] block">Site</span><span className="font-bold text-[#1D2939]">{current.site_id}</span></div>
        </div>

        {current.return_type === "against_request" && issueInfo && (
          <div className="space-y-1">
            <p className="text-xs font-bold text-[#344054] uppercase">Verify Against Original Store Issue</p>
            <table className="w-full text-xs border-collapse">
              <thead><tr>
                {["Item", "Originally Issued", "Already Returned", "Current Return"].map((h) => (
                  <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase">{h}</th>
                ))}
              </tr></thead>
              <tbody>
                {current.items.map((it, i) => {
                  const src = issueInfo.items.find((x) => x.product_id === it.product_id);
                  return (
                    <tr key={it.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`process-verify-row-${i}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{it.product_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(src?.issued_qty)} {formatUnit(it.unit_of_measure)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.already_returned_qty)} {formatUnit(it.unit_of_measure)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-bold">{formatQty(it.return_qty)} {formatUnit(it.unit_of_measure)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <table className="w-full text-xs border-collapse">
          <thead><tr>
            {["Item Code", "Material", "Return Qty", "UOM", "Reason", "Remarks", "SAP Goods Movement"].map((h) => (
              <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
            ))}
          </tr></thead>
          <tbody>
            {current.items.map((it, i) => (
              <tr key={`${it.product_id}-${i}`} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`process-item-row-${i}`}>
                <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{it.product_id}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "\u2014"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.return_qty)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{formatUnit(it.unit_of_measure)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.reason_label}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.remarks || "\u2014"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">
                  {it.sap_goods_movement?.ok ? <span className="text-[#027A48] font-bold">{it.sap_goods_movement.external_id}</span>
                    : it.sap_goods_movement ? <span className="text-[#B42318]">{it.sap_goods_movement.error || it.sap_goods_movement.reason || "Failed"}</span> : "\u2014"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {current.status === "resolved" ? (
          <p className="text-sm text-[#027A48] font-bold flex items-center gap-1.5"><CheckCircle size={16} weight="fill" /> Resolved - SAP Goods Movement posted successfully.</p>
        ) : (
          <>
            {anyFailed && (
              <p className="text-xs text-[#B42318] bg-[#FEF3F2] border border-[#FECDCA] rounded-sm p-2">One or more SAP Goods Movements failed above - you can retry Confirm Return, it will only retry the failed item(s).</p>
            )}
            <div className="flex items-end gap-3">
              <div className="flex-1">
                <Textarea value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} placeholder="Rejection Reason (required to reject)" className="h-9 min-h-9" data-testid="store-return-reject-reason-input" />
              </div>
              <Button variant="outline" className="border-[#B42318] text-[#B42318] hover:bg-[#FEF3F2]" onClick={rejectReturn} disabled={rejecting || confirming} data-testid="store-return-reject-button">
                <XCircle size={14} className="mr-1.5" /> {rejecting ? "Rejecting..." : "Reject Return"}
              </Button>
              <Button className="bg-[#0E7C86] hover:bg-[#0B5F67]" onClick={confirmReturn} disabled={confirming || rejecting} data-testid="store-return-confirm-button">
                <CheckCircle size={14} className="mr-1.5" /> {confirming ? "Posting to SAP..." : anyFailed ? "Retry Confirm Return" : "Confirm Return"}
              </Button>
            </div>
          </>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onPrint(current)} data-testid="store-return-process-print-button"><Printer size={13} className="mr-1.5" /> Print</Button>
          <Button variant="outline" onClick={onClose} data-testid="store-return-process-close-button"><X size={13} className="mr-1.5" /> Close</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};
