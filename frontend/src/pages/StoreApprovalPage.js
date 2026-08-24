import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useNavigate, useLocation, useParams, useSearchParams } from "react-router-dom";
import axios from "axios";
import "@/App.css";
import { Package, ArrowLeft, ArrowClockwise, WarningCircle, CaretUp, CaretDown, MagnifyingGlass, DownloadSimple, Shield } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { useAuth } from "@/contexts/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const SITE_FILTER_KEY = "storeApprovalSelectedSite";

const formatQty = (v) => (v == null ? "\u2014" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));

// SAP's own BOM data returns "MASS" as a dimension/QuantityTypeCode label,
// not a real unit - matches the same display-only fix on Production
// Confirmation/Create Order screens. Internal values (unit_code sent back
// to the API, CSV export raw data) are untouched.
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");

// Aug 2026, user's explicit ask: raw SAP logistics area IDs like
// "P1/P1-RM" are meaningless to a store person - show "P1 - Raw Material
// (RM)" everywhere one of these IDs is displayed (Issued From column,
// the fixed movement notice, Movement History table). Display-only -
// the underlying warehouse_id values sent to the API are untouched.
const WAREHOUSE_TYPE_LABELS = { RM: "Raw Material (RM)", QC: "Quality Hold (QC)", SFG: "Semi-Finished Goods (SFG)", FG: "Finished Goods (FG)" };
// Full-ID overrides for warehouses that don't fit the generic
// "{site}-{TYPE}" pattern the splitter below assumes - keep in sync with
// the backend's _SITE_RM_WAREHOUSE_OVERRIDE (store_approval_service.py).
const WAREHOUSE_ID_LABELS = { "P3/P3-Z1-01-A": "P3 - Raw Material Zone 1-01-A (RM)" };
const humanizeWarehouseId = (id) => {
  if (!id) return id;
  if (WAREHOUSE_ID_LABELS[id]) return WAREHOUSE_ID_LABELS[id];
  const raw = id.includes("/") ? id.split("/").pop() : id; // "P1/P1-RM" -> "P1-RM"
  const dashIdx = raw.lastIndexOf("-");
  if (dashIdx === -1) return raw;
  const site = raw.slice(0, dashIdx);
  const suffix = raw.slice(dashIdx + 1);
  return `${site} - ${WAREHOUSE_TYPE_LABELS[suffix] || suffix}`;
};
// Aug 2026, user's explicit ask: site P3 has no standard "-RM" warehouse
// in SAP at all - its raw material stock lives entirely in this one
// zone/bin warehouse instead (confirmed live against SAP). Mirrors the
// backend's _SITE_RM_WAREHOUSE_OVERRIDE (store_approval_service.py) -
// keep both in sync if this ever changes.
const SITE_RM_WAREHOUSE_OVERRIDE = { P3: "P3-Z1-01-A" };
const rmWarehouseIdForSite = (siteId) => `${siteId}/${SITE_RM_WAREHOUSE_OVERRIDE[siteId] || `${siteId}-RM`}`;

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

// Aug 2026, user's ask: is this component actually a raw material, or a
// manufactured (semi-finished) part just being stocked/issued through the
// RM warehouse? Backend derives this from the SAP BOM cache (see
// store_approval_service.refresh_component_locations) - true/false/null
// (never checked yet in SAP). Display-only for now (user's explicit
// choice: "add a badge for now, let us check reliability" before any
// hiding logic).
const MaterialTypeBadge = ({ isManufactured, testId }) => {
  if (isManufactured == null) return null;
  return isManufactured ? (
    <Badge className="ml-1.5 bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF] border text-[10px] font-bold align-middle" data-testid={testId}>Manufactured</Badge>
  ) : (
    <Badge className="ml-1.5 bg-[#FFFAEB] text-[#B54708] border-[#FEDF89] border text-[10px] font-bold align-middle" data-testid={testId}>Bought-Out (RM)</Badge>
  );
};

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

