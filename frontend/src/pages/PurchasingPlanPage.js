import { useState, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import {
  Package,
  ClockCounterClockwise,
  WarningCircle,
  CurrencyCircleDollar,
  Database,
  ShoppingCartSimple,
  CaretDown,
  CaretRight,
  CaretUp,
  FileArrowDown,
  ArrowsOutSimple,
  ArrowsInSimple,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { NavTabs } from "@/components/NavTabs";

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

const formatQty = (value) => (value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: 2 }));

const formatMoney = (value, currency) =>
  value == null ? "—" : `${currency || ""} ${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

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
  if (field === "total") return component.value_by_month ? Object.values(component.value_by_month).reduce((s, v) => s + (v || 0), 0) : -Infinity;
  if (field.startsWith("qty:")) return component.qty_by_month[field.slice(4)] ?? -Infinity;
  if (field.startsWith("value:")) return component.value_by_month[field.slice(6)] ?? -Infinity;
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

  const months = plan?.months || [];
  const currency = plan?.components?.find((c) => c.currency)?.currency || "";

  const availableCategories = plan
    ? Array.from(new Set(plan.components.map((c) => c.category || "Uncategorized"))).sort()
    : [];
  const filteredComponents =
    plan && categoryFilter !== "all"
      ? plan.components.filter((c) => (c.category || "Uncategorized") === categoryFilter)
      : plan?.components || [];

  const totalValueByMonth = (month) =>
    filteredComponents.reduce((sum, c) => sum + (c.value_by_month[month] || 0), 0);

  const totalValueOverall = (component) =>
    months.reduce((sum, m) => sum + (component.value_by_month[m] || 0), 0);

  const categoryTotalQty = (items, month) => items.reduce((sum, c) => sum + (c.qty_by_month[month] || 0), 0);
  const categoryTotalValue = (items, month) => items.reduce((sum, c) => sum + (c.value_by_month[month] || 0), 0);
  const categoryGrandTotal = (items) => items.reduce((sum, c) => sum + totalValueOverall(c), 0);

  const exportToExcel = () => {
    if (!plan) return;
    const rows = filteredComponents.map((c) => {
      const row = {
        "Product ID": c.product_id,
        Description: c.description || "",
        Category: c.category || "Uncategorized",
        UOM: c.unit_of_measure || "",
      };
      months.forEach((m) => {
        row[`${formatMonth(m)} Qty`] = c.qty_by_month[m] ?? "";
        row[`${formatMonth(m)} Value`] = c.value_by_month[m] ?? "";
      });
      row["Total Value"] = totalValueOverall(c);
      return row;
    });
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(rows), "Purchasing Plan");

    if (plan.missing_boms.length > 0) {
      const missingRows = plan.missing_boms.map((mb) => ({
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
      <header className="h-12 bg-[#004B87] shadow-[0_1px_3px_0_rgba(16,24,40,0.1)] flex items-center justify-between px-4 shrink-0 z-10">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2.5" data-testid="app-title">
            <Database size={18} weight="bold" className="text-white" />
            <span className="font-heading text-sm font-bold text-white tracking-tight">SAP BOM Explorer</span>
            <span className="font-sans text-xs text-white/60 hidden sm:inline">| Purchasing Plan</span>
          </div>
          <NavTabs />
        </div>
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
              key={m}
              icon={CurrencyCircleDollar}
              label={`Value - ${formatMonth(m)}`}
              value={plan ? formatMoney(totalValueByMonth(m), currency) : "—"}
              testId={`stat-value-${m}`}
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
            <button
              type="button"
              onClick={() => setWarningsOpen((v) => !v)}
              className="w-full flex items-center gap-2 p-2.5 text-left"
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
            {warningsOpen && (
              <div className="border-t border-[#FEDF89] max-h-64 overflow-auto">
                <table className="w-full text-[13px]" data-testid="missing-boms-table">
                  <thead>
                    <tr className="bg-[#FFF7E0]">
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        OMS Part No
                      </th>
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        Mapped SAP ID
                      </th>
                      <th className="text-left px-2.5 py-1 font-heading text-xs font-bold text-[#B54708] uppercase">
                        Reason
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.missing_boms.map((mb, i) => (
                      <tr key={mb.part_no} className="border-t border-[#FEDF89]/60" data-testid={`missing-bom-row-${i}`}>
                        <td className="px-2.5 py-1 text-[#7A4504] font-medium">{mb.part_no}</td>
                        <td className="px-2.5 py-1 text-[#7A4504]">{mb.sap_id || "—"}</td>
                        <td className="px-2.5 py-1 text-[#7A4504]">{mb.reason}</td>
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
                    className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
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
                  {months.map((m) => (
                    <th
                      key={`${m}-qty`}
                      onClick={() => toggleSort(`qty:${m}`)}
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                      data-testid={`purchasing-plan-sort-header-qty-${m}`}
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        {formatMonth(m)} Qty
                        {sortConfig.field === `qty:${m}` &&
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
                        {formatMonth(m)} Value
                        {sortConfig.field === `value:${m}` &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                  ))}
                  <th
                    onClick={() => toggleSort("total")}
                    className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                    data-testid="purchasing-plan-sort-header-total"
                  >
                    <span className="inline-flex items-center gap-1 justify-end">
                      Total Value
                      {sortConfig.field === "total" &&
                        (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                    </span>
                  </th>
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
                        <td colSpan={3} className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] font-bold text-[#344054]">
                          <span className="inline-flex items-center gap-1.5">
                            {isCollapsed ? (
                              <CaretRight size={12} weight="bold" />
                            ) : (
                              <CaretDown size={12} weight="bold" />
                            )}
                            {category} ({items.length})
                          </span>
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
                            key={`${category}-${m}-val`}
                            className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#344054]"
                          >
                            {formatMoney(categoryTotalValue(items, m), categoryCurrency)}
                          </td>
                        ))}
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-[13px] tabular-nums text-right font-bold text-[#344054]">
                          {formatMoney(categoryGrandTotal(items), categoryCurrency)}
                        </td>
                      </tr>
                      {!isCollapsed &&
                        items.map((c, i) => (
                          <tr
                            key={c.product_id}
                            className={`${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] transition-colors duration-150`}
                            data-testid={`purchasing-plan-row-${category}-${i}`}
                          >
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] font-medium">
                              {c.product_id}
                            </td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">{c.description || "—"}</td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">{c.unit_of_measure || "—"}</td>
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
                                key={`${c.product_id}-${m}-val`}
                                className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] text-right"
                                data-testid={`purchasing-plan-value-${category}-${i}-${m}`}
                              >
                                {formatMoney(c.value_by_month[m], c.currency)}
                              </td>
                            ))}
                            <td
                              className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] text-right font-bold"
                              data-testid={`purchasing-plan-total-${category}-${i}`}
                            >
                              {formatMoney(totalValueOverall(c), c.currency)}
                            </td>
                          </tr>
                        ))}
                    </Fragment>
                  );
                })}
                {filteredComponents.length === 0 && (
                  <tr>
                    <td colSpan={4 + months.length * 2} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="purchasing-plan-no-components">
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
    </div>
  );
}
