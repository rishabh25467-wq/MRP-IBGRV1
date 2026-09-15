import { useState, useEffect, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import {
  Package,
  ClockCounterClockwise,
  WarningCircle,
  CurrencyCircleDollar,
  Database,
  Shield,
  ShoppingCartSimple,
  CaretDown,
  CaretRight,
  CaretUp,
  FileArrowDown,
  ArrowsOutSimple,
  ArrowsInSimple,
  ArrowClockwise,
  XCircle,
  MagnifyingGlass,
  ChartBar,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const StatCard = ({ icon: Icon, label, value, testId }) => (
  <div
    className="bg-white border border-[#D0D5DD] rounded-sm p-3 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] flex flex-col gap-1.5"
    data-testid={testId}
  >
    <div className="flex items-center gap-1.5 text-[#475467]">
      <Icon size={14} weight="bold" />
      <span className="font-heading text-xs font-bold uppercase tracking-wider">{label}</span>
    </div>
    <span className="font-sans text-2xl font-bold tabular-nums text-[#1D2939]">{value}</span>
  </div>
);

const formatMonth = (monthStr) => {
  if (!monthStr) return "—";
  const [year, month] = monthStr.split("-").map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString("en-US", { month: "short", year: "numeric" });
};

const formatQty = (value) => (value == null ? "—" : value.toLocaleString("en-IN", { maximumFractionDigits: 2 }));

const formatMoney = (value, currency) =>
  value == null ? "—" : `${currency || ""} ${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const FRESHNESS_SLA_HOURS = 12;

const formatRelativeTime = (isoString) => {
  if (!isoString) return null;
  const diffMs = Date.now() - new Date(isoString).getTime();
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin} min ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hr${diffHr === 1 ? "" : "s"} ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay} day${diffDay === 1 ? "" : "s"} ago`;
};

const isStale = (isoString) => {
  if (!isoString) return false;
  const diffHrs = (Date.now() - new Date(isoString).getTime()) / 3600000;
  return diffHrs > FRESHNESS_SLA_HOURS;
};

const getDefaultMonth = () => {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};

const groupByCategory = (components) => {
  const groups = {};
  components.forEach((c) => {
    const cat = c.category || "Uncategorized";
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push(c);
  });
  return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
};

const getPlanSortValue = (component, field) => {
  if (field === "product_id") return (component.product_id || "").toLowerCase();
  if (field === "on_hand") return component.on_hand_qty ?? -Infinity;
  if (field === "msl") return component.msl ?? -Infinity;
  if (field.startsWith("qty:")) return component.qty_by_month[field.slice(4)] ?? -Infinity;
  if (field.startsWith("netqty:")) return component.net_qty_by_month[field.slice(7)] ?? -Infinity;
  if (field.startsWith("value:")) return component.value_by_month[field.slice(6)] ?? -Infinity;
  if (field.startsWith("netvalue:")) return component.net_value_by_month[field.slice(9)] ?? -Infinity;
  return 0;
};

const sortItems = (items, sortConfig) => {
  if (!sortConfig.field) return items;
  const sorted = [...items].sort((a, b) => {
    const va = getPlanSortValue(a, sortConfig.field);
    const vb = getPlanSortValue(b, sortConfig.field);
    if (va < vb) return sortConfig.direction === "asc" ? -1 : 1;
    if (va > vb) return sortConfig.direction === "asc" ? 1 : -1;
    return 0;
  });
  return sorted;
};

export default function PurchasingPlanPage() {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [lastGenerated, setLastGenerated] = useState(null);
  const [warningsOpen, setWarningsOpen] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [selectedMonth, setSelectedMonth] = useState(getDefaultMonth());
  const [collapsedCategories, setCollapsedCategories] = useState(new Set());
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [sortConfig, setSortConfig] = useState({ field: null, direction: "asc" });
  const [retrying, setRetrying] = useState(false);
  const [salesPlanOpen, setSalesPlanOpen] = useState(false);
  const [salesPlanMonth, setSalesPlanMonth] = useState(getDefaultMonth());
  const [salesPlanItems, setSalesPlanItems] = useState([]);
  const [salesPlanLoading, setSalesPlanLoading] = useState(false);
  const [salesPlanError, setSalesPlanError] = useState(null);
  const [salesPlanSearch, setSalesPlanSearch] = useState("");
  const [expandedSalesPlanRows, setExpandedSalesPlanRows] = useState(new Set());
  const [overrideInputs, setOverrideInputs] = useState({});
  const [savingOverride, setSavingOverride] = useState({});
  const [supplierSummaries, setSupplierSummaries] = useState({});
  const [expandedSupplierSplitRows, setExpandedSupplierSplitRows] = useState(new Set());

  const toggleSupplierSplit = (productId) => {
    setExpandedSupplierSplitRows((prev) => {
      const next = new Set(prev);
      if (next.has(productId)) {
        next.delete(productId);
      } else {
        next.add(productId);
      }
      return next;
    });
  };

  const toggleCategoryCollapse = (category) => {
    setCollapsedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  };

  const toggleSort = (field) => {
    setSortConfig((prev) =>
      prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }
    );
  };

  const POLL_INTERVAL_MS = 3000;
  const MAX_POLL_MS = 15 * 60 * 1000;

  const generatePlan = async () => {
    setLoading(true);
    setError(null);
    setElapsedSeconds(0);
    const startedAt = Date.now();
    try {
      const { data } = await axios.post(`${API}/purchasing-plan/generate`, { target_month: selectedMonth });
      const jobId = data.job_id;

      // Long-running live OMS+SAP pipeline (can take 1-3+ minutes) - poll a
      // status endpoint instead of holding one HTTP request open, since that
      // would exceed the platform's ingress/proxy timeout.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        setElapsedSeconds(Math.round((Date.now() - startedAt) / 1000));
        const { data: job } = await axios.get(`${API}/purchasing-plan/status/${jobId}`);
        if (job.status === "done") {
          setPlan(job.result);
          setLastGenerated(new Date());
          setCollapsedCategories(new Set());
          setCategoryFilter("all");
          setSortConfig({ field: null, direction: "asc" });
          toast.success("Purchasing plan generated", {
            description: `${job.result.components.length} components across ${job.result.months.length} months`,
          });
          break;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "Failed to generate purchasing plan");
        }
        if (Date.now() - startedAt > MAX_POLL_MS) {
          throw new Error("Purchasing plan generation is taking too long. Please try again.");
        }
      }
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Failed to generate purchasing plan";
      setError(detail);
      toast.error("Purchasing plan generation failed", { description: detail });
    } finally {
      setLoading(false);
    }
  };

  const loadSalesPlan = async (month) => {
    setSalesPlanLoading(true);
    setSalesPlanError(null);
    try {
      const { data } = await axios.get(`${API}/sales-plan`, { params: { month } });
      setSalesPlanItems(data.items);
    } catch (err) {
      setSalesPlanError(err?.response?.data?.detail || err.message || "Failed to load sales plan");
    } finally {
      setSalesPlanLoading(false);
    }
  };

  // Bulk-fetch app-managed supplier/quota assignments for every component in
  // the plan, so a "Suppliers" badge can be shown inline per row (chunked -
  // a full plan can have 800+ leaf components, too many product_ids for one
  // query string).
  const loadSupplierSummaries = async (productIds) => {
    if (!productIds.length) {
      setSupplierSummaries({});
      return;
    }
    const CHUNK_SIZE = 200;
    const chunks = [];
    for (let i = 0; i < productIds.length; i += CHUNK_SIZE) {
      chunks.push(productIds.slice(i, i + CHUNK_SIZE));
    }
    try {
      const results = await Promise.all(
        chunks.map((chunk) => axios.get(`${API}/part-suppliers/bulk`, { params: { product_ids: chunk.join(",") } }))
      );
      const merged = {};
      results.forEach(({ data }) => Object.assign(merged, data));
      setSupplierSummaries(merged);
    } catch (err) {
      // Non-critical enrichment - the plan itself is still usable without it.
      console.warn("Could not load supplier assignments for plan components", err);
    }
  };

  useEffect(() => {
    if (plan?.components?.length) {
      loadSupplierSummaries(plan.components.map((c) => c.product_id));
    } else {
      setSupplierSummaries({});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan]);

  useEffect(() => {
    if (salesPlanOpen) {
      loadSalesPlan(salesPlanMonth);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [salesPlanOpen, salesPlanMonth]);

  const toggleSalesPlanRow = (partNo) => {
    setExpandedSalesPlanRows((prev) => {
      const next = new Set(prev);
      next.has(partNo) ? next.delete(partNo) : next.add(partNo);
      return next;
    });
  };

  const filteredSalesPlanItems = salesPlanItems.filter((it) => {
    const q = salesPlanSearch.trim().toLowerCase();
    return !q || it.part_no.toLowerCase().includes(q) || (it.description || "").toLowerCase().includes(q);
  });

  const months = plan?.months || [];
  const currency = plan?.components?.find((c) => c.currency)?.currency || "";
  const fetchErrorPartNos = plan
    ? plan.missing_boms.filter((mb) => mb.confidence === "fetch_error").map((mb) => mb.part_no)
    : [];

  const retryFailedLookups = async () => {
    if (fetchErrorPartNos.length === 0) return;
    setRetrying(true);
    try {
      const { data } = await axios.post(`${API}/purchasing-plan/retry-missing`, { part_nos: fetchErrorPartNos });
      const resolvedNow = new Set(data.results.filter((r) => r.resolved).map((r) => r.part_no));
      const updatedByPartNo = new Map(data.results.filter((r) => !r.resolved).map((r) => [r.part_no, r]));
      setPlan((prev) => ({
        ...prev,
        missing_boms: prev.missing_boms
          .filter((mb) => !resolvedNow.has(mb.part_no))
          .map((mb) => {
            const updated = updatedByPartNo.get(mb.part_no);
            return updated ? { ...mb, sap_id: updated.sap_id, confidence: updated.confidence, reason: updated.reason } : mb;
          }),
      }));
      if (resolvedNow.size > 0) {
        toast.success(`${resolvedNow.size} part${resolvedNow.size === 1 ? "" : "s"} now resolved in SAP`, {
          description: "Regenerate the plan to include them in the totals.",
        });
      } else {
        toast.info("Still unreachable", { description: "Those parts couldn't be reached in SAP just now - try again shortly." });
      }
    } catch (err) {
      toast.error("Retry failed", { description: err?.response?.data?.detail || err.message || "Could not reach the server" });
    } finally {
      setRetrying(false);
    }
  };

  const saveOverride = async (partNo) => {
    const sapId = (overrideInputs[partNo] || "").trim();
    if (!sapId) return;
    setSavingOverride((prev) => ({ ...prev, [partNo]: true }));
    try {
      const { data } = await axios.post(`${API}/purchasing-plan/part-overrides`, { part_no: partNo, sap_id: sapId });
      if (data.resolved) {
        setPlan((prev) => ({
          ...prev,
          missing_boms: prev.missing_boms.filter((mb) => mb.part_no !== partNo),
        }));
        toast.success(`${partNo} resolved to ${sapId} in SAP`, {
          description: "Regenerate the plan to include it in the totals.",
        });
      } else {
        setPlan((prev) => ({
          ...prev,
          missing_boms: prev.missing_boms.map((mb) =>
            mb.part_no === partNo
              ? { ...mb, sap_id: sapId, confidence: data.confidence, reason: data.reason }
              : mb
          ),
        }));
        toast.error(`${sapId} still not resolvable in SAP`, { description: data.reason });
      }
      setOverrideInputs((prev) => ({ ...prev, [partNo]: "" }));
    } catch (err) {
      toast.error("Failed to save override", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSavingOverride((prev) => ({ ...prev, [partNo]: false }));
    }
  };

  const availableCategories = plan
    ? Array.from(new Set(plan.components.map((c) => c.category || "Uncategorized"))).sort()
    : [];
  const filteredComponents =
    plan && categoryFilter !== "all"
      ? plan.components.filter((c) => (c.category || "Uncategorized") === categoryFilter)
      : plan?.components || [];

  const totalValueByMonth = (month) =>
    filteredComponents.reduce((sum, c) => sum + (c.value_by_month[month] || 0), 0);

  const totalNetValueByMonth = (month) =>
    filteredComponents.reduce((sum, c) => sum + (c.net_value_by_month[month] || 0), 0);

  const categoryTotalOnHand = (items) => items.reduce((sum, c) => sum + (c.on_hand_qty || 0), 0);
  const categoryTotalMsl = (items) => items.reduce((sum, c) => sum + (c.msl || 0), 0);
  const categoryTotalQty = (items, month) => items.reduce((sum, c) => sum + (c.qty_by_month[month] || 0), 0);
  const categoryTotalNetQty = (items, month) => items.reduce((sum, c) => sum + (c.net_qty_by_month[month] || 0), 0);
  const categoryTotalValue = (items, month) => items.reduce((sum, c) => sum + (c.value_by_month[month] || 0), 0);
  const categoryTotalNetValue = (items, month) => items.reduce((sum, c) => sum + (c.net_value_by_month[month] || 0), 0);

  // Rounds a supplier's raw split qty to a practical, shippable lot size -
  // suppliers can't ship 257,394.6 units, they ship round lots. Combined
  // qty+price rule (no exact thresholds given by user, so using a sensible
  // default, easy to retune here if needed): costly/precision items (unit
  // cost >= 20) always round tight (nearest 10) since a lot swing has real
  // money impact; cheap, high-volume items (unit cost < 20 AND qty >= 200,
  // e.g. fasteners/washers) round coarse (nearest 100) since suppliers of
  // those don't care about single-digit precision anyway.
  const roundToLotSize = (qty, unitCost) => {
    const isCostly = unitCost != null && unitCost >= 20;
    const base = !isCostly && qty >= 200 ? 100 : 10;
    return Math.round(qty / base) * base;
  };

  // Auto-splits a component's Net Purchase Qty (per month) across its
  // assigned suppliers using each supplier's confirmed AI Quota %
  // (quota_arrangement_service - the Purchasing Strategy > Quota
  // Allocation page is where that % gets set/confirmed). Only suppliers
  // with a quota_percent on file are split; unassigned suppliers are
  // omitted rather than guessed at. Every supplier's raw share is
  // independently rounded to a shippable lot size (see roundToLotSize) -
  // per user's explicit goal ("split the business, not to the dot, in a
  // way suppliers can ship"), the resulting per-supplier total may drift
  // slightly from the exact Net Purchase Qty, which is an accepted
  // trade-off for clean, orderable lot sizes on every row (forcing an
  // exact reconciliation would undo the rounding for whichever supplier
  // absorbs the difference, defeating the point for that row).
  const computeSupplierSplit = (component) => {
    const assignments = (supplierSummaries[component.product_id] || []).filter((a) => a.quota_percent != null);
    return assignments.map((a) => ({
      ...a,
      qtyByMonth: Object.fromEntries(
        months.map((m) => {
          const netQty = component.net_qty_by_month[m] || 0;
          return [m, roundToLotSize((netQty * a.quota_percent) / 100, component.unit_cost)];
        })
      ),
    }));
  };

  const exportToExcel = () => {
    if (!plan) return;
    const rows = filteredComponents.map((c) => {
      const row = {
        "Product ID": c.product_id,
        Description: c.description || "",
        Category: c.category || "Uncategorized",
        UOM: c.unit_of_measure || "",
        "On-Hand Inventory": c.on_hand_qty ?? "",
        MSL: c.msl ?? "",
      };
      months.forEach((m) => {
        row[`${formatMonth(m)} Gross Qty`] = c.qty_by_month[m] ?? "";
        row[`${formatMonth(m)} Net Purchase Qty`] = c.net_qty_by_month[m] ?? "";
        row[`${formatMonth(m)} Gross Value`] = c.value_by_month[m] ?? "";
        row[`${formatMonth(m)} Net Value`] = c.net_value_by_month[m] ?? "";
      });
      return row;
    });
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(rows), "Purchasing Plan");

    if (plan.missing_boms.length > 0) {
      const missingRows = plan.missing_boms.map((mb) => ({
        Status: mb.confidence === "fetch_error" ? "Unresolved (retry)" : "Confirmed no BOM",
        "OMS Part No": mb.part_no,
        "Mapped SAP ID": mb.sap_id || "",
        Reason: mb.reason,
      }));
      XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(missingRows), "Missing BOMs");
    }

    const filename = `PurchasingPlan_${months.join("_")}.xlsx`;
    XLSX.writeFile(workbook, filename);
    toast.success("Excel file downloaded", { description: filename });
  };

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />

      {/* Header */}
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Procurement Planning</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>

      {/* Toolbar */}
      <div className="bg-white border-b border-[#D0D5DD] p-2 flex items-center gap-3 shrink-0 flex-wrap">
        <div className="flex items-center gap-1.5">
          <label htmlFor="purchasing-plan-month-picker" className="font-heading text-xs font-bold text-[#475467] uppercase">
            Target Month
          </label>
          <input
            id="purchasing-plan-month-picker"
            type="month"
            value={selectedMonth}
            onChange={(e) => setSelectedMonth(e.target.value)}
            disabled={loading}
            className="h-8 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
            data-testid="purchasing-plan-month-picker"
          />
        </div>
        <Button
          type="button"
          onClick={generatePlan}
          disabled={loading}
          className="h-8 bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D] text-white rounded-sm px-4 text-[13px] font-bold transition-colors"
          data-testid="generate-purchasing-plan-button"
        >
          <ShoppingCartSimple size={14} className="mr-1.5" />
          {loading ? `Generating... (${elapsedSeconds}s)` : plan ? "Regenerate Purchasing Plan" : "Generate Purchasing Plan"}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => setSalesPlanOpen(true)}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
          data-testid="sales-plan-lookup-button"
        >
          <ChartBar size={14} className="mr-1.5" />
          Sales Plan Lookup
        </Button>
        {plan && (
          <Badge
            variant="outline"
            className="bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] rounded font-sans text-xs h-8 flex items-center"
            data-testid="purchasing-plan-months-badge"
          >
            {months.map(formatMonth).join(" & ")}
          </Badge>
        )}
        {plan && (
          <div className="flex items-center gap-1.5">
            <label htmlFor="purchasing-plan-category-filter" className="font-heading text-xs font-bold text-[#475467] uppercase">
              Category
            </label>
            <Select value={categoryFilter} onValueChange={setCategoryFilter}>
              <SelectTrigger
                id="purchasing-plan-category-filter"
                className="h-8 w-48 text-[13px] rounded-sm border-[#D0D5DD]"
                data-testid="purchasing-plan-category-filter"
              >
                <SelectValue placeholder="All Categories" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all" data-testid="category-filter-option-all">
                  All Categories
                </SelectItem>
                {availableCategories.map((cat) => (
                  <SelectItem key={cat} value={cat} data-testid={`category-filter-option-${cat}`}>
                    {cat}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        <Button
          type="button"
          onClick={exportToExcel}
          disabled={!plan}
          className="h-8 bg-[#027A48] hover:bg-[#02623A] text-white text-xs rounded-sm transition-colors"
          data-testid="export-purchasing-plan-excel-button"
        >
          <FileArrowDown size={13} className="mr-1.5" />
          Export to Excel
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => setCollapsedCategories(new Set())}
          disabled={!plan}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054] transition-colors"
          data-testid="expand-all-categories-button"
        >
          <ArrowsOutSimple size={13} className="mr-1.5" />
          Expand Categories
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => plan && setCollapsedCategories(new Set(groupByCategory(filteredComponents).map(([cat]) => cat)))}
          disabled={!plan}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054] transition-colors"
          data-testid="collapse-all-categories-button"
        >
          <ArrowsInSimple size={13} className="mr-1.5" />
          Collapse Categories
        </Button>
        {plan && plan.bom_data_as_of && (
          <div
            className={`flex items-center gap-1.5 text-xs ${isStale(plan.bom_data_as_of) ? "text-[#B54708]" : "text-[#475467]"}`}
            title={new Date(plan.bom_data_as_of).toLocaleString()}
            data-testid="bom-data-freshness-badge"
          >
            {isStale(plan.bom_data_as_of) ? (
              <WarningCircle size={13} weight="bold" />
            ) : (
              <Database size={13} weight="bold" />
            )}
            <span className="font-sans">BOM data as of {formatRelativeTime(plan.bom_data_as_of)}</span>
          </div>
        )}
        {plan && plan.inventory_as_of && (
          <div
            className={`flex items-center gap-1.5 text-xs ${isStale(plan.inventory_as_of) ? "text-[#B54708]" : "text-[#475467]"}`}
            title={new Date(plan.inventory_as_of).toLocaleString()}
            data-testid="inventory-freshness-badge"
          >
            {isStale(plan.inventory_as_of) ? (
              <WarningCircle size={13} weight="bold" />
            ) : (
              <Package size={13} weight="bold" />
            )}
            <span className="font-sans">Inventory as of {formatRelativeTime(plan.inventory_as_of)}</span>
          </div>
        )}
        <div className="flex items-center gap-1.5 text-[#475467] ml-auto" data-testid="purchasing-plan-last-generated">
          <ClockCounterClockwise size={13} weight="bold" />
          <span className="font-sans text-xs">
            {lastGenerated ? `Last generated ${lastGenerated.toLocaleTimeString()}` : "Not generated yet"}
          </span>
        </div>
      </div>

      {/* Content */}
      <main className="flex-1 overflow-auto p-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
          <StatCard
            icon={Package}
            label="Leaf Components"
            value={plan ? filteredComponents.length : "—"}
            testId="stat-leaf-components"
          />
          {months.map((m) => (
            <StatCard
              key={`gross-${m}`}
              icon={CurrencyCircleDollar}
              label={`Gross Value - ${formatMonth(m)}`}
              value={plan ? formatMoney(totalValueByMonth(m), currency) : "—"}
              testId={`stat-value-${m}`}
            />
          ))}
          {months.map((m) => (
            <StatCard
              key={`net-${m}`}
              icon={CurrencyCircleDollar}
              label={`Net Purchase Value - ${formatMonth(m)}`}
              value={plan ? formatMoney(totalNetValueByMonth(m), currency) : "—"}
              testId={`stat-net-value-${m}`}
            />
          ))}
          <StatCard
            icon={WarningCircle}
            label="Missing BOMs"
            value={plan ? plan.missing_boms.length : "—"}
            testId="stat-missing-boms"
          />
        </div>

        {error && (
          <Alert
            variant="destructive"
            className="mb-4 rounded-sm border-[#F04438]/40 bg-[#FEF3F2]"
            data-testid="purchasing-plan-error-alert"
          >
            <WarningCircle size={16} />
            <AlertTitle className="font-heading text-sm">Generation failed</AlertTitle>
            <AlertDescription className="font-sans text-[13px]">{error}</AlertDescription>
          </Alert>
        )}

        {plan && plan.missing_boms.length > 0 && (
          <div
            className="mb-4 bg-[#FFFAEB] border border-[#FEDF89] rounded-sm"
            data-testid="missing-boms-warning-section"
          >
            <div className="w-full flex items-center gap-2 p-2.5">
              <button
                type="button"
                onClick={() => setWarningsOpen((v) => !v)}
                className="flex-1 flex items-center gap-2 text-left"
                data-testid="missing-boms-toggle"
              >
                {warningsOpen ? (
                  <CaretDown size={13} weight="bold" className="text-[#B54708]" />
                ) : (
                  <CaretRight size={13} weight="bold" className="text-[#B54708]" />
                )}
                <WarningCircle size={15} weight="fill" className="text-[#B54708]" />
                <span className="font-heading text-xs font-bold text-[#B54708]">
                  {plan.missing_boms.length} forecasted part{plan.missing_boms.length === 1 ? "" : "s"} could not be
                  mapped to a SAP BOM - excluded from this plan
                </span>
              </button>
              {fetchErrorPartNos.length > 0 && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={retryFailedLookups}
                  disabled={retrying}
                  className="h-7 text-xs rounded-sm border-[#B54708]/40 text-[#B54708] hover:bg-[#FEF0C7] shrink-0"
                  data-testid="retry-failed-lookups-button"
                >
                  <ArrowClockwise size={12} className={`mr-1.5 ${retrying ? "animate-spin" : ""}`} />
                  {retrying
                    ? "Retrying..."
                    : `Retry Failed Lookups (${fetchErrorPartNos.length})`}
                </Button>
              )}
            </div>
            {warningsOpen && (
              <div className="border-t border-[#FEDF89] max-h-64 overflow-auto">
                <table className="w-full text-[13px]" data-testid="missing-boms-table">
                  <thead>
                    <tr className="bg-[#FFF7E0]">
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        Status
                      </th>
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        OMS Part No
                      </th>
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        Mapped SAP ID
                      </th>
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        Reason
                      </th>
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        Fix Mapping
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.missing_boms.map((mb, i) => (
                      <tr key={mb.part_no} className="border-t border-[#FEDF89]/60" data-testid={`missing-bom-row-${i}`}>
                        <td className="px-2.5 py-1" data-testid={`missing-bom-confidence-${i}`}>
                          {mb.confidence === "fetch_error" ? (
                            <span className="inline-flex items-center gap-1 text-[#B54708]" title="SAP was unreachable - likely transient, safe to retry">
                              <ArrowClockwise size={12} weight="bold" />
                              Unresolved (retry)
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-[#7A4504]" title="SAP confirmed this part has no BOM">
                              <XCircle size={12} weight="bold" />
                              Confirmed no BOM
                            </span>
                          )}
                        </td>
                        <td className="px-2.5 py-1 text-[#7A4504] font-medium">{mb.part_no}</td>
                        <td className="px-2.5 py-1 text-[#7A4504]">{mb.sap_id || "—"}</td>
                        <td className="px-2.5 py-1 text-[#7A4504]">{mb.reason}</td>
                        <td className="px-2.5 py-1">
                          <div className="flex items-center gap-1.5">
                            <input
                              type="text"
                              placeholder="Correct SAP ID..."
                              value={overrideInputs[mb.part_no] || ""}
                              onChange={(e) => setOverrideInputs((prev) => ({ ...prev, [mb.part_no]: e.target.value }))}
                              onKeyDown={(e) => e.key === "Enter" && saveOverride(mb.part_no)}
                              disabled={savingOverride[mb.part_no]}
                              className="h-7 w-32 px-1.5 text-[12px] rounded-sm border border-[#FEDF89] text-[#101828] focus:outline-none focus:border-[#B54708] focus:ring-1 focus:ring-[#B54708]"
                              data-testid={`missing-bom-override-input-${i}`}
                            />
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => saveOverride(mb.part_no)}
                              disabled={savingOverride[mb.part_no] || !(overrideInputs[mb.part_no] || "").trim()}
                              className="h-7 text-xs rounded-sm border-[#B54708]/40 text-[#B54708] hover:bg-[#FEF0C7] shrink-0"
                              data-testid={`missing-bom-override-save-${i}`}
                            >
                              {savingOverride[mb.part_no] ? "Saving..." : "Save & Retry"}
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {loading && (
          <div data-testid="purchasing-plan-loading-skeleton">
            <p className="text-[13px] text-[#475467] mb-2" data-testid="purchasing-plan-loading-message">
              Fetching sales forecast from OMS and exploding live SAP BOMs across hundreds of components - this can
              take several minutes depending on SAP responsiveness ({elapsedSeconds}s elapsed)...
            </p>
            <div className="space-y-1.5">
              {[...Array(8)].map((_, i) => (
                <Skeleton key={i} className="h-8 w-full rounded-sm" />
              ))}
            </div>
          </div>
        )}

        {!loading && plan && (
          <div
            className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto"
            data-testid="purchasing-plan-table-container"
          >
            <table className="border-collapse w-full" data-testid="purchasing-plan-table">
              <thead>
                <tr>
                  <th
                    onClick={() => toggleSort("product_id")}
                    className="sticky left-0 z-20 bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none shadow-[2px_0_0_rgba(16,24,40,0.08)]"
                    data-testid="purchasing-plan-sort-header-product_id"
                  >
                    <span className="inline-flex items-center gap-1">
                      Product ID
                      {sortConfig.field === "product_id" &&
                        (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                    </span>
                  </th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                    Description
                  </th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                    UOM
                  </th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                    Suppliers
                  </th>
                  <th
                    onClick={() => toggleSort("on_hand")}
                    className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                    data-testid="purchasing-plan-sort-header-on-hand"
                  >
                    <span className="inline-flex items-center gap-1 justify-end">
                      On-Hand
                      {sortConfig.field === "on_hand" &&
                        (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                    </span>
                  </th>
                  <th
                    onClick={() => toggleSort("msl")}
                    className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                    data-testid="purchasing-plan-sort-header-msl"
                    title="Minimum Stock Level - set via Admin > Component Master"
                  >
                    <span className="inline-flex items-center gap-1 justify-end">
                      MSL
                      {sortConfig.field === "msl" &&
                        (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                    </span>
                  </th>
                  {months.map((m) => (
                    <th
                      key={`${m}-qty`}
                      onClick={() => toggleSort(`qty:${m}`)}
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                      data-testid={`purchasing-plan-sort-header-qty-${m}`}
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        {formatMonth(m)} Gross Qty
                        {sortConfig.field === `qty:${m}` &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                  ))}
                  {months.map((m) => (
                    <th
                      key={`${m}-netqty`}
                      onClick={() => toggleSort(`netqty:${m}`)}
                      className="bg-[#E5F0FA] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#004B87] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#D6E7F7] select-none"
                      data-testid={`purchasing-plan-sort-header-netqty-${m}`}
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        {formatMonth(m)} Net Purchase Qty
                        {sortConfig.field === `netqty:${m}` &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                  ))}
                  {months.map((m) => (
                    <th
                      key={`${m}-val`}
                      onClick={() => toggleSort(`value:${m}`)}
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                      data-testid={`purchasing-plan-sort-header-value-${m}`}
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        {formatMonth(m)} Gross Value
                        {sortConfig.field === `value:${m}` &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                  ))}
                  {months.map((m) => (
                    <th
                      key={`${m}-netval`}
                      onClick={() => toggleSort(`netvalue:${m}`)}
                      className="bg-[#E5F0FA] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#004B87] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#D6E7F7] select-none"
                      data-testid={`purchasing-plan-sort-header-netvalue-${m}`}
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        {formatMonth(m)} Net Value
                        {sortConfig.field === `netvalue:${m}` &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groupByCategory(filteredComponents).map(([category, rawItems]) => {
                  const items = sortItems(rawItems, sortConfig);
                  const isCollapsed = collapsedCategories.has(category);
                  const categoryCurrency = items.find((c) => c.currency)?.currency || currency;
                  return (
                    <Fragment key={category}>
                      <tr
                        className="bg-[#EAECF0] cursor-pointer hover:bg-[#DDE1E8] transition-colors duration-150"
                        onClick={() => toggleCategoryCollapse(category)}
                        data-testid={`category-group-header-${category}`}
                      >
                        <td colSpan={4} className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] font-bold text-[#344054]">
                          <span className="inline-flex items-center gap-1.5">
                            {isCollapsed ? (
                              <CaretRight size={12} weight="bold" />
                            ) : (
                              <CaretDown size={12} weight="bold" />
                            )}
                            {category} ({items.length})
                          </span>
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#344054]">
                          {formatQty(categoryTotalOnHand(items))}
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#344054]">
                          {formatQty(categoryTotalMsl(items))}
                        </td>
                        {months.map((m) => (
                          <td
                            key={`${category}-${m}-qty`}
                            className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#344054]"
                          >
                            {formatQty(categoryTotalQty(items, m))}
                          </td>
                        ))}
                        {months.map((m) => (
                          <td
                            key={`${category}-${m}-netqty`}
                            className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#004B87] bg-[#E5F0FA]"
                          >
                            {formatQty(categoryTotalNetQty(items, m))}
                          </td>
                        ))}
                        {months.map((m) => (
                          <td
                            key={`${category}-${m}-val`}
                            className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#344054]"
                          >
                            {formatMoney(categoryTotalValue(items, m), categoryCurrency)}
                          </td>
                        ))}
                        {months.map((m) => (
                          <td
                            key={`${category}-${m}-netval`}
                            className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#004B87] bg-[#E5F0FA]"
                          >
                            {formatMoney(categoryTotalNetValue(items, m), categoryCurrency)}
                          </td>
                        ))}
                      </tr>
                      {!isCollapsed &&
                        items.map((c, i) => {
                          const supplierSplit = computeSupplierSplit(c);
                          const isSplitExpanded = expandedSupplierSplitRows.has(c.product_id);
                          return (
                        <Fragment key={c.product_id}>
                        <tr
                          className={`${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] transition-colors duration-150`}
                          data-testid={`purchasing-plan-row-${category}-${i}`}
                        >
                            <td className={`sticky left-0 z-10 border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] font-medium shadow-[2px_0_0_rgba(16,24,40,0.08)] ${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"}`}>
                              {c.product_id}
                            </td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">{c.description || "—"}</td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">{c.unit_of_measure || "—"}</td>
                            <td
                              className="border border-[#D0D5DD] px-2 py-1 text-[13px]"
                              data-testid={`purchasing-plan-suppliers-${category}-${i}`}
                            >
                              {(supplierSummaries[c.product_id] || []).length === 0 ? (
                                <span className="text-[#98A2B3]">No supplier assigned</span>
                              ) : (
                                <div className="flex flex-wrap items-center gap-1">
                                  {supplierSummaries[c.product_id].map((a) => (
                                    <Badge
                                      key={a.id}
                                      variant="outline"
                                      className={
                                        a.preference === "Preferred"
                                          ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] text-xs whitespace-nowrap"
                                          : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs whitespace-nowrap"
                                      }
                                    >
                                      {a.supplier_name || a.supplier_id}
                                      {a.quota_percent != null ? ` ${a.quota_percent}%` : ""}
                                    </Badge>
                                  ))}
                                  {supplierSplit.length > 0 && (
                                    <button
                                      type="button"
                                      onClick={() => toggleSupplierSplit(c.product_id)}
                                      className="inline-flex items-center gap-0.5 text-[11px] font-bold text-[#004B87] hover:underline shrink-0"
                                      data-testid={`purchasing-plan-supplier-split-toggle-${category}-${i}`}
                                    >
                                      {isSplitExpanded ? <CaretDown size={10} weight="bold" /> : <CaretRight size={10} weight="bold" />}
                                      Split Qty
                                    </button>
                                  )}
                                </div>
                              )}
                            </td>
                            <td
                              className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] text-right"
                              data-testid={`purchasing-plan-on-hand-${category}-${i}`}
                            >
                              {formatQty(c.on_hand_qty)}
                            </td>
                            <td
                              className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] text-right"
                              data-testid={`purchasing-plan-msl-${category}-${i}`}
                            >
                              {formatQty(c.msl)}
                            </td>
                            {months.map((m) => (
                              <td
                                key={`${c.product_id}-${m}-qty`}
                                className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] text-right"
                                data-testid={`purchasing-plan-qty-${category}-${i}-${m}`}
                              >
                                {formatQty(c.qty_by_month[m])}
                              </td>
                            ))}
                            {months.map((m) => (
                              <td
                                key={`${c.product_id}-${m}-netqty`}
                                className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#004B87] text-right bg-[#F5FAFF]"
                                data-testid={`purchasing-plan-netqty-${category}-${i}-${m}`}
                              >
                                {formatQty(c.net_qty_by_month[m])}
                              </td>
                            ))}
                            {months.map((m) => (
                              <td
                                key={`${c.product_id}-${m}-val`}
                                className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] text-right"
                                data-testid={`purchasing-plan-value-${category}-${i}-${m}`}
                              >
                                {formatMoney(c.value_by_month[m], c.currency)}
                              </td>
                            ))}
                            {months.map((m) => (
                              <td
                                key={`${c.product_id}-${m}-netval`}
                                className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#004B87] text-right bg-[#F5FAFF]"
                                data-testid={`purchasing-plan-netvalue-${category}-${i}-${m}`}
                              >
                                {formatMoney(c.net_value_by_month[m], c.currency)}
                              </td>
                            ))}
                        </tr>
                        {isSplitExpanded && supplierSplit.length > 0 && (
                          <tr className="bg-[#F5FAFF]" data-testid={`purchasing-plan-supplier-split-row-${category}-${i}`}>
                            <td className="border border-[#D0D5DD] p-0" colSpan={6 + months.length * 4}>
                              <table className="w-full text-[12px] border-collapse">
                                <thead>
                                  <tr className="bg-[#E5F0FA]">
                                    <th className="border border-[#D0D5DD] px-2 py-1 text-left font-heading font-bold text-[#004B87] uppercase" style={{ paddingLeft: "32px" }}>
                                      Supplier (AI Quota Split)
                                    </th>
                                    <th className="border border-[#D0D5DD] px-2 py-1 text-right font-heading font-bold text-[#004B87] uppercase">
                                      Quota %
                                    </th>
                                    {months.map((m) => (
                                      <th key={`split-hdr-${m}`} className="border border-[#D0D5DD] px-2 py-1 text-right font-heading font-bold text-[#004B87] uppercase">
                                        {formatMonth(m)} Order Qty
                                      </th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {supplierSplit.map((s) => (
                                    <tr key={s.id} data-testid={`purchasing-plan-supplier-split-${category}-${i}-${s.id}`}>
                                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]" style={{ paddingLeft: "32px" }}>
                                        {s.supplier_name || s.supplier_id}
                                      </td>
                                      <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]">
                                        {s.quota_percent}%
                                      </td>
                                      {months.map((m) => (
                                        <td key={`${s.id}-${m}`} className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#004B87] font-medium">
                                          {formatQty(s.qtyByMonth[m])}
                                        </td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </td>
                          </tr>
                        )}
                        </Fragment>
                          );
                        })}
                    </Fragment>
                  );
                })}
                {filteredComponents.length === 0 && (
                  <tr>
                    <td colSpan={8 + months.length * 4} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="purchasing-plan-no-components">
                      {plan.components.length === 0
                        ? "No purchasable leaf components found in the forecast for these months"
                        : `No components in category "${categoryFilter}"`}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {!loading && !plan && !error && (
          <div
            className="border border-dashed border-[#D0D5DD] rounded-sm py-16 flex flex-col items-center gap-3 text-[#98A2B3] bg-white"
            data-testid="purchasing-plan-empty-state"
          >
            <ShoppingCartSimple size={28} weight="regular" />
            <p className="font-sans text-[13px]">
              Pick a target month above, then click "Generate Purchasing Plan" to pull that month's sales forecast
              from OMS and explode it against live SAP BOMs and standard costs
            </p>
          </div>
        )}
      </main>

      <Dialog open={salesPlanOpen} onOpenChange={setSalesPlanOpen}>
        <DialogContent className="max-w-4xl max-h-[85vh] flex flex-col" data-testid="sales-plan-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">Sales Plan Lookup (from OMS)</DialogTitle>
          </DialogHeader>
          <div className="flex items-center gap-3 flex-wrap shrink-0">
            <div className="flex items-center gap-1.5">
              <label htmlFor="sales-plan-month-picker" className="font-heading text-xs font-bold text-[#475467] uppercase">
                Month
              </label>
              <input
                id="sales-plan-month-picker"
                type="month"
                value={salesPlanMonth}
                onChange={(e) => setSalesPlanMonth(e.target.value)}
                className="h-8 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
                data-testid="sales-plan-month-picker"
              />
            </div>
            <div className="relative flex-1 min-w-[180px]">
              <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
              <input
                type="text"
                placeholder="Search part number or description..."
                value={salesPlanSearch}
                onChange={(e) => setSalesPlanSearch(e.target.value)}
                className="h-8 w-full pl-7 pr-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
                data-testid="sales-plan-search-input"
              />
            </div>
            <span className="text-xs text-[#475467] font-sans" data-testid="sales-plan-item-count">
              {filteredSalesPlanItems.length} of {salesPlanItems.length} items
            </span>
          </div>

          <div className="flex-1 overflow-auto border border-[#D0D5DD] rounded-sm" data-testid="sales-plan-table-container">
            {salesPlanLoading ? (
              <div className="flex items-center justify-center py-16 text-[#475467] text-sm">
                <ArrowClockwise size={16} className="animate-spin mr-2" />
                Loading sales plan for {formatMonth(salesPlanMonth)}...
              </div>
            ) : salesPlanError ? (
              <div className="flex items-center justify-center py-16 text-[#B54708] text-sm px-4 text-center" data-testid="sales-plan-error">
                {salesPlanError}
              </div>
            ) : (
              <table className="w-full text-[13px] border-collapse">
                <thead className="sticky top-0">
                  <tr>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 w-6"></th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">
                      Part No
                    </th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">
                      Description
                    </th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">
                      Total Planned Qty
                    </th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">
                      Sale Price
                    </th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">
                      Sale Value (INR)
                    </th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase" title="Customer-requested selling lead time, from OMS - use to back-calculate when procurement needs to start">
                      Lead Time (Days)
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filteredSalesPlanItems.map((it, i) => {
                    const isExpanded = expandedSalesPlanRows.has(it.part_no);
                    return (
                      <Fragment key={it.part_no}>
                        <tr
                          className={`${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] cursor-pointer transition-colors duration-150`}
                          onClick={() => toggleSalesPlanRow(it.part_no)}
                          data-testid={`sales-plan-row-${it.part_no}`}
                        >
                          <td className="border border-[#D0D5DD] px-1.5 py-1 text-center text-[#667085]">
                            {it.customers.length > 0 &&
                              (isExpanded ? <CaretDown size={11} weight="bold" /> : <CaretRight size={11} weight="bold" />)}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{it.part_no}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]">{it.description || "—"}</td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums font-bold text-[#101828]" data-testid={`sales-plan-total-qty-${it.part_no}`}>
                            {formatQty(it.total_qty)}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]" data-testid={`sales-plan-price-${it.part_no}`}>
                            {formatMoney(it.price, it.currency)}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums font-bold text-[#101828]" data-testid={`sales-plan-value-${it.part_no}`}>
                            {formatMoney(it.total_sale_value_inr, "INR")}
                          </td>
                          <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467]" data-testid={`sales-plan-lead-day-${it.part_no}`}>
                            {it.lead_day ?? "—"}
                          </td>
                        </tr>
                        {isExpanded &&
                          it.customers.map((cust) => (
                            <tr key={`${it.part_no}-${cust.customer_name}`} className="bg-[#F5FAFF]" data-testid={`sales-plan-customer-row-${it.part_no}`}>
                              <td className="border border-[#D0D5DD]"></td>
                              <td className="border border-[#D0D5DD] px-2 py-1 pl-6 text-[#475467] text-xs" colSpan={2}>
                                {cust.customer_name}
                              </td>
                              <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467] text-xs">
                                {formatQty(cust.qty)}
                              </td>
                              <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467] text-xs">
                                {formatMoney(cust.price, it.currency)}
                              </td>
                              <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467] text-xs">
                                {formatMoney(cust.sale_value_inr, "INR")}
                              </td>
                              <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#475467] text-xs">
                                {cust.lead_day ?? "—"}
                              </td>
                            </tr>
                          ))}
                      </Fragment>
                    );
                  })}
                  {!salesPlanLoading && filteredSalesPlanItems.length === 0 && (
                    <tr>
                      <td colSpan={7} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="sales-plan-no-items">
                        No sales plan items found for {formatMonth(salesPlanMonth)}
                      </td>
                    </tr>
                  )}
                </tbody>
                {!salesPlanLoading && filteredSalesPlanItems.length > 0 && (
                  <tfoot>
                    <tr className="bg-[#EAECF0] sticky bottom-0" data-testid="sales-plan-totals-row">
                      <td className="border border-[#D0D5DD]" colSpan={3}></td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-heading font-bold text-[#101828]" data-testid="sales-plan-total-qty-grand">
                        {formatQty(filteredSalesPlanItems.reduce((sum, it) => sum + (it.total_qty || 0), 0))}
                      </td>
                      <td className="border border-[#D0D5DD]"></td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums font-heading font-bold text-[#101828]" data-testid="sales-plan-total-value-grand">
                        {formatMoney(filteredSalesPlanItems.reduce((sum, it) => sum + (it.total_sale_value_inr || 0), 0), "INR")}
                      </td>
                      <td className="border border-[#D0D5DD]"></td>
                    </tr>
                  </tfoot>
                )}
              </table>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