// Aug 2026, matches production_confirmation_service.is_usable_stock_status()
// on the backend - SAP's stock_status can carry a real "Inspection"
// (Quality Inspection hold)/"Blocked" value that physically sits in a
// warehouse but is NOT usable for production. User's explicit ask after
// testing: show this directly on THIS screen (not just discover it after
// a failed/rejected SAP movement).
const RESTRICTED_STOCK_STATUSES = new Set(["inspection", "quality inspection", "blocked", "restricted-use", "restricted", "in transit"]);
const isRestrictedStatus = (status) => RESTRICTED_STOCK_STATUSES.has((status || "").trim().toLowerCase());
// Aug 2026 bug fix: a real SAP row can report a totally normal
// stock_status ("Not Assigned") while ALSO being flagged Restricted Use
// (SAP's own "Restr." checkbox on Stock Overview, CRESTRICTED_IND on the
// backend) - a SEPARATE field from stock_status, not another status
// value. Missed entirely until caught against a real 1kg restricted lot
// at site P2 that showed as plain usable stock on this screen.
const isLocationRestricted = (loc) => isRestrictedStatus(loc?.stock_status) || !!loc?.restricted;
// Aug 2026: only RM + QC (Quality Hold) warehouse rows ever reach this
// screen now (see store_approval_service.refresh_component_locations) -
// a QC-warehouse row reports stock_status "Not Assigned" in SAP's own
// feed (NOT "Inspection"), so it would otherwise slip through the check
// above as if it were normal usable stock. Being in the QC warehouse AT
// ALL means it's on hold, regardless of what stock_status says.
const isQcWarehouse = (warehouseId) => (warehouseId || "").endsWith("-QC");

const LocationBreakdown = ({ locations, unit, sourceWarehouseId }) => {
  if (!locations || locations.length === 0) {
    return <span className="text-[#98A2B3]">no stock at this site</span>;
  }
  const rmHasUsableStock = locations.some((loc) => loc.warehouse_id === sourceWarehouseId && !isLocationRestricted(loc));
  const rmHasOnlyRestrictedStock = !rmHasUsableStock && locations.some((loc) => loc.warehouse_id === sourceWarehouseId && isLocationRestricted(loc));
  return (
    <div className="space-y-0.5">
      {locations.map((loc, i) => {
        // Requests created before the warehouse/stock_status fields were
        // added froze the OLD {site, qty} shape into their doc forever -
        // fall back to the (unscoped) site code instead of a confusing
        // "Unknown Warehouse" for those legacy, already-open requests.
        const label = loc.warehouse || (loc.site ? loc.site.split("-").pop() : "Unknown Warehouse");
        const restricted = isLocationRestricted(loc) || isQcWarehouse(loc.warehouse_id);
        return (
          <div key={i} data-testid={restricted ? "location-restricted-row" : undefined}>
            <span className={restricted ? "text-[#B54708] font-bold" : "text-[#667085]"}>
              {label}{loc.stock_status ? ` (${loc.stock_status})` : ""}{restricted && !isRestrictedStatus(loc.stock_status) && !isQcWarehouse(loc.warehouse_id) ? " (Restricted Use)" : ""}{restricted ? " \u26A0 On Hold - not usable" : ""}:
            </span> {formatQty(loc.qty)} {formatUnit(unit)}
          </div>
        );
      })}
      {rmHasOnlyRestrictedStock && (
        <div className="text-[#B42318] font-bold" data-testid="rm-restricted-warning">
          {"\u26A0 RM stock is only on Quality Hold - not usable, will be treated as no stock to issue from."}
        </div>
      )}
    </div>
  );
};

