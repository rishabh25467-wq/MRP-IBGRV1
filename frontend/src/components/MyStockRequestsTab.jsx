import { useState, useEffect, useMemo, useCallback } from "react";
import axios from "axios";
import { ArrowClockwise, CaretUp, CaretDown, MagnifyingGlass } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "@/components/ui/sonner";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const STATUS_BADGE = {
  pending: { label: "Awaiting Store", tone: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]" },
  issuing: { label: "Issuing Stock...", tone: "bg-[#EFF8FF] text-[#0E7C86] border-[#B2DDFF]" },
  partial_pending_planner: { label: "Awaiting Your Approval", tone: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" },
  resolved_balance_pending: { label: "Balance Pending", tone: "bg-[#FEF6EE] text-[#B93815] border-[#F9DBAF]" },
  resolved: { label: "Resolved", tone: "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]" },
  cancelled: { label: "Cancelled", tone: "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]" },
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
  const [all, setAll] = useState([]);
  const [loading, setLoading] = useState(true);
  const [groupBy, setGroupBy] = useState("request");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortField, setSortField] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests/journal`);
      setAll(data.requests);
    } catch {
      toast.error("Failed to load your stock requests");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, [load]);

  const mine = useMemo(() => {
    const name = actorName.trim().toLowerCase();
    if (!name) return [];
    return all.filter((r) => (r.requester || "").trim().toLowerCase() === name);
  }, [all, actorName]);

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

  const itemRows = useMemo(() => mine.flatMap((r) => r.components.map((c) => ({
    request_id: r._id, status: r.status, material_id: r.material_id, site_id: r.site_id,
    created_at: r.created_at, updated_at: r.updated_at,
    product_id: c.product_id, description: c.description, unit_of_measure: c.unit_of_measure,
    required_qty: c.required_qty, issued_qty: c.issued_qty || 0, shortfall: c.shortfall || 0,
  }))), [mine]);

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
    if (term) rows = rows.filter((r) => JSON.stringify(r).toLowerCase().includes(term));
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

  if (!actorName.trim()) {
    return (
      <div className="bg-white border border-[#D0D5DD] rounded-sm p-6 text-center text-sm text-[#667085]" data-testid="myreq-no-name">
        Enter your name in the "Your name" field at the top to see the stock requests you've raised.
      </div>
    );
  }

  return (
    <div className="space-y-3" data-testid="my-stock-requests-tab">
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
        <Button variant="outline" onClick={load} data-testid="myreq-refresh-button">
          <ArrowClockwise size={14} className="mr-1.5" /> Refresh
        </Button>
        <span className="text-xs text-[#667085] ml-auto" data-testid="myreq-result-count">{filtered.length} row(s)</span>
      </div>

      <div className="overflow-x-auto bg-white border border-[#D0D5DD] rounded-sm">
        {loading ? (
          <p className="p-4 text-sm text-[#667085]">Loading...</p>
        ) : filtered.length === 0 ? (
          <p className="p-4 text-sm text-[#667085]" data-testid="myreq-empty-state">No stock requests found for "{actorName}".</p>
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
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono">{r._id}</td>
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
    </div>
  );
};
