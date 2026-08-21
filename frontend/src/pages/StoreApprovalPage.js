import { useState, useEffect, useCallback } from "react";
import axios from "axios";
import "@/App.css";
import { Package, ArrowLeft, ArrowClockwise, WarningCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const STORE_NAME_KEY = "storeApprovalActorName";

const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));

const LocationBreakdown = ({ locations, unit }) => {
  if (!locations || locations.length === 0) {
    return <span className="text-[#98A2B3]">no stock at this site</span>;
  }
  return (
    <div className="space-y-0.5">
      {locations.map((loc, i) => (
        <div key={i}>
          <span className="text-[#667085]">{loc.warehouse || "Unknown Warehouse"}{loc.stock_status ? ` (${loc.stock_status})` : ""}:</span> {formatQty(loc.qty)} {unit || ""}
        </div>
      ))}
    </div>
  );
};

const STATUS_BADGE = {
  pending: { label: "Awaiting Store", tone: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]" },
  partial_pending_planner: { label: "Awaiting Requester", tone: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" },
};

export default function StoreApprovalPage() {
  const [storeName, setStoreName] = useState(() => localStorage.getItem(STORE_NAME_KEY) || "");
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [issuedQty, setIssuedQty] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [resultMessage, setResultMessage] = useState(null);

  useEffect(() => localStorage.setItem(STORE_NAME_KEY, storeName), [storeName]);

  const loadRequests = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests`);
      setRequests(data.requests);
    } catch {
      toast.error("Failed to load pending stock requests");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRequests();
    const interval = setInterval(loadRequests, 8000);
    return () => clearInterval(interval);
  }, [loadRequests]);

  const openRequest = (r) => {
    setSelected(r);
    setResultMessage(null);
    const defaults = {};
    r.components.forEach((c) => { defaults[c.product_id] = c.issued_qty != null ? String(c.issued_qty) : String(c.required_qty); });
    setIssuedQty(defaults);
  };

  const backToQueue = () => {
    setSelected(null);
    setResultMessage(null);
    loadRequests();
  };

  if (!selected) {
    return (
      <div className="min-h-screen bg-[#F2F4F7] text-[#1D2939]">
        <Toaster position="top-right" />
        <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center px-4 sm:px-6 gap-3" data-testid="store-approval-header">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center">
            <Package size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Store Approval</span>
            <span className="font-sans text-[12px] text-white/70">Process stock requests from Production Planning</span>
          </div>
        </header>
        <main className="max-w-4xl mx-auto p-4 sm:p-6 space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <Label className="text-xs font-bold text-[#344054]">Your Name</Label>
              <Input
                value={storeName}
                onChange={(e) => setStoreName(e.target.value)}
                placeholder="Store user name..."
                className="w-56 bg-white"
                data-testid="store-actor-name-input"
              />
            </div>
            <Button variant="outline" onClick={loadRequests} data-testid="store-refresh-button">
              <ArrowClockwise size={14} className="mr-1.5" /> Refresh
            </Button>
          </div>

          <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-hidden">
            <table className="w-full text-[13px] border-collapse" data-testid="store-requests-table">
              <thead>
                <tr>
                  {["Requested", "Material", "Site", "Qty", "Requester", "Short Components", "Status", ""].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {requests.map((r, i) => (
                  <tr key={r._id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`store-request-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{new Date(r.created_at).toLocaleString("en-IN")}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{r.material_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.site_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.quantity)} {r.unit_code}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.requester}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.components.length}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Badge className={`${STATUS_BADGE[r.status]?.tone || ""} border`}>{STATUS_BADGE[r.status]?.label || r.status}</Badge>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Button size="sm" onClick={() => openRequest(r)} data-testid={`store-request-open-${i}`}>
                        {r.status === "pending" ? "Process" : "View"}
                      </Button>
                    </td>
                  </tr>
                ))}
                {!loading && requests.length === 0 && (
                  <tr><td colSpan={8} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="store-requests-empty-state">No pending stock requests right now.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </main>
      </div>
    );
  }

  const isPending = selected.status === "pending";
  const hasShortfall = isPending && selected.components.some((c) => {
    const q = Number(issuedQty[c.product_id]);
    return Number.isNaN(q) || q < c.required_qty;
  });

  const submitIssue = async (decision) => {
    if (!storeName.trim()) {
      toast.error("Enter your name first");
      return;
    }
    const issued = selected.components.map((c) => ({ product_id: c.product_id, issued_qty: Number(issuedQty[c.product_id]) || 0 }));
    if (issued.some((i) => Number.isNaN(i.issued_qty) || i.issued_qty < 0)) {
      toast.error("Issued quantities must be valid, non-negative numbers");
      return;
    }
    setSubmitting(true);
    try {
      const { data } = await axios.post(`${API}/store-requests/${selected._id}/issue`, {
        issued, decision: hasShortfall ? decision : null, actor: storeName.trim(),
      });
      if (data.status === "resolved") {
        setResultMessage("Stock issue recorded - the automated Production Order pipeline is resuming now.");
        toast.success("Recorded - order creation resuming");
      } else if (data.status === "partial_pending_planner") {
        setResultMessage("Sent to the requester for approval - they'll decide whether to proceed with the partial stock.");
        toast.success("Sent to requester for approval");
      }
      setSelected(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to submit issued quantities");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center px-4 sm:px-6 gap-3">
        <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center">
          <Package size={18} weight="fill" className="text-white" />
        </div>
        <span className="font-heading text-[16px] font-bold text-white tracking-tight">Store Approval</span>
      </header>
      <main className="max-w-3xl mx-auto p-4 sm:p-6 space-y-4">
        <button onClick={backToQueue} className="flex items-center gap-1.5 text-sm text-[#344054] hover:text-[#0E7C86]" data-testid="store-back-to-queue-button">
          <ArrowLeft size={14} weight="bold" /> Back to queue
        </button>

        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3" data-testid="store-request-detail">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="font-heading text-sm font-bold text-[#1D2939] uppercase tracking-wide">{selected.material_id} &middot; {formatQty(selected.quantity)} {selected.unit_code}</h3>
              <p className="text-xs text-[#667085]">Site {selected.site_id} &middot; Requested by {selected.requester} &middot; Proposal {selected.production_proposal_id}</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {isPending && (
                <Input
                  value={storeName}
                  onChange={(e) => setStoreName(e.target.value)}
                  placeholder="Your name..."
                  className="h-8 w-40 bg-white text-xs"
                  data-testid="store-actor-name-input-detail"
                />
              )}
              <Badge className={`${STATUS_BADGE[selected.status]?.tone || "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]"} border`}>{STATUS_BADGE[selected.status]?.label || selected.status}</Badge>
            </div>
          </div>

          {resultMessage && (
            <div className="bg-[#ECFDF3] border border-[#ABEFC6] rounded-sm px-3 py-2 text-xs text-[#027A48]" data-testid="store-result-message">{resultMessage}</div>
          )}

          {selected.status === "partial_pending_planner" && !resultMessage && (
            <div className="bg-[#EFF8FF] border border-[#B2DDFF] rounded-sm px-3 py-2 text-xs text-[#175CD3] flex items-start gap-2" data-testid="store-awaiting-requester-banner">
              <WarningCircle size={14} className="mt-0.5 shrink-0" /> Already submitted - waiting for the requester to approve or reject this partial stock issue.
            </div>
          )}

          <div className="border border-[#D0D5DD] rounded-sm overflow-hidden">
            <table className="w-full text-[12px] border-collapse" data-testid="store-detail-components-table">
              <thead>
                <tr>
                  {["Component", "Required by Production", "In Stock at This Site (By Warehouse)", "Issued Qty"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {selected.components.map((c, i) => (
                  <tr key={c.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top">{c.product_id}{c.description ? ` - ${c.description}` : ""}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums align-top">{formatQty(c.required_qty)} {c.unit_of_measure || ""}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums align-top" data-testid={`store-locations-${i}`}>
                      <LocationBreakdown locations={c.locations} unit={c.unit_of_measure} />
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top">
                      {isPending ? (
                        <Input
                          type="number"
                          value={issuedQty[c.product_id] ?? ""}
                          onChange={(e) => setIssuedQty((prev) => ({ ...prev, [c.product_id]: e.target.value }))}
                          className="h-7 w-28 text-right tabular-nums"
                          data-testid={`store-issued-qty-input-${i}`}
                        />
                      ) : (
                        <span className="tabular-nums">{formatQty(c.issued_qty)} {c.unit_of_measure || ""}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {isPending && !resultMessage && (
            <div className="space-y-2">
              {hasShortfall ? (
                <>
                  <p className="text-[11px] text-[#B54708]">One or more components are still short of the required quantity. Choose how to proceed:</p>
                  <div className="flex flex-wrap gap-2">
                    <Button disabled={submitting} onClick={() => submitIssue("proceed")} data-testid="store-submit-proceed-button">
                      Proceed with Partial Stock
                    </Button>
                    <Button variant="outline" disabled={submitting} onClick={() => submitIssue("send_to_planner")} data-testid="store-submit-send-to-planner-button">
                      Send to Requester for Approval
                    </Button>
                  </div>
                </>
              ) : (
                <Button disabled={submitting} onClick={() => submitIssue(null)} data-testid="store-submit-full-button">
                  Confirm Stock Fully Issued
                </Button>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