const STATUS_BADGE = {
  pending: { label: "Awaiting Store", tone: "bg-[#FFFAEB] text-[#B54708] border-[#FEDF89]" },
  issuing: { label: "Issuing Stock...", tone: "bg-[#EFF8FF] text-[#0E7C86] border-[#B2DDFF]" },
  partial_pending_planner: { label: "Awaiting Requester", tone: "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]" },
  resolved_balance_pending: { label: "Balance Pending", tone: "bg-[#FEF6EE] text-[#B93815] border-[#F9DBAF]" },
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
  const navigate = useNavigate();
  const location = useLocation();
  const { requestId } = useParams();
  const [searchParams] = useSearchParams();
  // Aug 2026, user's explicit ask: Store Approval now requires Entra ID
  // login (was previously anonymous with a manual "Your Name" box) - the
  // acting store person's name comes straight from their signed-in
  // session instead.
  const { user } = useAuth();
  const storeActorName = user?.name || user?.email || "";
  // Aug 2026, user's ask: "always load what they select once" - the
  // chosen site persists across visits instead of resetting to "All
  // Sites" every time.
  const [siteFilter, setSiteFilter] = useState(() => localStorage.getItem(SITE_FILTER_KEY) || "all");
  // Aug 2026, user's ask: real sub-routes instead of hidden component
  // state, so a specific tab or a request's detail is shareable/
  // bookmarkable and survives browser back/forward + a page refresh.
  // viewMode is the underlying LIST tab (queue/journal/balance/movements)
  // - kept even while a request's detail is open (carried via the
  // ?from= query param on /storeapproval/request/:id) so "Back to queue"
  // returns to the right tab and the background 8s poll further below
  // keeps that tab's data fresh the whole time the detail view is open.
  const viewMode = requestId
    ? (searchParams.get("from") || "queue")
    : location.pathname.endsWith("/journal") ? "journal"
    : location.pathname.endsWith("/balance") ? "balance"
    : location.pathname.endsWith("/movements") ? "movements"
    : "queue";
  const listPath = (mode) => (mode === "queue" ? "/storeapproval" : `/storeapproval/${mode}`);
  const [requests, setRequests] = useState([]);
  const [journalRequests, setJournalRequests] = useState([]);
  const [balanceRequests, setBalanceRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [issuedQty, setIssuedQty] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [submitElapsed, setSubmitElapsed] = useState(0);
  const [issueProgress, setIssueProgress] = useState(null);
  const [refreshingStock, setRefreshingStock] = useState(false);
  const [refreshElapsed, setRefreshElapsed] = useState(0);
  const [refreshStatus, setRefreshStatus] = useState(null);
  const [resultMessage, setResultMessage] = useState(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [userSearch, setUserSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortField, setSortField] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  useEffect(() => localStorage.setItem(SITE_FILTER_KEY, siteFilter), [siteFilter]);

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

  // Ticks while "Refresh Live Stock Now" (v2, Aug 2026) is in flight - a
  // fast, retried, quantities-only SAP pull (see
  // inventory_service.refresh_stock_quantities_only), deliberately NOT the
  // slow full refresh (with Standard Costs valuation) an earlier version
  // of this button used, which is what made it feel "stuck" for minutes.
  useEffect(() => {
    if (!refreshingStock) {
      setRefreshElapsed(0);
      return;
    }
    const interval = setInterval(() => setRefreshElapsed((s) => s + 1), 1000);
    return () => clearInterval(interval);
  }, [refreshingStock]);

  const submitProgressMessage = issueProgress && issueProgress.progress_total > 0
    ? `Moving stock for component ${issueProgress.progress_current} of ${issueProgress.progress_total}${issueProgress.current_component ? ` (${issueProgress.current_component})` : ""}...`
    : "Recording issued quantities and posting the SAP Goods Movement...";

  // Shared across all 3 loaders below (only one runs per 8s poll tick,
  // picked by viewMode) - a single transient network/Cloudflare blip on
  // this background poll used to pop a loud toast every single time,
  // even though the very next 8s tick almost always succeeds. Now it
  // fails silently (logged to console for diagnosis) and only surfaces a
  // toast once the SAME poll has failed 3 times in a row - a real,
  // persistent problem worth interrupting the user for, not one blip.
  const pollFailureCountRef = useRef(0);
  // Aug 2026 bug fix (user's own report): the Plant/Site filter used to
  // only list sites that happened to have a currently-open request, so a
  // real site like P9 silently vanished from the dropdown whenever it had
  // no pending request at that exact moment. Fetched once on mount from
  // the full inventory_cache instead (see server.py's /known-sites).
  const [knownSites, setKnownSites] = useState([]);
  useEffect(() => {
    axios.get(`${API}/store-requests/known-sites`).then(({ data }) => setKnownSites(data.sites || [])).catch(() => {});
  }, []);

  const loadRequests = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests`);
      setRequests(data.requests);
      pollFailureCountRef.current = 0;
    } catch (e) {
      pollFailureCountRef.current += 1;
      console.error("Failed to load pending stock requests:", e);
      if (pollFailureCountRef.current >= 3) {
        toast.error("Still unable to reach the server for the stock request queue - check your connection");
        pollFailureCountRef.current = 0; // re-arm for another 3 before nagging again during a longer outage
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const loadJournal = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests/journal`);
      setJournalRequests(data.requests);
      pollFailureCountRef.current = 0;
    } catch (e) {
      pollFailureCountRef.current += 1;
      console.error("Failed to load the requests journal:", e);
      if (pollFailureCountRef.current >= 3) {
        toast.error("Still unable to reach the server for the requests journal - check your connection");
        pollFailureCountRef.current = 0;
      }
    } finally {
      setLoading(false);
    }
  }, []);

  // Rule 2 (Aug 2026): requests where the store already issued a partial
  // quantity, the order proceeded, but a balance is still outstanding -
  // reopen one of these once more stock physically arrives in RM.
  const loadBalancePending = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/store-requests/balance-pending`);
      setBalanceRequests(data.requests);
      pollFailureCountRef.current = 0;
    } catch (e) {
      pollFailureCountRef.current += 1;
      console.error("Failed to load balance-pending requests:", e);
      if (pollFailureCountRef.current >= 3) {
        toast.error("Still unable to reach the server for balance-pending requests - check your connection");
        pollFailureCountRef.current = 0;
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const refresh = () => {
      if (viewMode === "queue") return loadRequests();
      if (viewMode === "balance") return loadBalancePending();
      return loadJournal();
    };
    refresh();
    const interval = setInterval(refresh, 8000);
    return () => clearInterval(interval);
  }, [viewMode, loadRequests, loadJournal, loadBalancePending]);

  // Aug 2026 fix (testing_agent iteration_103): a request can be "issuing"
  // because a DIFFERENT session/tab started the job - pollIssueJob() only
  // exists for the tab that actually clicked submit, so without this the
  // detail view sat on "Issuing Stock..." forever despite promising it
  // would update automatically.
  useEffect(() => {
    if (!selected || selected.status !== "issuing" || submitting) return;
    const interval = setInterval(async () => {
      try {
        const { data } = await axios.get(`${API}/store-requests/${selected._id}`);
        setSelected(data);
      } catch {
        // transient - next tick will retry
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [selected, submitting]);

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(field === "created_at" ? "desc" : "asc");
    }
  };

  const rawList = viewMode === "queue" ? requests : viewMode === "balance" ? balanceRequests : journalRequests;
  const siteOptions = useMemo(() => {
    const fromRequests = rawList.map((r) => r.site_id).filter(Boolean);
    return Array.from(new Set([...knownSites, ...fromRequests])).sort();
  }, [rawList, knownSites]);

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

  // Fetches the request behind /storeapproval/request/:requestId (direct
  // link, refresh, or browser back/forward all land here the same way -
  // no reliance on already having the row from a list fetch).
  useEffect(() => {
    if (!requestId) {
      setSelected(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const { data } = await axios.get(`${API}/store-requests/${requestId}`);
        if (cancelled) return;
        setSelected(data);
        setResultMessage(null);
        // Aug 2026, user's explicit ask: leave Issued Qty blank rather
        // than prefilling the required/shortfall amount - the store
        // person types what they actually issue, validated against
        // usable RM stock below (see usableRmQty/overIssueErrors).
        const defaults = {};
        data.components.forEach((c) => {
          defaults[c.product_id] = "";
        });
        setIssuedQty(defaults);
      } catch {
        if (cancelled) return;
        toast.error("Could not load that request - it may not exist anymore");
        navigate(listPath(viewMode), { replace: true });
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestId]);

  const openRequest = (r) => {
    navigate(`/storeapproval/request/${r._id}?from=${viewMode}`);
  };

  const backToQueue = () => {
    navigate(listPath(viewMode));
    if (viewMode === "queue") loadRequests();
    else if (viewMode === "balance") loadBalancePending();
    else loadJournal();
  };

  if (!selected) {
    if (requestId) {
      return (
        <div className="min-h-screen bg-[#F2F4F7] text-[#1D2939]">
          <Toaster position="top-right" />
          <main className="max-w-6xl mx-auto p-4 sm:p-6" data-testid="store-request-loading">
            <p className="text-sm text-[#667085] flex items-center gap-1.5"><ArrowClockwise size={14} className="animate-spin" /> Loading request {requestId}...</p>
          </main>
        </div>
      );
    }
    return (
      <div className="min-h-screen bg-[#F2F4F7] text-[#1D2939]">
        <Toaster position="top-right" />
        <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4" data-testid="store-approval-header">
          <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
            <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
              <Shield size={18} weight="fill" className="text-white" />
            </div>
            <div className="flex flex-col leading-tight">
              <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
              <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Store Approval</span>
            </div>
          </div>
          <div className="w-px h-7 bg-white/25 shrink-0" />
          <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
            <NavTabs />
          </div>
          <SapConnectionStatus />
        </header>
        <main className="max-w-6xl mx-auto p-4 sm:p-6 space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex gap-1 bg-white border border-[#D0D5DD] rounded-sm p-1">
              <button
                type="button"
                onClick={() => navigate("/storeapproval")}
                className={`px-3 py-1.5 text-xs font-bold rounded-sm ${viewMode === "queue" ? "bg-[#0E7C86] text-white" : "text-[#344054]"}`}
                data-testid="store-view-mode-queue"
              >
                Pending Queue
              </button>
              <button
                type="button"
                onClick={() => navigate("/storeapproval/journal")}
                className={`px-3 py-1.5 text-xs font-bold rounded-sm ${viewMode === "journal" ? "bg-[#0E7C86] text-white" : "text-[#344054]"}`}
                data-testid="store-view-mode-journal"
              >
                Journal (All Requests)
              </button>
              <button
                type="button"
                onClick={() => navigate("/storeapproval/balance")}
                className={`px-3 py-1.5 text-xs font-bold rounded-sm ${viewMode === "balance" ? "bg-[#0E7C86] text-white" : "text-[#344054]"}`}
                data-testid="store-view-mode-balance"
              >
                Balance Pending
              </button>
              <button
                type="button"
                onClick={() => navigate("/storeapproval/movements")}
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
            <Button variant="outline" onClick={() => (viewMode === "queue" ? loadRequests() : viewMode === "balance" ? loadBalancePending() : loadJournal())} data-testid="store-refresh-button">
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
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.warehouse ? `${humanizeWarehouseId(row.warehouse)}${row.owner ? ` \u00b7 ${row.owner}` : ""}` : "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.target_bin ? humanizeWarehouseId(row.target_bin) : "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.requester || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{row.store_actor || "\u2014"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-[11px]">
                        {!row.movement?.attempted ? (
                          <span className="text-[#98A2B3]" title={row.movement?.reason || ""}>not moved</span>
                        ) : row.movement.ok ? (
                          <span className="text-[#175CD3] font-bold">{row.movement.dry_run ? "Dry Run OK" : "Moved"} ({row.movement.external_id})</span>
                        ) : (
                          <span className="text-[#B42318]" title={[row.movement.error_detail, row.movement.error_hi].filter(Boolean).join(" / ") || (row.movement.faults || []).map((f) => f.note).join("; ") || ""}>Failed</span>
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
                    {rawList.length === 0 ? (viewMode === "journal" ? "No requests recorded yet." : viewMode === "balance" ? "No requests with an outstanding balance right now." : "No pending stock requests right now.") : "No requests match your filters."}
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
  const isIssuing = selected.status === "issuing";
  const isReopenable = selected.status === "resolved_balance_pending";
  const hasShortfall = isPending && selected.components.some((c) => {
    const q = Number(issuedQty[c.product_id]);
    return Number.isNaN(q) || q < c.required_qty;
  });

  // Aug 2026, user's explicit ask: "allowable" = usable RM stock only
  // (excludes Inspection/Restricted-Use rows, matches isLocationRestricted
  // used in the location breakdown above) - a store person should never
  // be able to type in more than what's actually free to issue from the
  // RM warehouse, regardless of how much production still needs.
  const rmWarehouseId = rmWarehouseIdForSite(selected.site_id);
  const usableRmQty = (c) => (c.locations || [])
    .filter((loc) => loc.warehouse_id === rmWarehouseId && !isLocationRestricted(loc))
    .reduce((sum, loc) => sum + (Number(loc.qty) || 0), 0);
  const overIssueErrors = {};
  selected.components.forEach((c) => {
    const raw = issuedQty[c.product_id];
    if (raw === "" || raw == null) return;
    const q = Number(raw);
    const max = usableRmQty(c);
    if (!Number.isNaN(q) && q > max) overIssueErrors[c.product_id] = max;
  });
  const hasOverIssueError = Object.keys(overIssueErrors).length > 0;

  const submitIssue = async (decision) => {
    if (hasOverIssueError) {
      toast.error("One or more issued quantities exceed the usable stock available - fix them before submitting");
      return;
    }
    const issued = selected.components.map((c) => ({ product_id: c.product_id, issued_qty: Number(issuedQty[c.product_id]) || 0 }));
    if (issued.some((i) => Number.isNaN(i.issued_qty) || i.issued_qty < 0)) {
      toast.error("Issued quantities must be valid, non-negative numbers");
      return;
    }
    setSubmitting(true);
    try {
      // Aug 2026: this endpoint now returns almost instantly (job_id) - the
      // real SAP Goods Movement calls run in a background job, polled
      // below, so a request with several components can never trip the
      // platform's ingress/Cloudflare timeout the way one long synchronous
      // POST used to.
      const { data } = await axios.post(`${API}/store-requests/${selected._id}/issue`, {
        issued, decision: hasShortfall ? decision : null, actor: storeActorName,
      });
      setSelected(data.request);
      await pollIssueJob(data.job_id, selected._id);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to submit issued quantities");
      setSubmitting(false);
    }
  };

  // "Refresh Live Stock Now" v2 (Aug 2026) - a fast, retried, quantities-
  // only SAP pull, then re-fetches THIS request so its component
  // locations pick up whatever was just found. Never blocks/breaks the
  // screen on failure - just toasts and leaves the existing data as-is.
  const refreshLiveStock = async () => {
    setRefreshingStock(true);
    setRefreshStatus(`Connecting to SAP for Site ${selected.site_id}...`);
    try {
      const { data } = await axios.post(`${API}/store-requests/refresh-live-stock`, null, { params: { site_id: selected.site_id } });
      setRefreshStatus(`Checking live stock at Site ${selected.site_id} (Raw Material & Quality Hold)...`);
      const job = await new Promise((resolve) => {
        const interval = setInterval(async () => {
          try {
            const { data: job } = await axios.get(`${API}/store-requests/refresh-live-stock/${data.job_id}`);
            if (job.status === "done" || job.status === "failed") {
              clearInterval(interval);
              resolve(job);
            }
          } catch {
            clearInterval(interval);
            resolve({ status: "failed", error: "Lost connection while refreshing live stock" });
          }
        }, 2000);
      });
      if (job.status === "failed") {
        toast.error(job.error || "Live stock refresh failed - still showing the last known data");
      } else {
        const found = job.result?.rows_found;
        setRefreshStatus(found != null ? `Found ${found} record(s) at Site ${selected.site_id} - updating table...` : "Updating table...");
        await new Promise((r) => setTimeout(r, 700));
        toast.success("Live SAP stock refreshed");
      }
      const { data: fresh } = await axios.get(`${API}/store-requests/${selected._id}`);
      setSelected(fresh);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to start a live stock refresh");
    } finally {
      setRefreshingStock(false);
      setRefreshStatus(null);
    }
  };

  const pollIssueJob = (jobId, requestId) => new Promise((resolve) => {
    const interval = setInterval(async () => {
      try {
        const { data: job } = await axios.get(`${API}/store-requests/issue-status/${jobId}`);
        setIssueProgress(job);
        if (job.status === "done" || job.status === "failed") {
          clearInterval(interval);
          setSubmitting(false);
          setIssueProgress(null);
          const { data: freshRequest } = await axios.get(`${API}/store-requests/${requestId}`);
          setSelected(freshRequest);
          if (job.status === "failed") {
            toast.error(job.error || "Stock issue failed - the request has been reverted so you can retry");
          } else if (freshRequest.status === "resolved") {
            setResultMessage(freshRequest.resolution === "balance_completed"
              ? "Balance fully issued - this request is now closed."
              : "Stock issue recorded - the automated Production Order pipeline is resuming now.");
            toast.success(freshRequest.resolution === "balance_completed" ? "Balance fully issued - closed" : "Recorded - order creation resuming");
          } else if (freshRequest.status === "resolved_balance_pending") {
            setResultMessage("Recorded - a balance is still outstanding on this request. You'll find it under \"Balance Pending\" to reopen once more stock arrives.");
            toast.success("Recorded - balance still outstanding");
          } else if (freshRequest.status === "partial_pending_planner") {
            setResultMessage("Sent to the requester for approval - they'll decide whether to proceed with the partial stock.");
            toast.success("Sent to requester for approval");
          }
          resolve();
        }
      } catch {
        clearInterval(interval);
        setSubmitting(false);
        setIssueProgress(null);
        toast.error("Lost connection while tracking the stock issue - refresh to check its current status");
        resolve();
      }
    }, 2000);
  });

  return (
    <div className="min-h-screen bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Store Approval</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <div className="shrink-0 w-8" />
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
              <Badge className={`${STATUS_BADGE[selected.status]?.tone || "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]"} border`}>{STATUS_BADGE[selected.status]?.label || selected.status}</Badge>
            </div>
          </div>

          {(isPending || isReopenable) && (
            <div className="flex flex-wrap items-center gap-3 bg-[#F9FAFB] border border-[#EAECF0] rounded-sm px-3 py-2" data-testid="store-refresh-live-stock-control">
              <Button
                variant="outline"
                size="sm"
                className="h-8 bg-white shrink-0"
                disabled={refreshingStock || submitting || isIssuing}
                onClick={refreshLiveStock}
                data-testid="store-refresh-live-stock-button"
                title="Just posted a Goods Receipt at this site? Pull live SAP quantities for THIS site now instead of waiting up to 30 min for the scheduled refresh"
              >
                <ArrowClockwise size={13} className={`mr-1.5 ${refreshingStock ? "animate-spin" : ""}`} />
                {refreshingStock ? `Refreshing (${refreshElapsed}s)...` : "Refresh Live Stock Now"}
              </Button>
              {refreshingStock ? (
                <div className="flex-1 min-w-[240px] max-w-md space-y-1">
                  <div className="h-1.5 w-full bg-[#EAECF0] rounded-full overflow-hidden">
                    <div className="h-full w-1/3 bg-[#0E7C86] rounded-full animate-[store-issue-progress_1.1s_ease-in-out_infinite]" />
                  </div>
                  <p className="text-[11px] text-[#667085]" data-testid="store-refresh-live-stock-status">{refreshStatus}</p>
                </div>
              ) : (
                <p className="text-[11px] text-[#667085]">Just posted a Goods Receipt? Pull live SAP quantities for this site before issuing.</p>
              )}
            </div>
          )}

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
                  {["Component", "Required Qty", "In Stock (Warehouse)", "Issued From", "Issued Qty", "Stock Movement (RM \u2192 SFG)"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {selected.components.map((c, i) => (
                  <tr key={c.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top">
                      {c.product_id}{c.description ? ` - ${c.description}` : ""}
                      <MaterialTypeBadge isManufactured={c.is_manufactured} testId={`store-material-type-${i}`} />
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums align-top">{formatQty(c.required_qty)} {formatUnit(c.unit_of_measure)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums align-top" data-testid={`store-locations-${i}`}>
                      <LocationBreakdown locations={c.locations} unit={c.unit_of_measure} sourceWarehouseId={rmWarehouseIdForSite(selected.site_id)} />
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top text-[11px]" data-testid={`store-issue-source-${i}`}>
                      {/* Aug 2026, user's fixed business rule - always Site RM -> Site SFG, no picker anymore */}
                      {humanizeWarehouseId(c.issued_from_warehouse || rmWarehouseIdForSite(selected.site_id))}
                      {c.issued_from_owner ? ` \u00b7 ${c.issued_from_owner}` : ""}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 align-top">
                      {isPending ? (
                        <div className="space-y-0.5">
                          <Input
                            type="number"
                            placeholder="Enter qty..."
                            value={issuedQty[c.product_id] ?? ""}
                            onChange={(e) => setIssuedQty((prev) => ({ ...prev, [c.product_id]: e.target.value }))}
                            className={`h-7 w-28 text-right tabular-nums ${overIssueErrors[c.product_id] !== undefined ? "border-[#B42318] focus-visible:ring-[#B42318]" : ""}`}
                            data-testid={`store-issued-qty-input-${i}`}
                          />
                          {overIssueErrors[c.product_id] !== undefined && (
                            <p className="text-[10px] text-[#B42318] font-bold" data-testid={`store-issued-qty-error-${i}`}>
                              Exceeds usable stock ({formatQty(overIssueErrors[c.product_id])} {formatUnit(c.unit_of_measure)} available)
                            </p>
                          )}
                        </div>
                      ) : isReopenable && c.shortfall > 0 ? (
                        <div className="space-y-0.5" data-testid={`store-reopen-issue-cell-${i}`}>
                          <p className="text-[10px] text-[#667085]">Already issued: {formatQty(c.issued_qty)} {formatUnit(c.unit_of_measure)}</p>
                          <Input
                            type="number"
                            placeholder="Enter qty..."
                            value={issuedQty[c.product_id] ?? ""}
                            onChange={(e) => setIssuedQty((prev) => ({ ...prev, [c.product_id]: e.target.value }))}
                            className={`h-7 w-28 text-right tabular-nums ${overIssueErrors[c.product_id] !== undefined ? "border-[#B42318] focus-visible:ring-[#B42318]" : ""}`}
                            data-testid={`store-issued-qty-input-${i}`}
                          />
                          {overIssueErrors[c.product_id] !== undefined && (
                            <p className="text-[10px] text-[#B42318] font-bold" data-testid={`store-issued-qty-error-${i}`}>
                              Exceeds usable stock ({formatQty(overIssueErrors[c.product_id])} {formatUnit(c.unit_of_measure)} available)
                            </p>
                          )}
                        </div>
                      ) : isReopenable ? (
                        <span className="tabular-nums text-[#027A48]" data-testid={`store-issued-qty-complete-${i}`}>{formatQty(c.issued_qty)} {formatUnit(c.unit_of_measure)} (fully issued)</span>
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
                        <div className="text-[#B42318]" title={c.goods_movement.error_detail || ""}>
                          <p className="font-bold" data-testid={`store-goods-movement-error-en-${i}`}>Failed - {c.goods_movement.error || (c.goods_movement.faults || []).map((f) => f.note).join("; ") || "unknown error"}</p>
                          {c.goods_movement.error_hi && (
                            <p className="text-[#912018]" lang="hi" data-testid={`store-goods-movement-error-hi-${i}`}>{c.goods_movement.error_hi}</p>
                          )}
                        </div>
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
                Issuing stock records a SAP Goods Movement <strong>{humanizeWarehouseId(rmWarehouseIdForSite(selected.site_id))} &rarr; {humanizeWarehouseId(`${selected.site_id}/${selected.site_id}-SFG`)}</strong> (fixed by site - not user-chosen). This is LIVE - stock physically moves in SAP the moment you confirm.
              </div>
              {hasShortfall ? (
                <>
                  <p className="text-[11px] text-[#B54708]">One or more components are still short of the required quantity. Choose how to proceed:</p>
                  <div className="flex flex-wrap gap-2">
                    <Button disabled={submitting || hasOverIssueError} onClick={() => submitIssue("proceed")} data-testid="store-submit-proceed-button">
                      Proceed with Partial Stock
                    </Button>
                    <Button variant="outline" disabled={submitting || hasOverIssueError} onClick={() => submitIssue("send_to_planner")} data-testid="store-submit-send-to-planner-button">
                      Send to Requester for Approval
                    </Button>
                  </div>
                </>
              ) : (
                <Button disabled={submitting || hasOverIssueError} onClick={() => submitIssue(null)} data-testid="store-submit-full-button">
                  Confirm Stock Fully Issued
                </Button>
              )}
            </div>
          )}

          {isReopenable && !resultMessage && (
            <div className="space-y-2">
              <div className="bg-[#FEF6EE] border border-[#F9DBAF] rounded-sm px-3 py-2 text-xs text-[#B93815]" data-testid="store-reopen-notice">
                This request still has an outstanding balance. Enter what you can issue now for the short component(s) above - you can reopen this again later if there's still a balance left.
              </div>
              <Button disabled={submitting || hasOverIssueError} onClick={() => submitIssue(null)} data-testid="store-submit-reopen-button">
                Issue Remaining Balance
              </Button>
            </div>
          )}

          {(submitting || isIssuing) && !resultMessage && (
            <div className="bg-white border border-[#D0D5DD] rounded-sm px-3 py-3 space-y-2" data-testid="store-issue-progress">
              <div className="h-1.5 w-full bg-[#EAECF0] rounded-full overflow-hidden">
                <div className="h-full w-1/3 bg-[#0E7C86] rounded-full animate-[store-issue-progress_1.1s_ease-in-out_infinite]" />
              </div>
              <p className="text-xs text-[#344054] flex items-center gap-1.5" data-testid="store-issue-progress-message">
                <ArrowClockwise size={12} className="animate-spin text-[#0E7C86]" />
                {submitting ? submitProgressMessage : "A stock issue is already being processed for this request (started from another session) - this page will update automatically."}
                {submitting && <span className="text-[#98A2B3] tabular-nums ml-1" data-testid="store-issue-progress-elapsed">({submitElapsed}s)</span>}
              </p>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
