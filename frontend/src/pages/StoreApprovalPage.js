import { useState, useEffect, useCallback, useMemo } from "react";
import axios from "axios";
import "@/App.css";
import { Package, ArrowLeft, ArrowClockwise, WarningCircle, CaretUp, CaretDown, MagnifyingGlass, DownloadSimple } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Toaster, toast } from "@/components/ui/sonner";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const STORE_NAME_KEY = "storeApprovalActorName";

const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));

// SAP's own BOM data returns "MASS" as a dimension/QuantityTypeCode label,
// not a real unit - matches the same display-only fix on Production
// Confirmation/Create Order screens. Internal values (unit_code sent back
// to the API, CSV export raw data) are untouched.
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");

// Aging (Aug 2026, user's explicit ask): "time since requested" text +
// severity tier, reused for the Pending Queue's live badge and the
// Movement History report's "Age at Issue" column/CSV export.
const ageParts = (fromIso, toIso) => {
  if (!fromIso) return null;
  const ms = (toIso ? new Date(toIso) : new Date()) - new Date(fromIso);
  if (Number.isNaN(ms) || ms < 0) return null;
  const mins = Math.floor(ms / 60000);
  const hours = Math.floor(mins / 60);
  const days = Math.floor(hours / 24);
  const label = days > 0 ? `${days}d ${hours % 24}h` : hours > 0 ? `${hours}h ${mins % 60}m` : `${mins}m`;
  const tier = days > 0 ? "overdue" : hours >= 4 ? "warn" : "fresh";
  return { label, tier, hours: ms / 3600000 };
};

const AGE_TIER_CLASS = { fresh: "text-[#027A48]", warn: "text-[#B54708] font-bold", overdue: "text-[#B42318] font-bold" };

const AgeBadge = ({ fromIso, toIso, testId }) => {
  const age = ageParts(fromIso, toIso);
  if (!age) return <span className="text-[#98A2B3]">\u2014</span>;
  return <span className={AGE_TIER_CLASS[age.tier]} data-testid={testId}>{age.label}{!toIso ? " ago" : ""}</span>;
};

const toCsv = (rows, columns) => {
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const header = columns.map((c) => esc(c.label)).join(",");
  const body = rows.map((r) => columns.map((c) => esc(c.get(r))).join(",")).join("\n");
  return `${header}\n${body}`;
};

const downloadCsv = (filename, csv) => {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
};

const LocationBreakdown = ({ locations, unit }) => {
  if (!locations || locations.length === 0) {
    return <span className="text-[#98A2B3]">no stock at this site</span>;
  }
  return (
    <div className="space-y-0.5">
      {locations.map((loc, i) => {
        // Requests created before the warehouse/stock_status fields were
        // added froze the OLD {site, qty} shape into their doc forever -
        // fall back to the (unscoped) site code instead of a confusing
        // "Unknown Warehouse" for those legacy, already-open requests.
        const label = loc.warehouse || (loc.site ? loc.site.split("-").pop() : "Unknown Warehouse");
        return (
          <div key={i}>
            <span className="text-[#667085]">{label}{loc.stock_status ? ` (${loc.stock_status})` : ""}:</span> {formatQty(loc.qty)} {formatUnit(unit)}
          </div>
        );
      })}
    </div>
  );
};

