import { useState, useEffect, useMemo, useCallback } from "react";
import { createPortal } from "react-dom";
import axios from "axios";
import { ArrowClockwise, CaretUp, CaretDown, MagnifyingGlass, Printer, X } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/components/ui/sonner";
import { useAuth } from "@/contexts/AuthContext";
import { RequestPrintSlip } from "@/components/RequestPrintSlip";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const STATUS_BADGE = {
  pending: { label: "Awaiting Store", tone: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]" },
  issuing: { label: "Issuing Stock...", tone: "bg-[#EFF8FF] text-[#0E7C86] border-[#B2DDFF]" },
  partial_pending_planner: { label: "Awaiting Your Approval", tone: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" },
  resolved_balance_pending: { label: "Balance Pending", tone: "bg-[#FEF6EE] text-[#B93815] border-[#F9DBAF]" },
  resolved: { label: "Resolved", tone: "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]" },
  cancelled: { label: "Cancelled", tone: "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]" },
};

// Only match against columns actually rendered for the current group-by
// level - JSON.stringify(row) previously also matched hidden internals
// (job_id, proposal id, warehouse locations/UUIDs), confusing users
// (testing agent iteration_104).
const SEARCH_FIELDS = {
  request: ["_id", "material_id", "site_id", "status"],
  item: ["request_id", "product_id", "description", "status"],
  product: ["product_id", "description"],
};

const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");
const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatDate = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "\u2014");

const SortTh = ({ label, field, sortField, sortDir, onSort }) => (
  <th
    onClick={() => onSort(field)}
    className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap cursor-pointer select-none"
    data-testid={`myreq-sort-${field}`}
  >
    <span className="inline-flex items-center gap-1">
      {label}
      {sortField === field && (sortDir === "asc" ? <CaretUp size={11} weight="bold" /> : <CaretDown size={11} weight="bold" />)}
    </span>
  </th>
);

