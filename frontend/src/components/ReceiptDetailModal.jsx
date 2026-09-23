import { useState, useEffect, useRef } from "react";
import axios from "axios";
import { CircleNotch, CheckCircle, WarningCircle, ArrowRight } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { toast } from "@/components/ui/sonner";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const POLL_MS = 1500;

const formatQty = (v) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 }));

// Sep 23 2026, user's redesign ask - a single reusable modal: "receive"
// mode (Pending tab) fires the Goods Receipt the instant it opens (so
// that ~fast SAP call overlaps with the time the user spends reading
// this preview, per the user's explicit choice) and only fires the
// slower Warehouse Move once the user hits Confirm; "view" mode
// (Completed tab) just shows the stored outcome with a Retry button.
export function ReceiptDetailModal({ order, mode, onClose, onUpdated }) {
  const alreadyMoved = Boolean(order.receipt_relocation);
  const [step, setStep] = useState(mode === "view" ? "done" : "preview");
  const [receiptJob, setReceiptJob] = useState(null);
  const [relocationJob, setRelocationJob] = useState(null);
  const [finalResult, setFinalResult] = useState(alreadyMoved ? order.receipt_relocation : null);
  const pollRef = useRef(null);

  useEffect(() => {
    if (mode !== "receive" || alreadyMoved) return;
    axios.post(`${API}/inbound-receipts/${order.sto_id}/receive`, { items: [] })
      .then(({ data }) => {
        if (data.already_received) {
          setFinalResult(data.result?.receipt_relocation || { status: "done", lines: [] });
          setStep("done");
        } else if (data.job_id) {
          setReceiptJob({ job_id: data.job_id, status: "running" });
        }
      })
      .catch((e) => toast.error(e?.response?.data?.detail || "Could not start the receipt."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const job = step === "posting_receipt" ? receiptJob : step === "moving_stock" ? relocationJob : null;
    if (!job || job.status !== "running") return;
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await axios.get(`${API}/inbound-receipts/receive-status/${job.job_id}`);
        if (data.status === "running") return;
        clearInterval(pollRef.current);
        if (step === "posting_receipt") {
          setReceiptJob({ ...job, status: data.status, error: data.error });
          if (data.status === "done") beginRelocation();
          else setStep("receipt_failed");
        } else {
          setRelocationJob({ ...job, status: data.status, error: data.error });
          if (data.status === "done") {
            // Sep 24 2026 fix - first-time completion (receive_stock_transfer_order)
            // returns a WRAPPED object with the real relocation shape (status
            // "done"/"failed", to, lines, gac_id) nested under `receipt_relocation`,
            // while a retry (retry_receipt_relocation) returns that same shape
            // directly with no wrapper. Prefer the nested shape when present so
            // both paths render identically (this used to show "Received" with no
            // GM ID on a fresh completion, since `data.result.status` was
            // "received"/"failed" - the overall receipt status - not "done"/"failed").
            setFinalResult(data.result?.receipt_relocation || data.result);
            setStep("done");
            onUpdated();
          } else {
            setStep("relocation_failed");
          }
        }
      } catch {
        // transient poll failure - try again next tick
      }
    }, POLL_MS);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, receiptJob, relocationJob]);

  const beginRelocation = async () => {
    setStep("moving_stock");
    try {
      const { data } = await axios.post(`${API}/inbound-receipts/${order.sto_id}/relocate`);
      setRelocationJob({ job_id: data.job_id, status: "running" });
    } catch (e) {
      setRelocationJob({ status: "failed", error: e?.response?.data?.detail || "Could not start the warehouse move." });
      setStep("relocation_failed");
    }
  };

  const handleConfirm = () => {
    if (!alreadyMoved && receiptJob?.status !== "done") {
      setStep("posting_receipt");
    } else {
      beginRelocation();
    }
  };

  const handleRetryReceipt = () => {
    setReceiptJob(null);
    setStep("preview");
    axios.post(`${API}/inbound-receipts/${order.sto_id}/receive`, { items: [] })
      .then(({ data }) => data.job_id && setReceiptJob({ job_id: data.job_id, status: "running" }))
      .catch((e) => toast.error(e?.response?.data?.detail || "Could not retry the receipt."));
  };

  const progressPct = step === "preview" ? 0 : step === "posting_receipt" ? 25 : step === "receipt_failed" ? 25
    : step === "moving_stock" ? 75 : step === "relocation_failed" ? 75 : 100;

  const statusText = {
    preview: "Reviewing shipment details…",
    posting_receipt: "Posting Goods Receipt in SAP…",
    receipt_failed: "Could not post the Goods Receipt in SAP",
    moving_stock: `Moving stock to ${order.ship_to_location_name || "the destination warehouse"}…`,
    relocation_failed: "Some stock could not be moved to the warehouse",
    done: null,
  }[step];

  const lines = finalResult?.lines || order.receipt_results || [];
  const relocationHasIssue = finalResult && (finalResult.status === "partial" || finalResult.status === "failed");
  const bannerCls = !finalResult ? "" : finalResult.status === "failed" ? "text-[#B42318]" : finalResult.status === "partial" ? "text-[#92400E]" : "text-[#027A48]";

  const deliveryId = order.inbound_delivery_ids?.[0] || order.outbound_delivery_ids?.[0];

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" data-testid="receipt-detail-modal">
        <DialogHeader>
          <DialogTitle data-testid="receipt-detail-modal-title">
            {deliveryId && <span className="text-[#0B6B74]" data-testid="receipt-detail-modal-delivery-id">{deliveryId}</span>}
            {deliveryId && " · "}
            {order.sto_id} — {order.ship_from_site_id} <ArrowRight size={14} className="inline" /> {order.ship_to_site_id}
          </DialogTitle>
          <DialogDescription>SAP #{order.sap_order_id} · {order.ship_to_location_name || "—"}</DialogDescription>
        </DialogHeader>

        <table className="w-full text-sm" data-testid="receipt-detail-modal-items">
          <thead>
            <tr className="text-[#667085] text-xs">
              <th className="text-left py-1">Product</th>
              <th className="text-left py-1">Description</th>
              <th className="text-right py-1">Qty</th>
            </tr>
          </thead>
          <tbody>
            {(order.items || []).map((it) => (
              <tr key={it.line_no}>
                <td className="py-1 font-mono text-xs">{it.product_id}</td>
                <td className="py-1 text-xs">{it.description}</td>
                <td className="py-1 text-right text-xs">{formatQty(it.requested_qty)} {it.unit_of_measure}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {step !== "done" && (
          <div className="mt-2" data-testid="receipt-detail-modal-progress">
            <Progress value={progressPct} className="h-1.5" />
            <div className="flex items-center gap-2 mt-2 text-sm">
              {["posting_receipt", "moving_stock"].includes(step) && <CircleNotch size={15} className="animate-spin text-[#0B6B74] shrink-0" />}
              {["receipt_failed", "relocation_failed"].includes(step) && <WarningCircle size={15} className="text-[#B42318] shrink-0" />}
              <span className={["receipt_failed", "relocation_failed"].includes(step) ? "text-[#B42318]" : "text-[#344054]"} data-testid="receipt-detail-modal-status-text">
                {statusText}
              </span>
            </div>
            {step === "receipt_failed" && <p className="text-xs text-[#B42318] mt-1" data-testid="receipt-detail-modal-error">{receiptJob?.error}</p>}
            {step === "relocation_failed" && <p className="text-xs text-[#B42318] mt-1" data-testid="receipt-detail-modal-error">{relocationJob?.error}</p>}
          </div>
        )}

        {step === "done" && finalResult && (
          <div className="mt-2" data-testid="receipt-detail-modal-result">
            <div className={`flex items-center gap-2 text-sm font-medium mb-2 ${bannerCls}`}>
              {finalResult.status === "failed" || finalResult.status === "partial" ? <WarningCircle size={16} weight="fill" /> : <CheckCircle size={16} weight="fill" />}
              {finalResult.status === "done" ? `Moved to ${finalResult.to}` : finalResult.status === "partial" ? "Partially moved" : finalResult.status === "failed" ? "Warehouse move failed" : "Received"}
              {/* Sep 24 2026, user's explicit ask ("show goods movement ID here
                  itself immediately") - since the atomic relocation fix, every
                  line shares ONE gac_id, so it's shown once here right in the
                  banner instead of making the user scan the per-line table below. */}
              {finalResult.status === "done" && finalResult.gac_id && (
                <span className="text-[#667085] font-normal" data-testid="receipt-detail-modal-gac-id">· GM {finalResult.gac_id}</span>
              )}
            </div>
            {lines.length > 0 && (
              <table className="w-full text-xs border-t border-[#EAECF0] pt-1">
                <thead>
                  <tr className="text-[#667085]">
                    <th className="text-left font-medium py-1">Product</th>
                    <th className="text-right font-medium py-1">Qty Moved</th>
                    <th className="text-right font-medium py-1">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l) => (
                    <tr key={l.product_id} className="border-b border-[#EAECF0]">
                      <td className="py-1.5 pr-2 font-mono text-[#344054]">{l.product_id}</td>
                      <td className="py-1.5 pr-2 text-right text-[#344054]">{l.quantity != null ? `${formatQty(l.quantity)} ${l.unit_of_measure || ""}` : "—"}</td>
                      <td className="py-1.5 text-right">
                        {l.ok ? <span className="text-[#027A48]">GM {l.gac_id}</span> : <span className="text-[#B42318]">{l.error}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {step === "done" && !finalResult && order.receipt_error && (
          <div className="mt-2" data-testid="receipt-detail-modal-result">
            <div className="flex items-center gap-2 text-sm font-medium mb-1 text-[#B42318]">
              <WarningCircle size={16} weight="fill" /> Goods Receipt failed
            </div>
            <p className="text-xs text-[#B42318]">{order.receipt_error}</p>
          </div>
        )}

        <div className="flex justify-end gap-2 mt-3">
          <Button variant="outline" onClick={onClose} data-testid="receipt-detail-modal-close-btn">Close</Button>
          {step === "preview" && (
            <Button onClick={handleConfirm} data-testid="receipt-detail-modal-confirm-btn">{alreadyMoved ? "Retry Warehouse Move" : "Confirm & Receive"}</Button>
          )}
          {step === "receipt_failed" && (
            <Button onClick={handleRetryReceipt} data-testid="receipt-detail-modal-retry-receipt-btn">Retry</Button>
          )}
          {(step === "relocation_failed" || (step === "done" && relocationHasIssue)) && (
            <Button onClick={beginRelocation} data-testid="receipt-detail-modal-retry-relocation-btn">Retry Warehouse Move</Button>
          )}
          {step === "done" && !finalResult && order.receipt_error && (
            <Button onClick={handleRetryReceipt} data-testid="receipt-detail-modal-retry-receipt-btn">Retry</Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