const STATUS_BADGE = {
  pending: { label: "Awaiting Store", tone: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]" },
  partial_pending_planner: { label: "Awaiting Requester", tone: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" },
  resolved: { label: "Resolved", tone: "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]" },
  cancelled: { label: "Cancelled", tone: "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]" },
};

// Free-text search across every field that matters, not just Material -
// user's explicit ask ("search by any field").
const matchesSearch = (r, term) => {
  if (!term) return true;
  const haystack = [
    r._id, r.issue_id, r.material_id, r.site_id, r.requester, r.production_proposal_id, r.status,
    r.store_actor, r.planner_actor,
    ...(r.components || []).flatMap((c) => [c.product_id, c.description]),
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(term.toLowerCase());
};

const SortableHeader = ({ label, field, sortField, sortDir, onSort }) => (
  <th
    className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer select-none"
    onClick={() => onSort(field)}
    data-testid={`store-journal-sort-${field}`}
  >
    <span className="inline-flex items-center gap-1">
      {label}
      {sortField === field && (sortDir === "asc" ? <CaretUp size={11} weight="bold" /> : <CaretDown size={11} weight="bold" />)}
    </span>
  </th>
);

export default function StoreApprovalPage() {
  const [storeName, setStoreName] = useState(() => localStorage.getItem(STORE_NAME_KEY) || "");
  const [viewMode, setViewMode] = useState("queue"); // "queue" | "journal"
  const [requests, setRequests] = useState([]);
  const [journalRequests, setJournalRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [issuedQty, setIssuedQty] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [submitElapsed, setSubmitElapsed] = useState(0);
  const [resultMessage, setResultMessage] = useState(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [userSearch, setUserSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [siteFilter, setSiteFilter] = useState("all");
  const [sortField, setSortField] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  useEffect(() => localStorage.setItem(STORE_NAME_KEY, storeName), [storeName]);

  // Ticks while a Goods Movement POST is in flight (backend now retries up
  // to 3x with a 5s backoff on transient SAP errors, so this single
  // request can occasionally take 30-70s - user's explicit ask was to show
  // a progress bar + message so it never looks frozen the way "Failed -
  // Radish QMS/SAP unavailable" used to).
  useEffect(() => {
    if (!submitting) {
      setSubmitElapsed(0);
      return;
    }
    const interval = setInterval(() => setSubmitElapsed((s) => s + 1), 1000);
    return () => clearInterval(interval);
  }, [submitting]);

  const submitProgressMessage = submitElapsed < 5
    ? "Recording issued quantities and posting the SAP Goods Movement..."
    : submitElapsed < 20
    ? "Still working - SAP is taking a little longer than usual to confirm the movement..."
    : "Almost there - retrying once more with SAP before giving up...";

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

  const loadJournal = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests/journal`);
      setJournalRequests(data.requests);
    } catch {
      toast.error("Failed to load the requests journal");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const refresh = () => (viewMode === "queue" ? loadRequests() : loadJournal());
    refresh();
    const interval = setInterval(refresh, 8000);
    return () => clearInterval(interval);
  }, [viewMode, loadRequests, loadJournal]);

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(field === "created_at" ? "desc" : "asc");
    }
  };

  const rawList = viewMode === "queue" ? requests : journalRequests;
  const siteOptions = useMemo(() => Array.from(new Set(rawList.map((r) => r.site_id).filter(Boolean))).sort(), [rawList]);

  const displayedRequests = useMemo(() => {
    let list = rawList.filter((r) => matchesSearch(r, searchTerm));
    if (statusFilter !== "all") list = list.filter((r) => r.status === statusFilter);
    if (siteFilter !== "all") list = list.filter((r) => r.site_id === siteFilter);
    const dir = sortDir === "asc" ? 1 : -1;
    list = [...list].sort((a, b) => {
      let va = a[sortField];
      let vb = b[sortField];
      if (sortField === "created_at") { va = new Date(va).getTime(); vb = new Date(vb).getTime(); }
      if (typeof va === "string") va = va.toLowerCase();
      if (typeof vb === "string") vb = vb.toLowerCase();
      if (va == null) return 1;
      if (vb == null) return -1;
      return va < vb ? -dir : va > vb ? dir : 0;
    });
    return list;
  }, [rawList, searchTerm, statusFilter, siteFilter, sortField, sortDir]);

  const movementRows = useMemo(() => {
    // Register of every physically-issued component across all requests -
    // one row per component-issue (Aug 2026, "Movement History" tab).
    const rows = [];
    journalRequests.forEach((r) => {
      (r.components || []).forEach((c) => {
        if (c.issued_qty == null) return; // this request was never issued (still pending)
        rows.push({
          key: `${r._id}:${c.product_id}`,
          request_id: r._id, issue_id: r.issue_id, site_id: r.site_id,
          material_id: r.material_id, product_id: c.product_id, description: c.description,
          issued_qty: c.issued_qty, unit_of_measure: c.unit_of_measure,
          warehouse: c.issued_from_warehouse, owner: c.issued_from_owner,
          target_bin: r.target_logistics_area_id,
          requester: r.requester, store_actor: r.store_actor,
          movement: c.goods_movement, when: r.resolved_at || r.updated_at,
          requested_at: r.created_at,
        });
      });
    });
    let list = rows;
    if (siteFilter !== "all") list = list.filter((row) => row.site_id === siteFilter);
    if (searchTerm) {
      const t = searchTerm.toLowerCase();
      list = list.filter((row) => [row.product_id, row.description, row.material_id, row.request_id, row.issue_id].filter(Boolean).join(" ").toLowerCase().includes(t));
    }
    if (userSearch) {
      const t = userSearch.toLowerCase();
      list = list.filter((row) => [row.requester, row.store_actor].filter(Boolean).join(" ").toLowerCase().includes(t));
    }
    if (dateFrom) {
      const from = new Date(dateFrom); from.setHours(0, 0, 0, 0);
      list = list.filter((row) => row.when && new Date(row.when) >= from);
    }
    if (dateTo) {
      const to = new Date(dateTo); to.setHours(23, 59, 59, 999);
      list = list.filter((row) => row.when && new Date(row.when) <= to);
    }
    return list.sort((a, b) => new Date(b.when || 0) - new Date(a.when || 0));
  }, [journalRequests, siteFilter, searchTerm, userSearch, dateFrom, dateTo]);

  const openRequest = (r) => {
    setSelected(r);
    setResultMessage(null);
    const defaults = {};
    r.components.forEach((c) => {
      defaults[c.product_id] = c.issued_qty != null ? String(c.issued_qty) : String(c.required_qty);
    });
    setIssuedQty(defaults);
  };

  const backToQueue = () => {
    setSelected(null);
    setResultMessage(null);
    viewMode === "queue" ? loadRequests() : loadJournal();
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
        <main className="max-w-6xl mx-auto p-4 sm:p-6 space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            {viewMode !== "movements" && (
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
            )}
            <div className="flex gap-1 bg-white border border-[#D0D5DD] rounded-sm p-1">
              <button
                type="button"
                onClick={() => setViewMode("queue")}
                className={`px-3 py-1.5 text-xs font-bold rounded-sm ${viewMode === "queue" ? "bg-[#0E7C86] text-white" : "text-[#344054]"}`}
                data-testid="store-view-mode-queue"
              >
                Pending Queue
              </button>
              <button
                type="button"
                onClick={() => setViewMode("journal")}
                className={`px-3 py-1.5 text-xs font-bold rounded-sm ${viewMode === "journal" ? "bg-[#0E7C86] text-white" : "text-[#344054]"}`}
                data-testid="store-view-mode-journal"
              >
                Journal (All Requests)
              </button>
              <button
                type="button"
                onClick={() => setViewMode("movements")}
                className={`px-3 py-1.5 text-xs font-bold rounded-sm ${viewMode === "movements" ? "bg-[#0E7C86] text-white" : "text-[#344054]"}`}
                data-testid="store-view-mode-movements"
              >
                Movement History
              </button>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Search</Label>
              <div className="relative">
                <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                <Input
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder={viewMode === "movements" ? "Search item / material / request ID..." : "Search material, site, requester, component..."}
                  className="w-64 bg-white pl-7"
                  data-testid="store-search-input"
                />
              </div>
            </div>
            {viewMode === "movements" && (
              <div>
                <Label className="text-xs font-bold text-[#344054]">Requester / Issued By</Label>
                <div className="relative">
                  <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                  <Input
                    value={userSearch}
                    onChange={(e) => setUserSearch(e.target.value)}
                    placeholder="Search a name..."
                    className="w-56 bg-white pl-7"
                    data-testid="store-movements-user-search-input"
                  />
                </div>
              </div>
            )}
            {viewMode === "movements" && (
              <>
                <div>
                  <Label className="text-xs font-bold text-[#344054]">From</Label>
                  <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-36 bg-white" data-testid="store-movements-date-from" />
                </div>
                <div>
                  <Label className="text-xs font-bold text-[#344054]">To</Label>
                  <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-36 bg-white" data-testid="store-movements-date-to" />
                </div>
                {(dateFrom || dateTo) && (
                  <Button
                    variant="outline"
                    className="h-9"
                    onClick={() => { setDateFrom(""); setDateTo(""); }}
                    data-testid="store-movements-date-clear"
                  >
                    Clear Dates
                  </Button>
                )}
              </>
            )}
            {viewMode !== "movements" && (
              <div>
                <Label className="text-xs font-bold text-[#344054]">Status</Label>
                <Select value={statusFilter} onValueChange={setStatusFilter}>
                  <SelectTrigger className="w-44 bg-white" data-testid="store-status-filter-trigger"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all" data-testid="store-status-filter-all">All Statuses</SelectItem>
                    {Object.entries(STATUS_BADGE).map(([key, v]) => (
                      <SelectItem key={key} value={key} data-testid={`store-status-filter-${key}`}>{v.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div>
              <Label className="text-xs font-bold text-[#344054]">Plant / Site</Label>
              <Select value={siteFilter} onValueChange={setSiteFilter}>
                <SelectTrigger className="w-32 bg-white" data-testid="store-site-filter-trigger"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all" data-testid="store-site-filter-all">All Sites</SelectItem>
                  {siteOptions.map((s) => (
                    <SelectItem key={s} value={s} data-testid={`store-site-filter-${s}`}>{s}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button variant="outline" onClick={() => (viewMode === "queue" ? loadRequests() : loadJournal())} data-testid="store-refresh-button">
              <ArrowClockwise size={14} className="mr-1.5" /> Refresh
            </Button>
            {viewMode === "movements" && (
              <Button
                variant="outline"
                onClick={() => downloadCsv(
                  `stock-movement-history-${new Date().toISOString().slice(0, 10)}.csv`,
                  toCsv(movementRows, [
                    { label: "Request ID", get: (r) => r.request_id },
                    { label: "Issue ID", get: (r) => r.issue_id || "" },
                    { label: "Site", get: (r) => r.site_id },
                    { label: "Item", get: (r) => r.product_id },
                    { label: "Description", get: (r) => r.description || "" },
                    { label: "Qty Issued", get: (r) => r.issued_qty },
                    { label: "Unit", get: (r) => r.unit_of_measure || "" },
                    { label: "From Warehouse", get: (r) => r.warehouse || "" },
                    { label: "Owner", get: (r) => r.owner || "" },
                    { label: "To Bin", get: (r) => r.target_bin || "" },
                    { label: "Requested By", get: (r) => r.requester || "" },
                    { label: "Issued By", get: (r) => r.store_actor || "" },
                    { label: "SAP Movement Status", get: (r) => (!r.movement?.attempted ? "not moved" : r.movement.ok ? (r.movement.dry_run ? "Dry Run OK" : "Moved") : "Failed") },
                    { label: "Requested At", get: (r) => (r.requested_at ? new Date(r.requested_at).toLocaleString("en-IN") : "") },
                    { label: "Issued At", get: (r) => (r.when ? new Date(r.when).toLocaleString("en-IN") : "") },
                    { label: "Age at Issue", get: (r) => ageParts(r.requested_at, r.when)?.label || "" },
                  ]),
                )}
                data-testid="store-movements-download-csv"
              >
                <DownloadSimple size={14} className="mr-1.5" /> Download CSV
              </Button>
            )}
            <span className="text-xs text-[#667085] ml-auto" data-testid="store-result-count">
              {viewMode === "movements" ? `${movementRows.length} movement(s)` : `${displayedRequests.length} request(s)`}
            </span>
          </div>

          {viewMode === "movements" ? (
            <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto">
              <table className="w-full text-[12px] border-collapse" data-testid="store-movements-table">
                <thead>
                  <tr>
                    {["Request ID", "Issue ID", "Site", "Item", "Qty Issued", "From Warehouse", "To Bin", "Requested By", "Issued By", "SAP Movement", "Age at Issue", "When"].map((h) => (
                      <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {movementRows.map((row, i) => (
                    <tr key={row.key} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`store-movement-row-${i}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono font-bold text-[#175CD3]">{row.request_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono text-[#0E7C86]">{row.issue_id || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.site_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.product_id}{row.description ? ` - ${row.description}` : ""}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(row.issued_qty)} {formatUnit(row.unit_of_measure)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.warehouse ? `${row.warehouse}${row.owner ? ` \u00b7 ${row.owner}` : ""}` : "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.target_bin || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.requester || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.store_actor || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-[11px]">
                        {!row.movement?.attempted ? (
                          <span className="text-[#98A2B3]" title={row.movement?.reason || ""}>not moved</span>
                        ) : row.movement.ok ? (
                          <span className="text-[#175CD3] font-bold">{row.movement.dry_run ? "Dry Run OK" : "Moved"} ({row.movement.external_id})</span>
                        ) : (
                          <span className="text-[#B42318]" title={row.movement.error || (row.movement.faults || []).map((f) => f.note).join("; ") || ""}>Failed</span>
                        )}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 whitespace-nowrap">
                        {ageParts(row.requested_at, row.when)?.label || "\u2014"}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 whitespace-nowrap">{row.when ? new Date(row.when).toLocaleString("en-IN") : "\u2014"}</td>
                    </tr>
                  ))}
                  {movementRows.length === 0 && (
                    <tr><td colSpan={12} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="store-movements-empty-state">No stock movements recorded yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
          <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto">
            <table className="w-full text-[13px] border-collapse" data-testid="store-requests-table">
              <thead>
                <tr>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">Request ID</th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">Issue ID</th>
                  <SortableHeader label="Requested" field="created_at" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">Age</th>
                  <SortableHeader label="Material" field="material_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Site" field="site_id" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Qty" field="quantity" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Requester" field="requester" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">Short Components</th>
                  <SortableHeader label="Status" field="status" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide"></th>
                </tr>
              </thead>
              <tbody>
                {displayedRequests.map((r, i) => (
                  <tr key={r._id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`store-request-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono font-bold text-[#175CD3] max-w-[110px] truncate" data-testid={`store-request-id-${i}`} title={r._id}>{r._id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono text-[#0E7C86]" data-testid={`store-issue-id-${i}`}>{r.issue_id || "\u2014"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{new Date(r.created_at).toLocaleString("en-IN")}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      {r.status === "pending" || r.status === "partial_pending_planner" ? (
                        <AgeBadge fromIso={r.created_at} testId={`store-age-${i}`} />
                      ) : (
                        <span className="text-[#667085]" data-testid={`store-age-${i}`} title="Time from request to resolution">{ageParts(r.created_at, r.resolved_at)?.label || "\u2014"}</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{r.material_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.site_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.quantity)} {formatUnit(r.unit_code)}</td>
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
                {!loading && displayedRequests.length === 0 && (
                  <tr><td colSpan={11} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="store-requests-empty-state">
                    {rawList.length === 0 ? (viewMode === "journal" ? "No requests recorded yet." : "No pending stock requests right now.") : "No requests match your filters."}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          )}
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
      <main className="max-w-6xl mx-auto p-4 sm:p-6 space-y-4">
        <button onClick={backToQueue} className="flex items-center gap-1.5 text-sm text-[#344054] hover:text-[#0E7C86]" data-testid="store-back-to-queue-button">
          <ArrowLeft size={14} weight="bold" /> Back to queue
        </button>

        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3" data-testid="store-request-detail">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="font-heading text-sm font-bold text-[#1D2939] uppercase tracking-wide">{selected.material_id} &middot; {formatQty(selected.quantity)} {formatUnit(selected.unit_code)}</h3>
              <p className="text-xs text-[#667085]">
                Request ID <span className="font-mono font-bold text-[#175CD3]" data-testid="store-request-detail-id">{selected._id}</span>
                {selected.issue_id && (
                  <> &middot; Issue ID <span className="font-mono font-bold text-[#0E7C86]" data-testid="store-issue-detail-id">{selected.issue_id}</span></>
                )}
                {" "}&middot; Site {selected.site_id} &middot; Requested by {selected.requester} &middot; Proposal {selected.production_proposal_id}
              </p>
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

          <div className="border border-[#D0D5DD] rounded-sm overflow-x-auto">
            <table className="w-full text-[12px] border-collapse" data-testid="store-detail-components-table">
              <thead>
                <tr>
                  {["Component", "Required by Production", "In Stock at This Site (By Warehouse)", "Issued From (Site RM)", "Issued Qty", "SAP Stock Movement (RM \u2192 SFG)"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {selected.components.map((c, i) => (
                  <tr key={c.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top">{c.product_id}{c.description ? ` - ${c.description}` : ""}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums align-top">{formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums align-top" data-testid={`store-locations-${i}`}>
                      <LocationBreakdown locations={c.locations} unit={c.unit_of_measure} />
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top text-[11px]" data-testid={`store-issue-source-${i}`}>
                      {/* Aug 2026, user's fixed business rule - always Site RM -> Site SFG, no picker anymore */}
                      {c.issued_from_warehouse || `${selected.site_id}/${selected.site_id}-RM`}
                      {c.issued_from_owner ? ` \u00b7 ${c.issued_from_owner}` : ""}
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
                        <span className="tabular-nums">{formatQty(c.issued_qty)} {formatUnit(c.unit_of_measure)}</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top text-[11px]" data-testid={`store-goods-movement-${i}`}>
                      {!c.goods_movement?.attempted ? (
                        <span className="text-[#98A2B3]" title={c.goods_movement?.reason || ""}>not moved{c.goods_movement?.reason ? ` - ${c.goods_movement.reason}` : ""}</span>
                      ) : c.goods_movement.ok ? (
                        <span className="text-[#175CD3] font-bold">{c.goods_movement.dry_run ? "Dry Run OK" : "Moved"} ({c.goods_movement.external_id})</span>
                      ) : (
                        <span className="text-[#B42318]">Failed - {c.goods_movement.error || (c.goods_movement.faults || []).map((f) => f.note).join("; ") || "unknown error"}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {isPending && !resultMessage && (
            <div className="space-y-2">
              <div className="bg-[#F0FDF9] border border-[#A6F4C5] rounded-sm px-3 py-2 text-xs text-[#027A48]" data-testid="store-issue-movement-notice">
                Issuing stock records a SAP Goods Movement <strong>{selected.site_id}/{selected.site_id}-RM &rarr; {selected.site_id}/{selected.site_id}-SFG</strong> (fixed by site - not user-chosen). This is LIVE - stock physically moves in SAP the moment you confirm.
              </div>
              {submitting ? (
                <div className="bg-white border border-[#D0D5DD] rounded-sm px-3 py-3 space-y-2" data-testid="store-issue-progress">
                  <div className="h-1.5 w-full bg-[#EAECF0] rounded-full overflow-hidden">
                    <div className="h-full w-1/3 bg-[#0E7C86] rounded-full animate-[store-issue-progress_1.1s_ease-in-out_infinite]" />
                  </div>
                  <p className="text-xs text-[#344054] flex items-center gap-1.5" data-testid="store-issue-progress-message">
                    <ArrowClockwise size={12} className="animate-spin text-[#0E7C86]" />
                    {submitProgressMessage}
                    <span className="text-[#98A2B3] tabular-nums ml-1" data-testid="store-issue-progress-elapsed">{submitElapsed}s</span>
                  </p>
                </div>
              ) : hasShortfall ? (
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
