import { useState, useEffect, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import axios from "axios";
import { ArrowClockwise, MagnifyingGlass, Printer, Plus, Trash, X, ArrowLeft, PencilSimple } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/components/ui/sonner";
import { useAuth } from "@/contexts/AuthContext";
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
  <Badge className={`${RETURN_STATUS_BADGE[status]?.tone || ""} border`} data-testid={`return-status-badge-${status}`}>
    {RETURN_STATUS_BADGE[status]?.label || status}
  </Badge>
);

// Sep 9 2026, new "Return to Store" feature - lets Production either
// return material previously issued against a real Stock Request
// (source of truth for Issued Qty is the store_requests doc itself, see
// store_return_service.get_issued_items_for_request), or log a Manual
// Return with no prior request. Both submit to the SAME store_returns
// collection/queue Store works from (StoreReturnsPanel.jsx).
export const ReturnToStoreTab = ({ actorName }) => {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin" || user?.role === "super_admin";
  const [mode, setMode] = useState("list"); // list | pick-request | against-items | manual
  const [returns, setReturns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [reasons, setReasons] = useState([]);
  const [selectedRequestId, setSelectedRequestId] = useState(null);
  const [editingReturn, setEditingReturn] = useState(null);
  const [detailReturn, setDetailReturn] = useState(null);
  const [printTarget, setPrintTarget] = useState(null);

  const loadReturns = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-returns/journal`, { params: isAdmin ? {} : { requester: actorName } });
      setReturns(data.returns || []);
    } catch {
      toast.error("Failed to load your return requests");
    } finally {
      setLoading(false);
    }
  }, [actorName, isAdmin]);

  useEffect(() => {
    axios.get(`${API}/store-returns/reasons`).then(({ data }) => setReasons(data.reasons || [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (mode !== "list") return;
    loadReturns();
    const interval = setInterval(loadReturns, 10000);
    return () => clearInterval(interval);
  }, [mode, loadReturns]);

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

  const backToList = () => { setMode("list"); setSelectedRequestId(null); setEditingReturn(null); };
  const finishFlow = () => { backToList(); loadReturns(); };

  const editRejected = (ret) => {
    setEditingReturn(ret);
    setSelectedRequestId(ret.original_request_id || null);
    setMode(ret.return_type === "against_request" ? "against-items" : "manual");
  };

  return (
    <div className="space-y-3" data-testid="return-to-store-tab">
      {printTarget && createPortal(<ReturnPrintSlip ret={printTarget} />, document.body)}

      {mode === "list" && (
        <>
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => setMode("pick-request")} className="bg-[#0E7C86] hover:bg-[#0B5F67]" data-testid="return-start-against-request">
              <Plus size={14} className="mr-1.5" /> A. Return Against Request
            </Button>
            <Button onClick={() => setMode("manual")} variant="outline" data-testid="return-start-manual">
              <Plus size={14} className="mr-1.5" /> B. Manual Return Request
            </Button>
            <Button variant="outline" onClick={loadReturns} className="ml-auto" data-testid="return-list-refresh-button">
              <ArrowClockwise size={14} className="mr-1.5" /> Refresh
            </Button>
          </div>
          <ReturnList returns={returns} loading={loading} isAdmin={isAdmin} onView={setDetailReturn} onPrint={setPrintTarget} onEditRejected={editRejected} />
        </>
      )}

      {mode === "pick-request" && (
        <PickRequestStep actorName={actorName} isAdmin={isAdmin} onPick={(id) => { setSelectedRequestId(id); setMode("against-items"); }} onCancel={backToList} />
      )}

      {mode === "against-items" && (
        <AgainstRequestItemsStep
          requestId={selectedRequestId} actorName={actorName} reasons={reasons}
          editingReturn={editingReturn} onDone={finishFlow} onCancel={backToList}
        />
      )}

      {mode === "manual" && (
        <ManualReturnStep actorName={actorName} reasons={reasons} editingReturn={editingReturn} onDone={finishFlow} onCancel={backToList} />
      )}

      {detailReturn && (
        <ReturnDetailDialog ret={detailReturn} onClose={() => setDetailReturn(null)} onPrint={() => { setPrintTarget(detailReturn); setDetailReturn(null); }} />
      )}
    </div>
  );
};

const ReturnList = ({ returns, loading, isAdmin, onView, onPrint, onEditRejected }) => {
  const [search, setSearch] = useState("");
  const term = search.trim().toLowerCase();
  const filtered = term
    ? returns.filter((r) => [r._id, r.original_request_id, r.site_id, r.requester, ...(r.items || []).map((i) => i.product_id)].filter(Boolean).some((f) => String(f).toLowerCase().includes(term)))
    : returns;

  return (
    <div className="space-y-2">
      <div className="flex items-end gap-3">
        <div>
          <Label className="text-xs font-bold text-[#344054]">Search</Label>
          <div className="relative">
            <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search return ID, item, site..." className="w-64 bg-white pl-7" data-testid="return-list-search-input" />
          </div>
        </div>
        <span className="text-xs text-[#667085] ml-auto" data-testid="return-list-result-count">{filtered.length} row(s)</span>
      </div>
      <div className="overflow-x-auto bg-white border border-[#D0D5DD] rounded-sm">
        {loading ? (
          <p className="p-4 text-sm text-[#667085]">Loading...</p>
        ) : filtered.length === 0 ? (
          <p className="p-4 text-sm text-[#667085]" data-testid="return-list-empty-state">No return requests found.</p>
        ) : (
          <table className="w-full text-xs border-collapse">
            <thead><tr>
              {["Return ID", "Return Type", "Original Request", "Item(s)", "Qty", "Site", "Reason", "Status", "Action"].map((h) => (
                <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {filtered.map((r, i) => (
                <tr key={r._id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`return-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    <button type="button" onClick={() => onView(r)} className="font-mono text-[#0E7C86] font-bold underline hover:text-[#0B5F67]" data-testid={`return-id-link-${i}`}>
                      {r._id}
                    </button>
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.return_type === "against_request" ? "Against Request" : "Manual"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{r.original_request_id || "\u2014"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{(r.items || []).map((it) => it.product_id).join(", ")}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty((r.items || []).reduce((s, it) => s + (it.return_qty || 0), 0))}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.site_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{(r.items || [])[0]?.reason_label}{(r.items || []).length > 1 ? " +" : ""}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5"><StatusBadge status={r.status} /></td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    <div className="flex gap-1">
                      {r.status === "rejected" && (
                        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={() => onEditRejected(r)} data-testid={`return-edit-resubmit-button-${i}`}>
                          <PencilSimple size={11} className="mr-1" /> Edit & Resubmit
                        </Button>
                      )}
                      <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={() => onPrint(r)} data-testid={`return-print-button-${i}`}>
                        <Printer size={11} className="mr-1" /> Print
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};

const StepShell = ({ title, onCancel, children }) => (
  <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-4" data-testid="return-step-shell">
    <div className="flex items-center justify-between">
      <h3 className="font-heading text-sm font-bold text-[#1D2939] uppercase tracking-wide">{title}</h3>
      <Button variant="outline" size="sm" onClick={onCancel} data-testid="return-step-cancel-button">
        <ArrowLeft size={13} className="mr-1.5" /> Cancel
      </Button>
    </div>
    {children}
  </div>
);

// -------------------- A. Return Against Request - Step 1 --------------------
const PickRequestStep = ({ actorName, isAdmin, onPick, onCancel }) => {
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    axios.get(`${API}/store-returns/previous-requests`, { params: { requester: actorName } })
      .then(({ data }) => setRequests(data.requests || []))
      .catch(() => toast.error("Failed to load your previous stock requests"))
      .finally(() => setLoading(false));
  }, [actorName]);

  return (
    <StepShell title="Step 1 - Select Previous Request" onCancel={onCancel}>
      {loading ? (
        <p className="text-sm text-[#667085]">Loading...</p>
      ) : requests.length === 0 ? (
        <p className="text-sm text-[#667085]" data-testid="pick-request-empty-state">No previous stock requests with issued stock found.</p>
      ) : (
        <table className="w-full text-xs border-collapse">
          <thead><tr>
            {["Request ID", "Date", "Site", ""].map((h) => (
              <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase">{h}</th>
            ))}
          </tr></thead>
          <tbody>
            {requests.map((r, i) => (
              <tr key={r._id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`pick-request-row-${i}`}>
                <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{r._id}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{formatDate(r.created_at)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{r.site_id}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">
                  <Button size="sm" className="h-6 px-2 text-[11px] bg-[#0E7C86] hover:bg-[#0B5F67]" onClick={() => onPick(r._id)} data-testid={`pick-request-select-button-${i}`}>Select</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </StepShell>
  );
};

// -------------------- A. Return Against Request - Step 2 --------------------
const AgainstRequestItemsStep = ({ requestId, actorName, reasons, editingReturn, onDone, onCancel }) => {
  const [info, setInfo] = useState(null);
  const [loading, setLoading] = useState(true);
  const [rows, setRows] = useState({}); // product_id -> {return_qty, reason_code, remarks}
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    axios.get(`${API}/store-returns/against-request/${requestId}`)
      .then(({ data }) => {
        setInfo(data);
        const initial = {};
        (data.items || []).forEach((it) => {
          const existing = editingReturn?.items?.find((e) => e.product_id === it.product_id);
          initial[it.product_id] = {
            return_qty: existing ? String(existing.return_qty) : "",
            reason_code: existing?.reason_code || "",
            remarks: existing?.remarks || "",
          };
        });
        setRows(initial);
      })
      .catch(() => toast.error("Failed to load issued items for this request"))
      .finally(() => setLoading(false));
  }, [requestId, editingReturn]);

  const setRow = (productId, patch) => setRows((prev) => ({ ...prev, [productId]: { ...prev[productId], ...patch } }));

  const submit = async () => {
    const items = Object.entries(rows)
      .filter(([, v]) => Number(v.return_qty) > 0)
      .map(([product_id, v]) => ({ product_id, return_qty: Number(v.return_qty), reason_code: v.reason_code, remarks: v.remarks }));
    if (items.length === 0) {
      toast.error("Enter a Return Qty for at least one item");
      return;
    }
    if (items.some((it) => !it.reason_code)) {
      toast.error("Select a Return Reason for every item you're returning");
      return;
    }
    if (items.some((it) => it.reason_code === "other" && !(it.remarks || "").trim())) {
      toast.error("Remarks are required when reason is 'Other'");
      return;
    }
    setSubmitting(true);
    try {
      if (editingReturn) {
        await axios.put(`${API}/store-returns/${editingReturn._id}`, { return_type: "against_request", original_request_id: requestId, items, actor: actorName });
        toast.success(`${editingReturn._id} resubmitted to Store`);
      } else {
        const { data } = await axios.post(`${API}/store-returns`, { return_type: "against_request", original_request_id: requestId, items, actor: actorName });
        toast.success(`Return request ${data._id} submitted to Store`);
      }
      onDone();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to submit return request");
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <StepShell title="Step 2 - Items Actually Issued" onCancel={onCancel}><p className="text-sm text-[#667085]">Loading...</p></StepShell>;
  if (!info) return null;

  return (
    <StepShell title={`Step 2 - Items Actually Issued (Request ${requestId})`} onCancel={onCancel}>
      <table className="w-full text-xs border-collapse">
        <thead><tr>
          {["Item Code", "Material", "Requested Qty", "Issued Qty", "Already Returned", "Return Qty", "Return Reason", "Remarks"].map((h) => (
            <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {info.items.map((it, i) => {
            const row = rows[it.product_id] || {};
            return (
              <tr key={it.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`against-item-row-${i}`}>
                <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{it.product_id}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "\u2014"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.required_qty)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.issued_qty)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.already_returned_qty)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">
                  <Input
                    type="number" min={0} max={it.available_to_return} step="any"
                    value={row.return_qty || ""} onChange={(e) => setRow(it.product_id, { return_qty: e.target.value })}
                    className="h-7 w-24 text-right" placeholder={`max ${formatQty(it.available_to_return)}`}
                    data-testid={`against-item-return-qty-${i}`}
                  />
                </td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">
                  <Select value={row.reason_code || ""} onValueChange={(v) => setRow(it.product_id, { reason_code: v })}>
                    <SelectTrigger className="h-7 w-40 bg-white" data-testid={`against-item-reason-${i}`}><SelectValue placeholder="Select reason" /></SelectTrigger>
                    <SelectContent>
                      {reasons.map((r) => <SelectItem key={r.code} value={r.code}>{r.label}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">
                  {row.reason_code === "other" && (
                    <Input value={row.remarks || ""} onChange={(e) => setRow(it.product_id, { remarks: e.target.value })} className="h-7 w-40" placeholder="Remarks (required)" data-testid={`against-item-remarks-${i}`} />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="flex justify-end">
        <Button onClick={submit} disabled={submitting} className="bg-[#0E7C86] hover:bg-[#0B5F67]" data-testid="against-items-submit-button">
          {submitting ? "Submitting..." : editingReturn ? "Resubmit Return Request" : "Submit Return Request"}
        </Button>
      </div>
    </StepShell>
  );
};

// -------------------- B. Manual Return Request --------------------
const ProductAutocomplete = ({ value, onChange, testId }) => {
  const [query, setQuery] = useState(value || "");
  const [suggestions, setSuggestions] = useState([]);
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef(null);
  const debounceRef = useRef(null);

  useEffect(() => setQuery(value || ""), [value]);

  useEffect(() => {
    const onOutside = (e) => { if (wrapperRef.current && !wrapperRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, []);

  const handleInput = (v) => {
    setQuery(v);
    onChange({ product_id: v, description: undefined });
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (v.trim().length < 2) { setSuggestions([]); return; }
    debounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/products/search`, { params: { q: v.trim(), limit: 8 } });
        setSuggestions(data);
        setOpen(true);
      } catch { setSuggestions([]); }
    }, 250);
  };

  return (
    <div className="relative" ref={wrapperRef}>
      <Input value={query} onChange={(e) => handleInput(e.target.value)} placeholder="Item Code" className="h-7 w-36" data-testid={testId} />
      {open && suggestions.length > 0 && (
        <div className="absolute z-20 mt-1 w-64 bg-white border border-[#D0D5DD] rounded-sm shadow-md max-h-48 overflow-auto">
          {suggestions.map((s) => (
            <button
              key={s.product_id} type="button"
              className="block w-full text-left px-2 py-1.5 text-xs hover:bg-[#F2F4F7]"
              onClick={() => { setQuery(s.product_id); onChange({ product_id: s.product_id, description: s.description }); setOpen(false); }}
              data-testid={`${testId}-suggestion-${s.product_id}`}
            >
              <span className="font-mono font-bold">{s.product_id}</span>{s.description ? ` - ${s.description}` : ""}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

const EMPTY_MANUAL_ROW = { product_id: "", description: "", qty: "", unit_of_measure: "EA", reason_code: "", remarks: "" };

const ManualReturnStep = ({ actorName, reasons, editingReturn, onDone, onCancel }) => {
  const [sites, setSites] = useState([]);
  const [siteId, setSiteId] = useState(editingReturn?.site_id || "");
  const [rows, setRows] = useState(
    editingReturn?.items?.length
      ? editingReturn.items.map((it) => ({ product_id: it.product_id, description: it.description || "", qty: String(it.return_qty), unit_of_measure: it.unit_of_measure || "EA", reason_code: it.reason_code, remarks: it.remarks || "" }))
      : [{ ...EMPTY_MANUAL_ROW }]
  );
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    axios.get(`${API}/store-requests/known-sites`).then(({ data }) => setSites(data.sites || [])).catch(() => {});
  }, []);

  const setRow = (idx, patch) => setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  const addRow = () => setRows((prev) => [...prev, { ...EMPTY_MANUAL_ROW }]);
  const removeRow = (idx) => setRows((prev) => prev.filter((_, i) => i !== idx));

  const submit = async () => {
    if (!siteId) { toast.error("Select a Site/Plant"); return; }
    if (rows.some((r) => !r.product_id.trim() || !Number(r.qty) || Number(r.qty) <= 0 || !r.reason_code)) {
      toast.error("Every row needs an Item Code, a Qty greater than 0, and a Return Reason");
      return;
    }
    if (rows.some((r) => r.reason_code === "other" && !(r.remarks || "").trim())) {
      toast.error("Remarks are required when reason is 'Other'");
      return;
    }
    const items = rows.map((r) => ({ product_id: r.product_id.trim(), description: r.description, qty: Number(r.qty), unit_of_measure: r.unit_of_measure || "EA", reason_code: r.reason_code, remarks: r.remarks }));
    setSubmitting(true);
    try {
      if (editingReturn) {
        await axios.put(`${API}/store-returns/${editingReturn._id}`, { return_type: "manual", site_id: siteId, items, actor: actorName });
        toast.success(`${editingReturn._id} resubmitted to Store`);
      } else {
        const { data } = await axios.post(`${API}/store-returns`, { return_type: "manual", site_id: siteId, items, actor: actorName });
        toast.success(`Return request ${data._id} submitted to Store`);
      }
      onDone();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to submit return request");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <StepShell title="Manual Return Request" onCancel={onCancel}>
      <div>
        <Label className="text-xs font-bold text-[#344054]">Site / Plant</Label>
        <Select value={siteId} onValueChange={setSiteId}>
          <SelectTrigger className="w-40 bg-white" data-testid="manual-return-site-select"><SelectValue placeholder="Select site" /></SelectTrigger>
          <SelectContent>
            {sites.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      <table className="w-full text-xs border-collapse">
        <thead><tr>
          {["Item Code", "Qty", "UOM", "Return Reason", "Remarks", ""].map((h) => (
            <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`manual-return-row-${i}`}>
              <td className="border border-[#D0D5DD] px-2 py-1.5">
                <ProductAutocomplete value={r.product_id} onChange={(v) => setRow(i, v)} testId={`manual-return-item-code-${i}`} />
              </td>
              <td className="border border-[#D0D5DD] px-2 py-1.5">
                <Input type="number" min={0} step="any" value={r.qty} onChange={(e) => setRow(i, { qty: e.target.value })} className="h-7 w-20 text-right" data-testid={`manual-return-qty-${i}`} />
              </td>
              <td className="border border-[#D0D5DD] px-2 py-1.5">
                <Input value={r.unit_of_measure} onChange={(e) => setRow(i, { unit_of_measure: e.target.value })} className="h-7 w-16" data-testid={`manual-return-uom-${i}`} />
              </td>
              <td className="border border-[#D0D5DD] px-2 py-1.5">
                <Select value={r.reason_code} onValueChange={(v) => setRow(i, { reason_code: v })}>
                  <SelectTrigger className="h-7 w-40 bg-white" data-testid={`manual-return-reason-${i}`}><SelectValue placeholder="Select reason" /></SelectTrigger>
                  <SelectContent>
                    {reasons.map((rs) => <SelectItem key={rs.code} value={rs.code}>{rs.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </td>
              <td className="border border-[#D0D5DD] px-2 py-1.5">
                {r.reason_code === "other" && (
                  <Input value={r.remarks} onChange={(e) => setRow(i, { remarks: e.target.value })} className="h-7 w-40" placeholder="Remarks (required)" data-testid={`manual-return-remarks-${i}`} />
                )}
              </td>
              <td className="border border-[#D0D5DD] px-2 py-1.5">
                {rows.length > 1 && (
                  <button type="button" onClick={() => removeRow(i)} className="text-[#B42318]" data-testid={`manual-return-remove-row-${i}`}><Trash size={13} /></button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex justify-between">
        <Button variant="outline" size="sm" onClick={addRow} data-testid="manual-return-add-item-button">
          <Plus size={13} className="mr-1.5" /> Add Item
        </Button>
        <Button onClick={submit} disabled={submitting} className="bg-[#0E7C86] hover:bg-[#0B5F67]" data-testid="manual-return-submit-button">
          {submitting ? "Submitting..." : editingReturn ? "Resubmit Return Request" : "Submit Return Request"}
        </Button>
      </div>
    </StepShell>
  );
};

// -------------------- Return Detail Dialog --------------------
export const ReturnDetailDialog = ({ ret, onClose, onPrint }) => (
  <Dialog open onOpenChange={(o) => !o && onClose()}>
    <DialogContent className="rounded-sm max-w-4xl" data-testid="return-detail-dialog">
      <DialogHeader>
        <DialogTitle className="font-heading flex items-center justify-between gap-2 pr-6">
          <span className="font-mono">{ret._id}</span>
          <StatusBadge status={ret.status} />
        </DialogTitle>
      </DialogHeader>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs bg-[#F9FAFB] border border-[#D0D5DD] rounded-sm p-3">
        <div><span className="text-[#667085] block">Return Type</span><span className="font-bold text-[#1D2939]">{ret.return_type === "against_request" ? "Against Request" : "Manual"}</span></div>
        <div><span className="text-[#667085] block">Original Request</span><span className="font-bold text-[#1D2939] font-mono">{ret.original_request_id || "\u2014"}</span></div>
        <div><span className="text-[#667085] block">Requested By</span><span className="font-bold text-[#1D2939]">{ret.requester}</span></div>
        <div><span className="text-[#667085] block">Site</span><span className="font-bold text-[#1D2939]">{ret.site_id}</span></div>
        <div><span className="text-[#667085] block">Date/Time</span><span className="font-bold text-[#1D2939]">{formatDate(ret.created_at)}</span></div>
        {ret.status === "rejected" && ret.rejection_reason && (
          <div className="col-span-2 sm:col-span-3"><span className="text-[#B42318] block">Rejection Reason</span><span className="font-bold text-[#B42318]">{ret.rejection_reason}</span></div>
        )}
      </div>
      <table className="w-full text-xs border-collapse">
        <thead><tr>
          {["Item Code", "Material", "Return Qty", "UOM", "Reason", "Remarks", "SAP Goods Movement"].map((h) => (
            <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {(ret.items || []).map((it, i) => (
            <tr key={`${it.product_id}-${i}`} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`return-detail-item-row-${i}`}>
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
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onPrint} data-testid="return-detail-print-button"><Printer size={13} className="mr-1.5" /> Print</Button>
        <Button variant="outline" onClick={onClose} data-testid="return-detail-close-button"><X size={13} className="mr-1.5" /> Close</Button>
      </div>
    </DialogContent>
  </Dialog>
);