export const MyStockRequestsTab = ({ actorName }) => {
  const { user } = useAuth();
  // Aug 2026, user's explicit ask: admin/super_admin should see EVERYONE's
  // stock requests here (not just their own, since they're not really
  // "requesters" in this tab's normal sense) with a Site filter to narrow
  // it down - a plain "user" keeps the original "only what I created" view.
  const isAdmin = user?.role === "admin" || user?.role === "super_admin";
  const [all, setAll] = useState([]);
  const [loading, setLoading] = useState(true);
  const [groupBy, setGroupBy] = useState("request");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [siteFilter, setSiteFilter] = useState("all");
  const [knownSites, setKnownSites] = useState([]);
  const [sortField, setSortField] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");
  // Sep 2026, user's explicit ask: a requester should be able to print
  // their own request as a paper slip for the store, without needing the
  // store_approval page permission - shares RequestPrintSlip with
  // StoreApprovalPage.js's detail view.
  const [printTarget, setPrintTarget] = useState(null);
  // Sep 9 2026, user's explicit ask: clicking a Request ID opens a full
  // multi-item detail popup (item-level table + totals), instead of the
  // row only ever showing a single Material/Qty summary.
  const [detailRequest, setDetailRequest] = useState(null);

  useEffect(() => {
    if (!printTarget) return;
    const t = setTimeout(() => window.print(), 50);
    return () => clearTimeout(t);
  }, [printTarget]);

  useEffect(() => {
    const clearPrintTarget = () => setPrintTarget(null);
    window.addEventListener("afterprint", clearPrintTarget);
    return () => window.removeEventListener("afterprint", clearPrintTarget);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests/journal`, {
        params: isAdmin ? {} : { requester: actorName },
      });
      setAll(data.requests);
    } catch {
      toast.error("Failed to load your stock requests");
    } finally {
      setLoading(false);
    }
  }, [actorName, isAdmin]);

  useEffect(() => {
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, [load]);

  useEffect(() => {
    if (!isAdmin) return;
    axios.get(`${API}/store-requests/known-sites`).then(({ data }) => setKnownSites(data.sites || [])).catch(() => {});
  }, [isAdmin]);

  const mine = useMemo(() => {
    if (isAdmin) {
      return siteFilter === "all" ? all : all.filter((r) => r.site_id === siteFilter);
    }
    // Bug fix (Aug 2026): backend now already scopes this to `actorName`
    // via ?requester=, bypassing the store site-binding restriction that
    // was wrongly blanking this tab out for requesters with no bound
    // sites. Keep this client-side filter too as a harmless double-check.
    const name = actorName.trim().toLowerCase();

    if (!name) return [];
    return all.filter((r) => (r.requester || "").trim().toLowerCase() === name);
  }, [all, actorName, isAdmin, siteFilter]);

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir("desc");
    }
  };

  const requestRows = useMemo(() => mine.map((r) => ({
    ...r,
    short_count: r.components.filter((c) => (c.shortfall || 0) > 0).length,
  })), [mine]);

  const itemRows = useMemo(() => mine.flatMap((r) => r.components.map((c) => {
    const issuedQty = c.issued_qty ?? 0;
    // A component the store hasn't acted on yet (issued_qty/shortfall
    // still null) has its FULL required_qty outstanding, not 0 - testing
    // agent iteration_104 found this under-reported outstanding demand.
    const shortfall = c.shortfall ?? Math.max(0, (c.required_qty || 0) - issuedQty);
    return {
      request_id: r._id, status: r.status, material_id: r.material_id, site_id: r.site_id,
      created_at: r.created_at, updated_at: r.updated_at,
      product_id: c.product_id, description: c.description, unit_of_measure: c.unit_of_measure,
      required_qty: c.required_qty, issued_qty: issuedQty, shortfall,
    };
  })), [mine]);

  const productRows = useMemo(() => {
    const map = new Map();
    itemRows.forEach((it) => {
      if (!map.has(it.product_id)) {
        map.set(it.product_id, {
          product_id: it.product_id, description: it.description, unit_of_measure: it.unit_of_measure,
          total_required: 0, total_issued: 0, total_shortfall: 0, requestSet: new Set(),
        });
      }
      const agg = map.get(it.product_id);
      agg.total_required += it.required_qty || 0;
      agg.total_issued += it.issued_qty || 0;
      agg.total_shortfall += it.shortfall || 0;
      agg.requestSet.add(it.request_id);
    });
    return Array.from(map.values()).map((a) => ({ ...a, request_count: a.requestSet.size }));
  }, [itemRows]);

  const activeRows = groupBy === "request" ? requestRows : groupBy === "item" ? itemRows : productRows;

  const filtered = useMemo(() => {
    let rows = activeRows;
    const term = search.trim().toLowerCase();
    if (term) {
      const fields = SEARCH_FIELDS[groupBy];
      rows = rows.filter((r) => fields.some((f) => String(r[f] ?? "").toLowerCase().includes(term)));
    }
    if (statusFilter !== "all" && groupBy !== "product") rows = rows.filter((r) => r.status === statusFilter);
    const dir = sortDir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = a[sortField], bv = b[sortField];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "string") return av.localeCompare(bv) * dir;
      return (av - bv) * dir;
    });
  }, [activeRows, search, statusFilter, sortField, sortDir, groupBy]);

  return (
    <div className="space-y-3" data-testid="my-stock-requests-tab">
      {printTarget && createPortal(<RequestPrintSlip request={printTarget} heading="Request For Store" />, document.body)}
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <Label className="text-xs font-bold text-[#344054]">Group By</Label>
          <Select value={groupBy} onValueChange={setGroupBy}>
            <SelectTrigger className="w-44 bg-white" data-testid="myreq-groupby-trigger"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="request" data-testid="myreq-groupby-request">Request Level</SelectItem>
              <SelectItem value="item" data-testid="myreq-groupby-item">Item Level</SelectItem>
              <SelectItem value="product" data-testid="myreq-groupby-product">Product Level</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs font-bold text-[#344054]">Search</Label>
          <div className="relative">
            <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search..." className="w-64 bg-white pl-7" data-testid="myreq-search-input" />
          </div>
        </div>
        {groupBy !== "product" && (
          <div>
            <Label className="text-xs font-bold text-[#344054]">Status</Label>
            <Select value={statusFilter} onValueChange={setStatusFilter}>
              <SelectTrigger className="w-44 bg-white" data-testid="myreq-status-filter-trigger"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all" data-testid="myreq-status-filter-all">All Statuses</SelectItem>
                {Object.entries(STATUS_BADGE).map(([key, v]) => (
                  <SelectItem key={key} value={key} data-testid={`myreq-status-filter-${key}`}>{v.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {isAdmin && (
          <div>
            <Label className="text-xs font-bold text-[#344054]">Site</Label>
            <Select value={siteFilter} onValueChange={setSiteFilter}>
              <SelectTrigger className="w-32 bg-white" data-testid="myreq-site-filter-trigger"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all" data-testid="myreq-site-filter-all">All Sites</SelectItem>
                {knownSites.map((s) => (
                  <SelectItem key={s} value={s} data-testid={`myreq-site-filter-${s}`}>{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        <Button variant="outline" onClick={load} data-testid="myreq-refresh-button">
          <ArrowClockwise size={14} className="mr-1.5" /> Refresh
        </Button>
        <span className="text-xs text-[#667085] ml-auto" data-testid="myreq-result-count">{filtered.length} row(s)</span>
      </div>

      <div className="overflow-x-auto bg-white border border-[#D0D5DD] rounded-sm">
        {loading ? (
          <p className="p-4 text-sm text-[#667085]">Loading...</p>
        ) : filtered.length === 0 ? (
          <p className="p-4 text-sm text-[#667085]" data-testid="myreq-empty-state">
            {isAdmin ? "No stock requests found." : `No stock requests found for "${actorName}".`}
          </p>
        ) : groupBy === "request" ? (
          <table className="w-full text-xs border-collapse">
            <thead><tr>
              <SortTh label="Request ID" field="_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Material" field="material_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Qty" field="quantity" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Site" field="site_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Status" field="status" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Short Components" field="short_count" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Requested At" field="created_at" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
            </tr></thead>
            <tbody>
              {filtered.map((r, i) => (
                <tr key={r._id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`myreq-request-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">
                    <button
                      type="button"
                      onClick={() => setDetailRequest(r)}
                      className="text-[#0E7C86] font-bold underline hover:text-[#0B5F67]"
                      data-testid={`myreq-request-id-link-${i}`}
                    >
                      {r._id}
                    </button>
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.material_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.quantity)} {formatUnit(r.unit_code)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{r.site_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5"><Badge className={`${STATUS_BADGE[r.status]?.tone} border`}>{STATUS_BADGE[r.status]?.label || r.status}</Badge></td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{r.short_count}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{formatDate(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : groupBy === "item" ? (
          <table className="w-full text-xs border-collapse">
            <thead><tr>
              <SortTh label="Request ID" field="request_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Component" field="product_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Required" field="required_qty" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Issued" field="issued_qty" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Shortfall" field="shortfall" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Status" field="status" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
            </tr></thead>
            <tbody>
              {filtered.map((it, i) => (
                <tr key={`${it.request_id}-${it.product_id}`} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`myreq-item-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{it.request_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{it.product_id}{it.description ? ` - ${it.description}` : ""}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.required_qty)} {formatUnit(it.unit_of_measure)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.issued_qty)} {formatUnit(it.unit_of_measure)}</td>
                  <td className={`border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums ${it.shortfall > 0 ? "text-[#B42318] font-bold" : ""}`}>{formatQty(it.shortfall)} {formatUnit(it.unit_of_measure)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5"><Badge className={`${STATUS_BADGE[it.status]?.tone} border`}>{STATUS_BADGE[it.status]?.label || it.status}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="w-full text-xs border-collapse">
            <thead><tr>
              <SortTh label="Component" field="product_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="# Requests" field="request_count" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Total Required" field="total_required" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Total Issued" field="total_issued" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortTh label="Outstanding Shortfall" field="total_shortfall" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
            </tr></thead>
            <tbody>
              {filtered.map((p, i) => (
                <tr key={p.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`myreq-product-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{p.product_id}{p.description ? ` - ${p.description}` : ""}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{p.request_count}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(p.total_required)} {formatUnit(p.unit_of_measure)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(p.total_issued)} {formatUnit(p.unit_of_measure)}</td>
                  <td className={`border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums ${p.total_shortfall > 0 ? "text-[#B42318] font-bold" : ""}`}>{formatQty(p.total_shortfall)} {formatUnit(p.unit_of_measure)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {detailRequest && (
        <RequestDetailDialog request={detailRequest} onClose={() => setDetailRequest(null)} onPrint={(r) => { setDetailRequest(null); setPrintTarget(r); }} />
      )}
    </div>
  );
};

// Sep 9 2026, user's explicit ask: clicking a Request ID shows the FULL
// request (header info + an item-level table with Requested/Store
// Issued/Pending Qty per item, plus grand totals) - not just the single
// Material/Qty summary the request-level row shows. Store Issued Qty is
// `c.issued_qty` - the ACTUAL quantity store_approval_service recorded
// against a real Goods Movement, never just assumed equal to what was
// requested (a component the store hasn't acted on yet has issued_qty
// still `null`, shown as Pending here, not silently treated as 0-vs-0).
const DETAIL_STATUS_LABEL = { ...STATUS_BADGE };

const RequestDetailDialog = ({ request: r, onClose, onPrint }) => {
  const items = (r.components || []).map((c) => {
    const issuedQty = c.issued_qty ?? 0;
    const pendingQty = Math.max(0, (c.required_qty || 0) - issuedQty);
    return { ...c, issuedQty, pendingQty };
  });
  const totals = items.reduce((acc, it) => ({
    requested: acc.requested + (it.required_qty || 0),
    issued: acc.issued + it.issuedQty,
    pending: acc.pending + it.pendingQty,
  }), { requested: 0, issued: 0, pending: 0 });

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="rounded-sm max-w-4xl" data-testid="myreq-detail-dialog">
        <DialogHeader>
          <DialogTitle className="font-heading flex items-center justify-between gap-2 pr-6">
            <span className="font-mono">{r._id}</span>
            <Badge className={`${DETAIL_STATUS_LABEL[r.status]?.tone} border`}>{DETAIL_STATUS_LABEL[r.status]?.label || r.status}</Badge>
          </DialogTitle>
        </DialogHeader>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs bg-[#F9FAFB] border border-[#D0D5DD] rounded-sm p-3" data-testid="myreq-detail-header">
          <div><span className="text-[#667085] block">Request Date/Time</span><span className="font-bold text-[#1D2939]">{formatDate(r.created_at)}</span></div>
          <div><span className="text-[#667085] block">Requested By</span><span className="font-bold text-[#1D2939]">{r.requester}</span></div>
          <div><span className="text-[#667085] block">Site/Plant</span><span className="font-bold text-[#1D2939]">{r.site_id}</span></div>
          <div><span className="text-[#667085] block">Total Items</span><span className="font-bold text-[#1D2939]" data-testid="myreq-detail-total-items">{items.length}</span></div>
        </div>

        <div className="overflow-x-auto border border-[#D0D5DD] rounded-sm">
          <table className="w-full text-xs border-collapse">
            <thead><tr>
              {["Item Code", "Material", "Requested Qty", "Store Issued Qty", "Pending Qty", "UOM", "Status"].map((h) => (
                <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {items.map((it, i) => (
                <tr key={it.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`myreq-detail-item-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{it.product_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "\u2014"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.required_qty)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.issuedQty)}</td>
                  <td className={`border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums ${it.pendingQty > 0 ? "text-[#B42318] font-bold" : ""}`}>{formatQty(it.pendingQty)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">{formatUnit(it.unit_of_measure)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    {it.pendingQty <= 0 ? "Issued" : it.issuedQty > 0 ? "Partially Issued" : "Pending"}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="bg-[#EAECF0] font-bold">
                <td colSpan={2} className="border border-[#D0D5DD] px-2 py-1.5 text-right">Totals</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums" data-testid="myreq-detail-total-requested">{formatQty(totals.requested)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums" data-testid="myreq-detail-total-issued">{formatQty(totals.issued)}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums" data-testid="myreq-detail-total-pending">{formatQty(totals.pending)}</td>
                <td className="border border-[#D0D5DD]" colSpan={2}></td>
              </tr>
            </tfoot>
          </table>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onPrint(r)} data-testid="myreq-detail-print-button">
            <Printer size={13} className="mr-1.5" /> Print
          </Button>
          <Button variant="outline" onClick={onClose} data-testid="myreq-detail-close-button">
            <X size={13} className="mr-1.5" /> Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};
