import { useState, useEffect, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import {
  Package,
  Database,
  WarningCircle,
  ArrowClockwise,
  CaretDown,
  CaretRight,
  CaretUp,
  CheckCircle,
  XCircle,
  ClockCounterClockwise,
  Funnel,
  Truck,
  TreeStructure,
  CalendarBlank,
  ListChecks,
  MagnifyingGlass,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { NavTabs } from "@/components/NavTabs";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const StatCard = ({ icon: Icon, label, value, testId, tone }) => (
  <div
    className="bg-white border border-[#D0D5DD] rounded-sm p-3 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] flex flex-col gap-1.5"
    data-testid={testId}
  >
    <div className="flex items-center gap-1.5 text-[#475467]">
      <Icon size={14} weight="bold" />
      <span className="font-heading text-xs font-bold uppercase tracking-wider">{label}</span>
    </div>
    <span className={`font-sans text-2xl font-bold tabular-nums ${tone || "text-[#1D2939]"}`}>{value}</span>
  </div>
);

const formatQty = (value) => (value == null ? "—" : value.toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatDate = (isoDate) => {
  if (!isoDate) return "—";
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
};
const formatMoney = (value, currency) =>
  value == null ? "—" : `${currency || ""} ${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const isPastDue = (isoDate) => {
  if (!isoDate) return false;
  return isoDate < new Date().toISOString().slice(0, 10);
};
const isoWeekLabel = (isoDate) => {
  const d = new Date(isoDate + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(weekNo).padStart(2, "0")}`;
};

const compareValues = (a, b) => {
  if (a == null && b == null) return 0;
  if (a == null) return -1;
  if (b == null) return 1;
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
};

const SortableHeader = ({ label, field, sortConfig, onSort, className, testId }) => {
  const sortable = !!field && !!onSort;
  const active = sortable && sortConfig?.field === field;
  return (
    <th
      className={`bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide ${
        sortable ? "cursor-pointer hover:bg-[#DCE0E6] select-none" : ""
      } ${className || ""}`}
      onClick={sortable ? () => onSort(field) : undefined}
      data-testid={testId}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {active && (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
      </span>
    </th>
  );
};

// -------------------- Open PO Demand tab --------------------
const OpenPoDemandTab = () => {
  const [customer, setCustomer] = useState("");
  const [plant, setPlant] = useState("");
  const [rows, setRows] = useState([]);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState("");
  const [basisFilter, setBasisFilter] = useState("all");
  const [sortConfig, setSortConfig] = useState({ field: "target_ship_date", direction: "asc" });

  const fetchDemand = async () => {
    setLoading(true);
    setError(null);
    try {
      const params = {};
      if (customer.trim()) params.customer = customer.trim();
      if (plant.trim()) params.plant = plant.trim();
      const { data } = await axios.get(`${API}/production-plan/open-po-demand`, { params });
      setRows(data.rows);
      setMeta({ count: data.count, truncated: data.truncated, max_changed_at: data.max_changed_at });
      setLoaded(true);
      toast.success(`Loaded ${data.count} open PO line(s)`);
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Failed to load Open PO Demand feed";
      setError(detail);
      toast.error("Could not load Open PO Demand feed", { description: detail });
    } finally {
      setLoading(false);
    }
  };

  const onSort = (field) => {
    setSortConfig((prev) => (prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }));
  };

  const getSortValue = (r, field) => {
    if (field === "customer") return (r.customer || "").toLowerCase();
    if (field === "customer_po") return (r.customer_po || "").toLowerCase();
    if (field === "item_code") return (r.item_code || "").toLowerCase();
    if (field === "description") return (r.description || "").toLowerCase();
    return r[field];
  };

  const q = search.trim().toLowerCase();
  const filteredRows = rows.filter((r) => {
    if (basisFilter !== "all" && r.target_ship_basis !== basisFilter) return false;
    if (!q) return true;
    return (
      (r.customer || "").toLowerCase().includes(q) ||
      (r.customer_po || "").toLowerCase().includes(q) ||
      (r.item_code || "").toLowerCase().includes(q) ||
      (r.description || "").toLowerCase().includes(q)
    );
  });
  const sortedRows = [...filteredRows].sort((a, b) => {
    const cmp = compareValues(getSortValue(a, sortConfig.field), getSortValue(b, sortConfig.field));
    return sortConfig.direction === "asc" ? cmp : -cmp;
  });

  const columns = [
    { label: "Customer", field: "customer" },
    { label: "Customer PO", field: "customer_po" },
    { label: "Item Code", field: "item_code" },
    { label: "Description", field: "description" },
    { label: "Qty Open", field: "qty_open" },
    { label: "Due Date", field: "due_date" },
    { label: "Target Ship Date", field: "target_ship_date" },
    { label: "Basis", field: "target_ship_basis" },
    { label: "Lead Time (D)", field: "lead_day" },
    { label: "Invoice Price", field: "invoice_price" },
  ];

  return (
    <div>
      <div className="bg-white border border-[#D0D5DD] rounded-sm p-2.5 flex items-center gap-3 flex-wrap mb-3">
        <div className="flex items-center gap-1.5">
          <Funnel size={13} className="text-[#475467]" />
          <input
            type="text"
            placeholder="Filter by customer..."
            value={customer}
            onChange={(e) => setCustomer(e.target.value)}
            className="h-8 w-56 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
            data-testid="open-po-customer-filter"
          />
        </div>
        <input
          type="text"
          placeholder="Filter by plant..."
          value={plant}
          onChange={(e) => setPlant(e.target.value)}
          className="h-8 w-40 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
          data-testid="open-po-plant-filter"
        />
        <Button
          type="button"
          onClick={fetchDemand}
          disabled={loading}
          className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white rounded-sm px-4 text-[13px] font-bold transition-colors"
          data-testid="open-po-fetch-button"
        >
          <Truck size={14} className="mr-1.5" />
          {loading ? "Fetching..." : "Fetch Open PO Demand"}
        </Button>
        {meta?.truncated && (
          <Badge variant="outline" className="bg-[#FFFAEB] text-[#B54708] border-[#FEDF89] rounded font-sans text-xs h-8 flex items-center">
            Result truncated - refine filters
          </Badge>
        )}
        {meta?.max_changed_at && (
          <div className="flex items-center gap-1.5 text-xs text-[#475467] ml-auto" data-testid="open-po-as-of">
            <ClockCounterClockwise size={13} weight="bold" />
            Feed watermark: {new Date(meta.max_changed_at).toLocaleString()}
          </div>
        )}
      </div>

      {loaded && (
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-2.5 flex items-center gap-3 flex-wrap mb-3">
          <div className="relative">
            <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <input
              type="text"
              placeholder="Search customer, PO, item, description..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 w-72 pl-7 pr-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
              data-testid="open-po-search-input"
            />
          </div>
          <Select value={basisFilter} onValueChange={setBasisFilter}>
            <SelectTrigger className="h-8 w-48 text-[13px] rounded-sm border-[#D0D5DD]" data-testid="open-po-basis-filter">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" data-testid="open-po-basis-filter-all">All Ship Bases</SelectItem>
              <SelectItem value="ex_factory_offset" data-testid="open-po-basis-filter-exfactory">Ex-Factory Offset</SelectItem>
              <SelectItem value="cfs_must_ship_by" data-testid="open-po-basis-filter-cfs">CFS Must-Ship-By</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-xs text-[#475467] ml-auto" data-testid="open-po-result-count">
            Showing {sortedRows.length} of {rows.length} line(s)
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-3">
        <StatCard icon={Package} label="Open PO Lines" value={loaded ? meta.count : "—"} testId="stat-open-po-count" />
        <StatCard
          icon={WarningCircle}
          label="CFS Basis Lines"
          value={loaded ? rows.filter((r) => r.target_ship_basis === "cfs_must_ship_by").length : "—"}
          testId="stat-open-po-cfs"
        />
        <StatCard
          icon={ClockCounterClockwise}
          label="Missing Lead Time (D)"
          value={loaded ? rows.filter((r) => r.lead_day == null).length : "—"}
          testId="stat-open-po-missing-lead"
        />
      </div>

      {error && (
        <Alert variant="destructive" className="mb-3 rounded-sm border-[#F04438]/40 bg-[#FEF3F2]" data-testid="open-po-error-alert">
          <WarningCircle size={16} />
          <AlertTitle className="font-heading text-sm">Could not load feed</AlertTitle>
          <AlertDescription className="font-sans text-[13px]">{error}</AlertDescription>
        </Alert>
      )}

      {loading && (
        <div className="space-y-1.5" data-testid="open-po-loading-skeleton">
          {[...Array(6)].map((_, i) => (
            <Skeleton key={i} className="h-8 w-full rounded-sm" />
          ))}
        </div>
      )}

      {!loading && loaded && (
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="open-po-table-container">
          <table className="border-collapse w-full text-[13px]" data-testid="open-po-table">
            <thead>
              <tr>
                {columns.map((col) => (
                  <SortableHeader
                    key={col.label}
                    label={col.label}
                    field={col.field}
                    sortConfig={sortConfig}
                    onSort={onSort}
                    testId={`open-po-sort-${col.field}`}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((r, i) => (
                <tr key={`${r.internal_pono}-${r.item_code}-${i}`} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`open-po-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]">{r.customer || "—"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]">{r.customer_po || "—"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{r.item_code}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]">{r.description || "—"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]">{formatQty(r.qty_open)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{formatDate(r.due_date)}</td>
                  <td className={`border border-[#D0D5DD] px-2 py-1 font-medium ${isPastDue(r.target_ship_date) ? "text-[#B42318]" : "text-[#004B87]"}`}>
                    {formatDate(r.target_ship_date)}
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467] text-xs">
                    {r.target_ship_basis === "cfs_must_ship_by" ? "CFS Must-Ship-By" : "Ex-Factory Offset"}
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]">{r.lead_day ?? "—"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]">{formatMoney(r.invoice_price, r.currency)}</td>
                </tr>
              ))}
              {sortedRows.length === 0 && (
                <tr>
                  <td colSpan={10} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="open-po-no-rows">
                    No open PO demand matches the current search/filters
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {!loading && !loaded && !error && (
        <div className="border border-dashed border-[#D0D5DD] rounded-sm py-16 flex flex-col items-center gap-3 text-[#98A2B3] bg-white" data-testid="open-po-empty-state">
          <Truck size={28} weight="regular" />
          <p className="font-sans text-[13px]">Click "Fetch Open PO Demand" to pull live open purchase-order lines from OMS</p>
        </div>
      )}
    </div>
  );
};

// -------------------- BOM Alternates tab --------------------
const BomAlternatesTab = () => {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selections, setSelections] = useState({});
  const [saving, setSaving] = useState({});
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortConfig, setSortConfig] = useState({ field: "product_id", direction: "asc" });

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const { data } = await axios.get(`${API}/production-plan/alternates`);
      setItems(data.items);
      const sel = {};
      data.items.forEach((it) => {
        sel[it.product_id] = it.resolved_bom_id || it.default_bom_id;
      });
      setSelections(sel);
      setLoaded(true);
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || "Failed to load BOM alternates");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async (productId) => {
    const chosenBomId = selections[productId];
    setSaving((prev) => ({ ...prev, [productId]: true }));
    try {
      const { data } = await axios.post(`${API}/production-plan/alternates/${encodeURIComponent(productId)}`, { chosen_bom_id: chosenBomId });
      setItems((prev) => prev.map((it) => (it.product_id === productId ? data : it)));
      toast.success(`Production's choice saved for ${productId}`, { description: `Will use ${chosenBomId} everywhere this component is used` });
    } catch (err) {
      toast.error("Failed to save choice", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSaving((prev) => ({ ...prev, [productId]: false }));
    }
  };

  const clear = async (productId) => {
    setSaving((prev) => ({ ...prev, [productId]: true }));
    try {
      const { data } = await axios.delete(`${API}/production-plan/alternates/${encodeURIComponent(productId)}`);
      setItems((prev) => prev.map((it) => (it.product_id === productId ? data : it)));
      setSelections((prev) => ({ ...prev, [productId]: data.default_bom_id }));
      toast.success(`Reverted ${productId} to the default revision`);
    } catch (err) {
      toast.error("Failed to clear choice", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSaving((prev) => ({ ...prev, [productId]: false }));
    }
  };

  if (loading) {
    return (
      <div className="space-y-1.5" data-testid="bom-alternates-loading-skeleton">
        {[...Array(6)].map((_, i) => (
          <Skeleton key={i} className="h-10 w-full rounded-sm" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <Alert variant="destructive" className="rounded-sm border-[#F04438]/40 bg-[#FEF3F2]" data-testid="bom-alternates-error-alert">
        <WarningCircle size={16} />
        <AlertTitle className="font-heading text-sm">Could not load BOM alternates</AlertTitle>
        <AlertDescription className="font-sans text-[13px]">{error}</AlertDescription>
      </Alert>
    );
  }

  const onSort = (field) => {
    setSortConfig((prev) => (prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }));
  };

  const q = search.trim().toLowerCase();
  const filteredItems = items.filter((it) => {
    if (statusFilter === "resolved" && !it.resolved_bom_id) return false;
    if (statusFilter === "default" && it.resolved_bom_id) return false;
    if (!q) return true;
    return it.product_id.toLowerCase().includes(q);
  });
  const sortedItems = [...filteredItems].sort((a, b) => {
    const va = sortConfig.field === "status" ? (a.resolved_bom_id ? 1 : 0) : a.product_id.toLowerCase();
    const vb = sortConfig.field === "status" ? (b.resolved_bom_id ? 1 : 0) : b.product_id.toLowerCase();
    const cmp = compareValues(va, vb);
    return sortConfig.direction === "asc" ? cmp : -cmp;
  });

  return (
    <div>
      <p className="text-[13px] text-[#475467] mb-3">
        These products/sub-assemblies currently have more than one SAP-consistent BOM revision using genuinely
        different raw materials (a real production alternate, not just an admin revision history). Purchasing
        defaults to the highest revision until production makes a standing choice here - the choice applies
        globally, everywhere this component is used (BOM Explorer, Purchasing Plan, MRP).
      </p>

      {loaded && items.length > 0 && (
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-2.5 flex items-center gap-3 flex-wrap mb-3">
          <div className="relative">
            <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <input
              type="text"
              placeholder="Search product ID..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 w-60 pl-7 pr-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
              data-testid="bom-alternates-search-input"
            />
          </div>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="h-8 w-52 text-[13px] rounded-sm border-[#D0D5DD]" data-testid="bom-alternates-status-filter">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" data-testid="bom-alternates-status-filter-all">All Statuses</SelectItem>
              <SelectItem value="resolved" data-testid="bom-alternates-status-filter-resolved">Production Choice Made</SelectItem>
              <SelectItem value="default" data-testid="bom-alternates-status-filter-default">Auto-Default Only</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-xs text-[#475467] ml-auto" data-testid="bom-alternates-result-count">
            Showing {sortedItems.length} of {items.length}
          </span>
        </div>
      )}

      {loaded && items.length === 0 ? (
        <div className="border border-dashed border-[#D0D5DD] rounded-sm py-16 flex flex-col items-center gap-3 text-[#98A2B3] bg-white" data-testid="bom-alternates-empty-state">
          <TreeStructure size={28} weight="regular" />
          <p className="font-sans text-[13px]">No genuine BOM alternates detected yet in the explored catalog</p>
        </div>
      ) : (
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="bom-alternates-table-container">
          <table className="border-collapse w-full text-[13px]" data-testid="bom-alternates-table">
            <thead>
              <tr>
                <SortableHeader label="Product ID" field="product_id" sortConfig={sortConfig} onSort={onSort} testId="bom-alternates-sort-product-id" />
                <SortableHeader label="Status" field="status" sortConfig={sortConfig} onSort={onSort} testId="bom-alternates-sort-status" />
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Choose BOM Revision</th>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sortedItems.map((it, i) => (
                <tr key={it.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`bom-alternate-row-${i}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium text-[#101828]">{it.product_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    {it.resolved_bom_id ? (
                      <span className="inline-flex items-center gap-1 text-[#027A48] text-xs" data-testid={`bom-alternate-status-resolved-${i}`}>
                        <CheckCircle size={13} weight="fill" /> Production choice: {it.resolved_bom_id}
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-[#B54708] text-xs" data-testid={`bom-alternate-status-default-${i}`}>
                        <WarningCircle size={13} weight="fill" /> Auto-default: {it.default_bom_id}
                      </span>
                    )}
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    <Select value={selections[it.product_id]} onValueChange={(v) => setSelections((prev) => ({ ...prev, [it.product_id]: v }))}>
                      <SelectTrigger className="h-8 w-72 text-[13px] rounded-sm border-[#D0D5DD]" data-testid={`bom-alternate-select-${i}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {it.options.map((opt) => (
                          <SelectItem key={opt.bom_id} value={opt.bom_id} data-testid={`bom-alternate-option-${i}-${opt.bom_id}`}>
                            {opt.bom_id} {opt.description ? `— ${opt.description}` : ""} (e.g. {opt.sample_item_id})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5">
                    <div className="flex items-center gap-1.5">
                      <Button
                        size="sm"
                        onClick={() => save(it.product_id)}
                        disabled={saving[it.product_id] || selections[it.product_id] === it.resolved_bom_id}
                        className="h-7 text-xs rounded-sm bg-[#004B87] hover:bg-[#003A6A] text-white"
                        data-testid={`bom-alternate-save-${i}`}
                      >
                        Save Choice
                      </Button>
                      {it.resolved_bom_id && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => clear(it.product_id)}
                          disabled={saving[it.product_id]}
                          className="h-7 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
                          data-testid={`bom-alternate-clear-${i}`}
                        >
                          Revert to Default
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {sortedItems.length === 0 && items.length > 0 && (
                <tr>
                  <td colSpan={4} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="bom-alternates-no-match">
                    No components match the current search/filters
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

// -------------------- MRP Plan tab --------------------
const MrpPlanTab = () => {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [customer, setCustomer] = useState("");
  const [groupBy, setGroupBy] = useState("flat"); // "flat" | "month" | "week"
  const [expanded, setExpanded] = useState(new Set());
  const [search, setSearch] = useState("");
  const [shortageOnly, setShortageOnly] = useState(false);
  const [sortConfig, setSortConfig] = useState({ field: "total_net_qty", direction: "desc" });

  const POLL_INTERVAL_MS = 3000;
  const MAX_POLL_MS = 15 * 60 * 1000;

  const generate = async () => {
    setLoading(true);
    setError(null);
    setElapsedSeconds(0);
    const startedAt = Date.now();
    try {
      const { data } = await axios.post(`${API}/production-plan/mrp/generate`, null, { params: customer.trim() ? { customer: customer.trim() } : {} });
      const jobId = data.job_id;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        setElapsedSeconds(Math.round((Date.now() - startedAt) / 1000));
        const { data: job } = await axios.get(`${API}/production-plan/mrp/status/${jobId}`);
        if (job.status === "done") {
          setPlan(job.result);
          setExpanded(new Set());
          toast.success("MRP plan generated", { description: `${job.result.components.length} component(s) from ${job.result.total_po_lines} open PO line(s)` });
          break;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "Failed to generate MRP plan");
        }
        if (Date.now() - startedAt > MAX_POLL_MS) {
          throw new Error("MRP plan generation is taking too long. Please try again.");
        }
      }
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Failed to generate MRP plan";
      setError(detail);
      toast.error("MRP plan generation failed", { description: detail });
    } finally {
      setLoading(false);
    }
  };

  const toggleExpand = (productId) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(productId) ? next.delete(productId) : next.add(productId);
      return next;
    });
  };

  const groupedLines = (lines) => {
    if (groupBy === "flat") return null;
    const buckets = {};
    lines.forEach((l) => {
      const key = !l.order_by_date ? "Unknown" : groupBy === "week" ? isoWeekLabel(l.order_by_date) : l.order_by_date.slice(0, 7);
      if (!buckets[key]) buckets[key] = { key, gross: 0, net: 0, count: 0 };
      buckets[key].gross += l.gross_qty;
      buckets[key].net += l.net_qty;
      buckets[key].count += 1;
    });
    return Object.values(buckets).sort((a, b) => a.key.localeCompare(b.key));
  };

  const totalNet = plan ? plan.components.reduce((s, c) => s + c.total_net_qty, 0) : 0;
  const totalGross = plan ? plan.components.reduce((s, c) => s + c.total_gross_qty, 0) : 0;
  const shortageCount = plan ? plan.components.filter((c) => c.total_net_qty > 0).length : 0;

  const onSort = (field) => {
    setSortConfig((prev) => (prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }));
  };

  const q = search.trim().toLowerCase();
  const filteredComponents = plan
    ? plan.components.filter((c) => {
        if (shortageOnly && !(c.total_net_qty > 0)) return false;
        if (!q) return true;
        return c.product_id.toLowerCase().includes(q) || (c.description || "").toLowerCase().includes(q);
      })
    : [];
  const getMrpSortValue = (c, field) => (field === "product_id" ? c.product_id.toLowerCase() : c[field]);
  const sortedComponents = [...filteredComponents].sort((a, b) => {
    const cmp = compareValues(getMrpSortValue(a, sortConfig.field), getMrpSortValue(b, sortConfig.field));
    return sortConfig.direction === "asc" ? cmp : -cmp;
  });
  const filteredTotalNet = sortedComponents.reduce((s, c) => s + c.total_net_qty, 0);
  const filteredTotalGross = sortedComponents.reduce((s, c) => s + c.total_gross_qty, 0);

  const mrpColumns = [
    { label: "Product ID", field: "product_id" },
    { label: "Description" },
    { label: "Lead Time (D)", field: "lead_time_days" },
    { label: "MSL", field: "msl" },
    { label: "On-Hand", field: "on_hand_qty" },
    { label: "Total Gross Qty", field: "total_gross_qty" },
    { label: "Total Net Qty", field: "total_net_qty" },
  ];

  return (
    <div>
      <div className="bg-white border border-[#D0D5DD] rounded-sm p-2.5 flex items-center gap-3 flex-wrap mb-3">
        <input
          type="text"
          placeholder="Filter by customer (optional)..."
          value={customer}
          onChange={(e) => setCustomer(e.target.value)}
          disabled={loading}
          className="h-8 w-56 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
          data-testid="mrp-customer-filter"
        />
        <Button
          type="button"
          onClick={generate}
          disabled={loading}
          className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white rounded-sm px-4 text-[13px] font-bold transition-colors"
          data-testid="generate-mrp-plan-button"
        >
          <ListChecks size={14} className="mr-1.5" />
          {loading ? `Generating... (${elapsedSeconds}s)` : plan ? "Regenerate MRP Plan" : "Generate MRP Plan"}
        </Button>
        {plan && (
          <div className="flex items-center gap-1.5">
            <label className="font-heading text-xs font-bold text-[#475467] uppercase">View demand by</label>
            <Select value={groupBy} onValueChange={setGroupBy}>
              <SelectTrigger className="h-8 w-36 text-[13px] rounded-sm border-[#D0D5DD]" data-testid="mrp-groupby-select">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="flat" data-testid="mrp-groupby-flat">Flat List</SelectItem>
                <SelectItem value="month" data-testid="mrp-groupby-month">By Month</SelectItem>
                <SelectItem value="week" data-testid="mrp-groupby-week">By Week</SelectItem>
              </SelectContent>
            </Select>
          </div>
        )}
        {plan && (
          <div className="flex items-center gap-1.5 text-xs text-[#475467] ml-auto" data-testid="mrp-po-data-as-of">
            <CalendarBlank size={13} weight="bold" />
            PO data watermark: {plan.po_data_as_of ? new Date(plan.po_data_as_of).toLocaleString() : "—"}
          </div>
        )}
      </div>

      {plan && (
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-2.5 flex items-center gap-3 flex-wrap mb-3">
          <div className="relative">
            <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <input
              type="text"
              placeholder="Search product ID or description..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 w-64 pl-7 pr-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
              data-testid="mrp-search-input"
            />
          </div>
          <label className="flex items-center gap-1.5 text-xs text-[#344054] cursor-pointer select-none">
            <input
              type="checkbox"
              checked={shortageOnly}
              onChange={(e) => setShortageOnly(e.target.checked)}
              className="accent-[#004B87]"
              data-testid="mrp-shortage-only-toggle"
            />
            Shortages only
          </label>
          <span className="text-xs text-[#475467] ml-auto" data-testid="mrp-result-count">
            Showing {sortedComponents.length} of {plan.components.length} component(s)
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <StatCard icon={Package} label="PO Lines Considered" value={plan ? plan.total_po_lines : "—"} testId="stat-mrp-po-lines" />
        <StatCard icon={Database} label="Components in Demand" value={plan ? plan.components.length : "—"} testId="stat-mrp-components" />
        <StatCard icon={WarningCircle} label="Components Short" value={plan ? shortageCount : "—"} tone="text-[#B42318]" testId="stat-mrp-shortages" />
        <StatCard icon={ListChecks} label="Total Net Procurement Qty" value={plan ? formatQty(totalNet) : "—"} tone="text-[#004B87]" testId="stat-mrp-net-qty" />
      </div>

      {error && (
        <Alert variant="destructive" className="mb-3 rounded-sm border-[#F04438]/40 bg-[#FEF3F2]" data-testid="mrp-error-alert">
          <WarningCircle size={16} />
          <AlertTitle className="font-heading text-sm">Generation failed</AlertTitle>
          <AlertDescription className="font-sans text-[13px]">{error}</AlertDescription>
        </Alert>
      )}

      {plan && plan.unresolved_items.length > 0 && (
        <div className="mb-3 bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-2.5" data-testid="mrp-unresolved-items-section">
          <span className="inline-flex items-center gap-2 font-heading text-xs font-bold text-[#B54708]">
            <WarningCircle size={15} weight="fill" />
            {plan.unresolved_items.length} open-PO item{plan.unresolved_items.length === 1 ? "" : "s"} could not be mapped to a SAP BOM - excluded
          </span>
          <div className="mt-1.5 text-xs text-[#7A4504]">
            {plan.unresolved_items.map((u) => `${u.item_code} (${u.reason})`).join(" · ")}
          </div>
        </div>
      )}

      {loading && (
        <div className="space-y-1.5" data-testid="mrp-loading-skeleton">
          <p className="text-[13px] text-[#475467] mb-2">
            Pulling open PO demand from OMS and exploding live SAP BOMs for every item involved ({elapsedSeconds}s elapsed)...
          </p>
          {[...Array(8)].map((_, i) => (
            <Skeleton key={i} className="h-8 w-full rounded-sm" />
          ))}
        </div>
      )}

      {!loading && plan && (
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="mrp-table-container">
          <table className="border-collapse w-full text-[13px]" data-testid="mrp-table">
            <thead>
              <tr>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5"></th>
                {mrpColumns.map((col) => (
                  <SortableHeader
                    key={col.label}
                    label={col.label}
                    field={col.field}
                    sortConfig={sortConfig}
                    onSort={col.field ? onSort : undefined}
                    testId={col.field ? `mrp-sort-${col.field}` : undefined}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedComponents.map((c, i) => {
                const isExpanded = expanded.has(c.product_id);
                const groups = groupedLines(c.demand_lines);
                return (
                  <Fragment key={c.product_id}>
                    <tr
                      className={`${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] cursor-pointer transition-colors duration-150`}
                      onClick={() => toggleExpand(c.product_id)}
                      data-testid={`mrp-row-${i}`}
                    >
                      <td className="border border-[#D0D5DD] px-1.5 py-1 text-center text-[#667085]">
                        {isExpanded ? <CaretDown size={12} weight="bold" /> : <CaretRight size={12} weight="bold" />}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{c.product_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]">{c.description || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]">{c.lead_time_days ?? "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]">{formatQty(c.msl)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]">{formatQty(c.on_hand_qty)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]" data-testid={`mrp-gross-${i}`}>
                        {formatQty(c.total_gross_qty)}
                      </td>
                      <td
                        className={`border border-[#D0D5DD] px-2 py-1 text-right tabular-nums font-bold ${c.total_net_qty > 0 ? "text-[#B42318]" : "text-[#027A48]"}`}
                        data-testid={`mrp-net-${i}`}
                      >
                        {formatQty(c.total_net_qty)}
                      </td>
                    </tr>
                    {isExpanded && groupBy === "flat" &&
                      c.demand_lines.map((l, j) => (
                        <tr key={`${c.product_id}-${j}`} className="bg-[#F5FAFF]" data-testid={`mrp-demand-line-${i}-${j}`}>
                          <td className="border border-[#D0D5DD]"></td>
                          <td className="border border-[#D0D5DD] px-2 py-1 pl-6 text-xs text-[#475467]" colSpan={2}>
                            {l.item_code} · {l.customer || "—"} · PO {l.customer_po || l.internal_pono || "—"}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-xs text-[#475467]" colSpan={2}>
                            Ship {formatDate(l.target_ship_date)} → Order by{" "}
                            <span className={isPastDue(l.order_by_date) ? "text-[#B42318] font-bold" : "text-[#004B87] font-bold"}>
                              {formatDate(l.order_by_date)}
                            </span>
                            {l.lead_time_missing && (
                              <span className="ml-1.5 text-[#B54708]" title="No Lead Time set for this component - order-by date defaults to the ship date">
                                (lead time unset)
                              </span>
                            )}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-xs text-[#475467]">{formatQty(l.gross_qty)}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-xs font-bold text-[#475467]">{formatQty(l.net_qty)}</td>
                        </tr>
                      ))}
                    {isExpanded && groupBy !== "flat" &&
                      groups.map((g) => (
                        <tr key={`${c.product_id}-${g.key}`} className="bg-[#F5FAFF]" data-testid={`mrp-demand-bucket-${i}-${g.key}`}>
                          <td className="border border-[#D0D5DD]"></td>
                          <td className="border border-[#D0D5DD] px-2 py-1 pl-6 text-xs text-[#475467]" colSpan={3}>
                            {groupBy === "week" ? "Week" : "Month"} {g.key} ({g.count} PO line{g.count === 1 ? "" : "s"})
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1"></td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-xs text-[#475467]">{formatQty(g.gross)}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-xs font-bold text-[#475467]">{formatQty(g.net)}</td>
                        </tr>
                      ))}
                  </Fragment>
                );
              })}
              {sortedComponents.length === 0 && (
                <tr>
                  <td colSpan={8} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="mrp-no-components">
                    {plan.components.length === 0
                      ? "No purchasable leaf components found against the current open PO demand"
                      : "No components match the current search/filters"}
                  </td>
                </tr>
              )}
            </tbody>
            {sortedComponents.length > 0 && (
              <tfoot>
                <tr className="bg-[#EAECF0]" data-testid="mrp-totals-row">
                  <td className="border border-[#D0D5DD]" colSpan={6}></td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-heading font-bold text-[#101828]">{formatQty(filteredTotalGross)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-heading font-bold text-[#B42318]">{formatQty(filteredTotalNet)}</td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}

      {!loading && !plan && !error && (
        <div className="border border-dashed border-[#D0D5DD] rounded-sm py-16 flex flex-col items-center gap-3 text-[#98A2B3] bg-white" data-testid="mrp-empty-state">
          <ListChecks size={28} weight="regular" />
          <p className="font-sans text-[13px]">
            Click "Generate MRP Plan" to net every open PO's component demand against current inventory + MSL
          </p>
        </div>
      )}
    </div>
  );
};

export default function ProductionPlanPage() {
  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />

      <header className="h-12 bg-[#004B87] shadow-[0_1px_3px_0_rgba(16,24,40,0.1)] flex items-center justify-between px-4 shrink-0 z-10">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2.5" data-testid="app-title">
            <Database size={18} weight="bold" className="text-white" />
            <span className="font-heading text-sm font-bold text-white tracking-tight">SAP BOM Explorer</span>
            <span className="font-sans text-xs text-white/60 hidden sm:inline">| Production Plan</span>
          </div>
          <NavTabs />
        </div>
      </header>

      <main className="flex-1 overflow-auto p-4">
        <Tabs defaultValue="open-po" className="w-full">
          <TabsList className="mb-3" data-testid="production-plan-tabs">
            <TabsTrigger value="open-po" data-testid="production-plan-tab-open-po">
              <Truck size={14} className="mr-1.5" /> Open PO Demand
            </TabsTrigger>
            <TabsTrigger value="alternates" data-testid="production-plan-tab-alternates">
              <TreeStructure size={14} className="mr-1.5" /> BOM Alternates
            </TabsTrigger>
            <TabsTrigger value="mrp" data-testid="production-plan-tab-mrp">
              <ListChecks size={14} className="mr-1.5" /> MRP Plan
            </TabsTrigger>
          </TabsList>
          <TabsContent value="open-po" data-testid="production-plan-content-open-po">
            <OpenPoDemandTab />
          </TabsContent>
          <TabsContent value="alternates" data-testid="production-plan-content-alternates">
            <BomAlternatesTab />
          </TabsContent>
          <TabsContent value="mrp" data-testid="production-plan-content-mrp">
            <MrpPlanTab />
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
