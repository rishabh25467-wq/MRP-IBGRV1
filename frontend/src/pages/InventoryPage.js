import { useState, useEffect, useMemo, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import { Link } from "react-router-dom";
import {
  Package,
  MagnifyingGlass,
  ArrowClockwise,
  CaretDown,
  CaretUp,
  CaretRight,
  CurrencyCircleDollar,
  MapPin,
  Database,
  Shield,
  Sparkle,
  Tag,
  WarningCircle,
  ArrowSquareOut,
  ClockCounterClockwise,
  DownloadSimple,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const PAGE_SIZE = 50;

const formatQty = (value) => (value == null ? "—" : value.toLocaleString("en-IN", { maximumFractionDigits: 2 }));

const formatMoney = (value, currency) =>
  value == null ? "—" : `${currency || ""} ${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const formatIST = (iso) => {
  if (!iso) return null;
  return (
    new Date(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST"
  );
};

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

export default function InventoryPage() {
  const [status, setStatus] = useState("idle"); // idle | cache-loading | running | done | failed
  const [items, setItems] = useState([]);
  const [categories, setCategories] = useState([]);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [siteFilter, setSiteFilter] = useState("all");
  const [entityFilter, setEntityFilter] = useState("all");
  const [noBomOnly, setNoBomOnly] = useState(false);
  const [expandedRows, setExpandedRows] = useState(new Set());
  const [page, setPage] = useState(1);
  const [backfillOpen, setBackfillOpen] = useState(false);
  const [backfillStatus, setBackfillStatus] = useState("idle"); // idle | running | done | failed
  const [backfillProgress, setBackfillProgress] = useState({ processed: 0, total: 0 });
  const [backfillResult, setBackfillResult] = useState(null);
  const [backfillError, setBackfillError] = useState(null);
  const [categorizeStatus, setCategorizeStatus] = useState("idle"); // idle | running | done | failed

  const refreshFromSap = async () => {
    setStatus("running");
    setError(null);
    try {
      const { data } = await axios.post(`${API}/inventory`);
      const jobId = data.job_id;
      let consecutiveFailures = 0;
      const poll = async () => {
        try {
          const { data: job } = await axios.get(`${API}/inventory/${jobId}`);
          consecutiveFailures = 0;
          if (job.status === "running") {
            setTimeout(poll, 2000);
          } else if (job.status === "done") {
            setItems(job.result.items);
            setCategories(job.result.categories);
            setUpdatedAt(job.result.updated_at);
            setStatus("done");
          } else {
            setStatus("failed");
            setError(job.error);
          }
        } catch (err) {
          // A transient network blip (or the job briefly not found yet)
          // must never silently freeze the UI on "running" forever - retry
          // a few times before actually giving up and surfacing an error.
          consecutiveFailures += 1;
          if (consecutiveFailures <= 5) {
            setTimeout(poll, 2000);
          } else {
            setStatus("failed");
            setError(err?.response?.data?.detail || err.message);
          }
        }
      };
      poll();
    } catch (err) {
      setStatus("failed");
      setError(err?.response?.data?.detail || err.message);
    }
  };

  const loadFromCache = async () => {
    setStatus("cache-loading");
    setError(null);
    try {
      const { data } = await axios.get(`${API}/inventory`);
      if (data.items.length > 0) {
        setItems(data.items);
        setCategories(data.categories);
        setUpdatedAt(data.updated_at);
        setStatus("done");
      } else {
        // No cache yet (fresh deploy, background scheduler hasn't run its
        // first cycle) - fall back to a live pull so the page isn't empty.
        refreshFromSap();
      }
    } catch (err) {
      setStatus("failed");
      setError(err?.response?.data?.detail || err.message);
    }
  };

  useEffect(() => {
    loadFromCache();
  }, []);

  const [backfillPhase, setBackfillPhase] = useState("resolving"); // resolving | refreshing_cache

  const startDeepBackfill = async () => {
    setBackfillOpen(true);
    setBackfillStatus("running");
    setBackfillPhase("resolving");
    setBackfillProgress({ processed: 0, total: 0 });
    setBackfillResult(null);
    setBackfillError(null);
    try {
      const { data } = await axios.post(`${API}/inventory/deep-backfill-uuids`);
      const jobId = data.job_id;
      let consecutiveFailures = 0;
      const poll = async () => {
        try {
          const { data: job } = await axios.get(`${API}/inventory/deep-backfill-uuids/${jobId}`);
          consecutiveFailures = 0;
          if (job.progress) setBackfillProgress(job.progress);
          if (job.phase) setBackfillPhase(job.phase);
          if (job.status === "running") {
            setTimeout(poll, 2000);
          } else if (job.status === "done") {
            setBackfillStatus("done");
            setBackfillResult(job.result);
            loadFromCache();
          } else {
            setBackfillStatus("failed");
            setBackfillError(job.error);
          }
        } catch (err) {
          // Don't let one flaky poll (network blip, backend hiccup) leave
          // the dialog spinning on "Checking..." forever with no way out -
          // retry a few times, then surface a real error.
          consecutiveFailures += 1;
          if (consecutiveFailures <= 5) {
            setTimeout(poll, 2000);
          } else {
            setBackfillStatus("failed");
            setBackfillError(err?.response?.data?.detail || err.message);
          }
        }
      };
      poll();
    } catch (err) {
      setBackfillStatus("failed");
      setBackfillError(err?.response?.data?.detail || err.message);
    }
  };

  const categorizeAllInventory = async () => {
    setCategorizeStatus("running");
    try {
      const { data } = await axios.post(`${API}/inventory/categorize-all`);
      const jobId = data.job_id;
      let consecutiveFailures = 0;
      const poll = async () => {
        try {
          const { data: job } = await axios.get(`${API}/inventory/categorize-all/${jobId}`);
          consecutiveFailures = 0;
          if (job.status === "running") {
            setTimeout(poll, 2000);
          } else if (job.status === "done") {
            setCategorizeStatus("done");
            toast.success("Categorization complete", {
              description: `${job.result.finished_goods} top-level item(s) set to Finished Goods, ${job.result.ai_categorized} classified by AI, out of ${job.result.total_items} total.`,
            });
            loadFromCache();
          } else {
            setCategorizeStatus("failed");
            toast.error("Categorization failed", { description: job.error });
          }
        } catch (err) {
          consecutiveFailures += 1;
          if (consecutiveFailures <= 5) {
            setTimeout(poll, 2000);
          } else {
            setCategorizeStatus("failed");
            toast.error("Categorization failed", { description: err?.response?.data?.detail || err.message });
          }
        }
      };
      poll();
    } catch (err) {
      setCategorizeStatus("failed");
      toast.error("Categorization failed", { description: err?.response?.data?.detail || err.message });
    }
  };

  const sites = useMemo(() => {
    const set = new Set();
    items.forEach((it) => it.locations.forEach((loc) => loc.site && set.add(loc.site)));
    return [...set].sort();
  }, [items]);

  const entities = useMemo(() => {
    const map = new Map();
    items.forEach((it) =>
      it.locations.forEach((loc) => {
        if (loc.company_code) map.set(loc.company_code, loc.company_name || loc.company_code);
      })
    );
    return [...map.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [items]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((it) => {
      const matchesSearch =
        !q || it.product_id.toLowerCase().includes(q) || (it.description || "").toLowerCase().includes(q);
      const matchesCategory =
        categoryFilter === "all" || (categoryFilter === "uncategorized" ? !it.category : it.category === categoryFilter);
      const matchesSite = siteFilter === "all" || it.locations.some((loc) => loc.site === siteFilter);
      const matchesEntity = entityFilter === "all" || it.locations.some((loc) => loc.company_code === entityFilter);
      const matchesNoBom = !noBomOnly || it.no_bom;
      return matchesSearch && matchesCategory && matchesSite && matchesEntity && matchesNoBom;
    });
  }, [items, search, categoryFilter, siteFilter, entityFilter, noBomOnly]);

  const [sortConfig, setSortConfig] = useState({ field: "product_id", direction: "asc" });
  const numericSortFields = ["total_qty", "unit_cost", "total_value"];

  const handleSort = (field) => {
    setSortConfig((prev) =>
      prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }
    );
  };

  const sorted = useMemo(() => {
    const { field, direction } = sortConfig;
    if (!field) return filtered;
    const dir = direction === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      if (numericSortFields.includes(field)) {
        const av = a[field] ?? -Infinity;
        const bv = b[field] ?? -Infinity;
        return (av - bv) * dir;
      }
      const av = (a[field] || "").toString().toLowerCase();
      const bv = (b[field] || "").toString().toLowerCase();
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [filtered, sortConfig]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const pagedItems = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const exportExcel = () => {
    const activeFilters = [
      search.trim() && `Search: "${search.trim()}"`,
      categoryFilter !== "all" && `Category: ${categoryFilter === "uncategorized" ? "Uncategorized" : categoryFilter}`,
      siteFilter !== "all" && `Site: ${siteFilter}`,
      entityFilter !== "all" && `Entity: ${entities.find(([code]) => code === entityFilter)?.[1] || entityFilter}`,
      noBomOnly && "Show only items without BOM",
    ]
      .filter(Boolean)
      .join(" | ") || "None";
    const headerRows = [
      [`Last Updated: ${updatedAt ? formatIST(updatedAt) : ""}`],
      [`Filters Applied: ${activeFilters}`],
      [],
      ["Product ID", "Description", "Category", "On-Hand Qty", "UOM", "Unit Cost", "Currency", "Total Value", "No BOM", "Historical BOM", "Locations"],
    ];
    const dataRows = sorted.map((it) => [
      it.product_id,
      it.description || "",
      it.category || "",
      it.total_qty ?? "",
      it.uom || "",
      it.unit_cost ?? "",
      it.currency || "",
      it.total_value ?? "",
      it.no_bom ? "Yes" : "No",
      it.historical_bom ? "Yes" : "No",
      (it.locations || []).map((loc) => `${loc.site}: ${loc.qty}`).join("; "),
    ]);
    const sheet = XLSX.utils.aoa_to_sheet([...headerRows, ...dataRows]);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, sheet, "Inventory");
    XLSX.writeFile(workbook, `inventory_export_${new Date().toISOString().slice(0, 10)}.xlsx`);
  };

  useEffect(() => {
    setPage(1);
  }, [search, categoryFilter, siteFilter, entityFilter, noBomOnly]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [totalPages, page]);

  const toggleRow = (productId) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      next.has(productId) ? next.delete(productId) : next.add(productId);
      return next;
    });
  };

  const totalQty = filtered.reduce((sum, it) => sum + (it.total_qty || 0), 0);
  const totalValue = filtered.reduce((sum, it) => sum + (it.total_value || 0), 0);
  const valuedCurrency = filtered.find((it) => it.currency)?.currency;

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]" data-testid="inventory-page">
      <Toaster position="top-right" richColors />

      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Inventory Management</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto max-w-[1600px] w-full mx-auto px-6 py-6 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h1 className="font-heading text-xl font-bold text-[#1D2939]">Inventory</h1>
            {updatedAt && (
              <span
                className="text-xs text-[#475467] font-sans bg-white border border-[#D0D5DD] rounded-full px-2.5 py-1"
                data-testid="inventory-last-updated"
              >
                Last Updated: {formatIST(updatedAt)}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={categorizeAllInventory}
              disabled={categorizeStatus === "running"}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="inventory-categorize-all-button"
              title="AI-classify every item on this page into a material/type category, including top-level assemblies (auto-set to Finished Goods)"
            >
              <Tag size={13} className={`mr-1.5 ${categorizeStatus === "running" ? "animate-pulse" : ""}`} />
              {categorizeStatus === "running" ? "Categorizing..." : "Categorize All"}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={startDeepBackfill}
              disabled={backfillStatus === "running"}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="inventory-deep-backfill-button"
              title="One-time, throttled live SAP lookup to link any remaining items to their Standard Cost, so more Unit Cost/Total Value cells populate"
            >
              <Sparkle size={13} className={`mr-1.5 ${backfillStatus === "running" ? "animate-pulse" : ""}`} />
              {backfillStatus === "running" ? "Resolving Links..." : "Resolve Missing Values"}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={exportExcel}
              disabled={sorted.length === 0}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="inventory-export-excel-button"
              title="Export the currently filtered/searched rows shown below to an Excel file"
            >
              <DownloadSimple size={13} className="mr-1.5" />
              Export Excel
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={refreshFromSap}
              disabled={status === "running"}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="inventory-refresh-button"
            >
              <ArrowClockwise size={13} className={`mr-1.5 ${status === "running" ? "animate-spin" : ""}`} />
              {status === "running" ? "Loading from SAP..." : "Refresh"}
            </Button>
          </div>
        </div>

        {status === "cache-loading" && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm p-8 text-center text-sm text-[#475467]" data-testid="inventory-cache-loading">
            <ArrowClockwise size={20} className="animate-spin inline-block mb-2" />
            <p>Loading cached inventory...</p>
          </div>
        )}

        {status === "running" && items.length === 0 && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm p-8 text-center text-sm text-[#475467]" data-testid="inventory-loading">
            <ArrowClockwise size={20} className="animate-spin inline-block mb-2" />
            <p>Pulling live On-Hand Inventory and Standard Costs from SAP - this can take a minute or two.</p>
          </div>
        )}

        {status === "failed" && (
          <div className="bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-4 text-sm text-[#912018]" data-testid="inventory-error">
            Could not load inventory: {error}
          </div>
        )}

        {items.length > 0 && (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <StatCard icon={Package} label="Items in Stock" value={filtered.length.toLocaleString("en-IN")} testId="inventory-stat-items" />
              <StatCard icon={Package} label="Total On-Hand Qty" value={formatQty(totalQty)} testId="inventory-stat-qty" />
              <StatCard
                icon={CurrencyCircleDollar}
                label="Total Inventory Value"
                value={formatMoney(totalValue, valuedCurrency)}
                testId="inventory-stat-value"
              />
            </div>

            <div className="bg-white border border-[#D0D5DD] rounded-sm p-3 flex items-center gap-2 flex-wrap">
              <div className="relative flex-1 min-w-[220px] max-w-sm">
                <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                <input
                  type="text"
                  placeholder="Search product ID or description..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="h-8 w-full pl-8 pr-3 text-sm border border-[#D0D5DD] rounded-sm focus:outline-none focus:ring-1 focus:ring-[#004B87]"
                  data-testid="inventory-search-input"
                />
              </div>
              <Select value={categoryFilter} onValueChange={setCategoryFilter}>
                <SelectTrigger className="h-8 w-44 text-xs rounded-sm border-[#D0D5DD]" data-testid="inventory-category-filter">
                  <SelectValue placeholder="Category" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Categories</SelectItem>
                  <SelectItem value="uncategorized">Uncategorized</SelectItem>
                  {categories.map((cat) => (
                    <SelectItem key={cat} value={cat}>
                      {cat}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={siteFilter} onValueChange={setSiteFilter}>
                <SelectTrigger className="h-8 w-44 text-xs rounded-sm border-[#D0D5DD]" data-testid="inventory-site-filter">
                  <SelectValue placeholder="Site" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Sites</SelectItem>
                  {sites.map((site) => (
                    <SelectItem key={site} value={site}>
                      {site}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={entityFilter} onValueChange={setEntityFilter}>
                <SelectTrigger className="h-8 w-44 text-xs rounded-sm border-[#D0D5DD]" data-testid="inventory-entity-filter">
                  <SelectValue placeholder="Entity" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Entities</SelectItem>
                  {entities.map(([code, name]) => (
                    <SelectItem key={code} value={code} data-testid={`inventory-entity-option-${code}`}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <label
                className="flex items-center gap-1.5 h-8 px-2.5 text-xs font-medium text-[#B54708] border border-[#D0D5DD] rounded-sm cursor-pointer select-none hover:bg-[#FFFAEB] shrink-0"
                data-testid="inventory-no-bom-only-filter"
              >
                <input
                  type="checkbox"
                  checked={noBomOnly}
                  onChange={(e) => setNoBomOnly(e.target.checked)}
                  className="accent-[#B54708]"
                />
                Show only items without BOM
              </label>
              <span className="text-xs text-[#475467] ml-auto font-sans" data-testid="inventory-item-count">
                {filtered.length} of {items.length} items
              </span>
            </div>

            <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto max-h-[65vh]">
              <table className="w-full text-[13px] border-collapse block md:table">
                <thead className="hidden md:table-header-group md:sticky md:top-0 md:z-[1]">
                  <tr className="md:table-row">
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 w-8"></th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("product_id")}
                      data-testid="inventory-sort-product-id"
                    >
                      <span className="inline-flex items-center gap-1">
                        Product ID
                        {sortConfig.field === "product_id" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("description")}
                      data-testid="inventory-sort-description"
                    >
                      <span className="inline-flex items-center gap-1">
                        Description
                        {sortConfig.field === "description" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("category")}
                      data-testid="inventory-sort-category"
                    >
                      <span className="inline-flex items-center gap-1">
                        Category
                        {sortConfig.field === "category" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("total_qty")}
                      data-testid="inventory-sort-qty"
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        On-Hand Qty
                        {sortConfig.field === "total_qty" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("uom")}
                      data-testid="inventory-sort-uom"
                    >
                      <span className="inline-flex items-center gap-1">
                        UOM
                        {sortConfig.field === "uom" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("unit_cost")}
                      data-testid="inventory-sort-unit-cost"
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        Unit Cost
                        {sortConfig.field === "unit_cost" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                    <th
                      className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase cursor-pointer select-none hover:bg-[#E4E7EC]"
                      onClick={() => handleSort("total_value")}
                      data-testid="inventory-sort-total-value"
                    >
                      <span className="inline-flex items-center gap-1 justify-end">
                        Total Value
                        {sortConfig.field === "total_value" &&
                          (sortConfig.direction === "asc" ? <CaretUp size={10} weight="bold" /> : <CaretDown size={10} weight="bold" />)}
                      </span>
                    </th>
                  </tr>
                </thead>
                <tbody className="block md:table-row-group">
                  {pagedItems.map((it, i) => {
                    const isExpanded = expandedRows.has(it.product_id);
                    return (
                      <Fragment key={it.product_id}>
                        <tr
                          className={`flex flex-col md:table-row mb-3 md:mb-0 last:mb-0 rounded-lg md:rounded-none border md:border-0 border-[#D0D5DD] overflow-hidden shadow-sm md:shadow-none ${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] cursor-pointer transition-colors duration-150`}
                          onClick={() => toggleRow(it.product_id)}
                          data-testid={`inventory-row-${it.product_id}`}
                        >
                          <td className="hidden md:table-cell border border-[#D0D5DD] px-1.5 py-1 text-center text-[#667085]">
                            {it.locations.length > 0 &&
                              (isExpanded ? <CaretDown size={11} weight="bold" /> : <CaretRight size={11} weight="bold" />)}
                          </td>
                          <td
                            data-label="Product ID"
                            className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 font-medium text-[#101828] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                          >
                            <span className="inline-flex items-center gap-1.5 md:contents">
                              <span className="md:hidden text-[#667085]">
                                {it.locations.length > 0 &&
                                  (isExpanded ? <CaretDown size={11} weight="bold" /> : <CaretRight size={11} weight="bold" />)}
                              </span>
                              {it.product_id}
                              {it.no_bom && (
                                <span
                                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-[#FFFAEB] border border-[#FEDF89] text-[#B54708] text-[10px] font-bold uppercase tracking-wide"
                                  data-testid={`inventory-no-bom-tag-${it.product_id}`}
                                  title="SAP confirms no BOM exists for this item, and it is not used as a component in any other CURRENT ACTIVE BOM"
                                >
                                  <WarningCircle size={11} weight="bold" />
                                  No BOM
                                </span>
                              )}
                              {it.no_bom && it.historical_bom && (
                                <span
                                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-[#F0F4F8] border border-[#D0D5DD] text-[#475467] text-[10px] font-bold uppercase tracking-wide"
                                  data-testid={`inventory-historical-bom-tag-${it.product_id}`}
                                  title="This item was found in an OLDER/superseded BOM revision, but is not part of any current active BOM"
                                >
                                  <ClockCounterClockwise size={11} weight="bold" />
                                  Historical BOM
                                </span>
                              )}
                              {it.no_bom && (
                                <Link
                                  to={`/?search=${encodeURIComponent(it.product_id)}`}
                                  onClick={(e) => e.stopPropagation()}
                                  className="inline-flex items-center gap-0.5 text-[#004B87] hover:text-[#00365f] hover:underline text-[10px] font-semibold shrink-0"
                                  data-testid={`inventory-no-bom-investigate-${it.product_id}`}
                                  title="Investigate in BOM Explorer"
                                >
                                  <ArrowSquareOut size={11} weight="bold" />
                                  Investigate
                                </Link>
                              )}
                            </span>
                          </td>
                          <td
                            data-label="Description"
                            className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 text-[#101828] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                          >
                            {it.description || "—"}
                          </td>
                          <td
                            data-label="Category"
                            className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 text-[#475467] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                          >
                            {it.category || "Uncategorized"}
                          </td>
                          <td
                            data-label="On-Hand Qty"
                            className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 md:text-right tabular-nums font-bold text-[#101828] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                            data-testid={`inventory-qty-${it.product_id}`}
                          >
                            {formatQty(it.total_qty)}
                          </td>
                          <td
                            data-label="UOM"
                            className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 text-[#475467] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                          >
                            {it.uom || "—"}
                          </td>
                          <td
                            data-label="Unit Cost"
                            className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 md:text-right tabular-nums text-[#475467] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                            data-testid={`inventory-unit-cost-${it.product_id}`}
                          >
                            {formatMoney(it.unit_cost, it.currency)}
                          </td>
                          <td
                            data-label="Total Value"
                            className="flex md:table-cell justify-between items-center gap-3 border-0 md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 md:text-right tabular-nums font-bold text-[#101828] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                            data-testid={`inventory-value-${it.product_id}`}
                          >
                            {formatMoney(it.total_value, it.currency)}
                          </td>
                        </tr>
                        {isExpanded &&
                          it.locations.map((loc, li) => (
                            <tr
                              key={`${it.product_id}-${li}`}
                              className="flex flex-wrap md:table-row bg-[#F5FAFF] mb-2 md:mb-0 rounded-md md:rounded-none px-3 md:px-0 py-1.5 md:py-0 gap-x-3"
                              data-testid={`inventory-location-row-${it.product_id}`}
                            >
                              <td className="hidden md:table-cell border border-[#D0D5DD]"></td>
                              <td className="border-0 md:border md:border-[#D0D5DD] px-0 md:px-2 py-0.5 md:py-1 pl-0 md:pl-6 text-[#475467] text-xs" colSpan={2}>
                                <MapPin size={10} className="inline mr-1 text-[#98A2B3]" />
                                {loc.site || "—"} / {loc.logistics_area || "—"}
                              </td>
                              <td className="border-0 md:border md:border-[#D0D5DD] px-0 md:px-2 py-0.5 md:py-1 text-[#475467] text-xs">{loc.stock_status || "—"}</td>
                              <td className="border-0 md:border md:border-[#D0D5DD] px-0 md:px-2 py-0.5 md:py-1 text-[#475467] text-xs" data-testid={`inventory-location-entity-${it.product_id}`}>
                                {loc.company_name || loc.company_code || "—"}
                              </td>
                              <td className="border-0 md:border md:border-[#D0D5DD] px-0 md:px-2 py-0.5 md:py-1 text-right tabular-nums text-[#475467] text-xs">
                                {formatQty(loc.qty)}
                              </td>
                              <td className="hidden md:table-cell border border-[#D0D5DD]" colSpan={2}></td>
                            </tr>
                          ))}
                      </Fragment>
                    );
                  })}
                  {pagedItems.length === 0 && (
                    <tr className="block md:table-row">
                      <td colSpan={8} className="block md:table-cell border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="inventory-no-items">
                        No inventory items match your filters
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {filtered.length > 0 && (
              <div className="flex items-center justify-between px-1" data-testid="inventory-pagination">
                <span className="text-xs text-[#475467] font-sans">
                  Showing {(page - 1) * PAGE_SIZE + 1}-{Math.min(page * PAGE_SIZE, filtered.length)} of {filtered.length}
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page <= 1}
                    className="h-7 text-xs rounded-sm border-[#D0D5DD] text-[#344054] px-2"
                    data-testid="inventory-pagination-prev"
                  >
                    Previous
                  </Button>
                  <span className="text-xs text-[#475467] font-sans tabular-nums">
                    Page {page} of {totalPages}
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                    disabled={page >= totalPages}
                    className="h-7 text-xs rounded-sm border-[#D0D5DD] text-[#344054] px-2"
                    data-testid="inventory-pagination-next"
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </main>

      <Dialog open={backfillOpen} onOpenChange={(open) => !open && backfillStatus !== "running" && setBackfillOpen(false)}>
        <DialogContent className="max-w-md" data-testid="inventory-deep-backfill-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">Resolve Missing Values</DialogTitle>
          </DialogHeader>
          {backfillStatus === "running" && (
            <div className="py-4 space-y-3" data-testid="inventory-deep-backfill-running">
              <div className="flex items-center gap-2 text-sm text-[#475467]" data-testid="inventory-deep-backfill-phase-text">
                <ArrowClockwise size={14} className="animate-spin" />
                {backfillPhase === "refreshing_cache"
                  ? "Links resolved - refreshing inventory valuations..."
                  : `Checking ${backfillProgress.processed} of ${backfillProgress.total || "?"} item(s) against SAP...`}
              </div>
              {backfillPhase === "resolving" && backfillProgress.total > 0 && (
                <div className="w-full h-2 bg-[#EAECF0] rounded-full overflow-hidden">
                  <div
                    className="h-full bg-[#004B87] transition-all duration-300"
                    style={{ width: `${(backfillProgress.processed / backfillProgress.total) * 100}%` }}
                  />
                </div>
              )}
              <p className="text-xs text-[#98A2B3]">
                A throttled pass against SAP to go easy on the tenant - each run is time-boxed to a few minutes; if there's a large backlog, just click again afterwards to continue.
              </p>
            </div>
          )}
          {backfillStatus === "done" && backfillResult && (
            <div className="py-2 space-y-2" data-testid="inventory-deep-backfill-result">
              <p className="text-sm text-[#101828]">
                Resolved <span className="font-bold">{backfillResult.resolved}</span> of{" "}
                <span className="font-bold">{backfillResult.total}</span> previously-unlinked item(s).
              </p>
              {backfillResult.stopped_early && (
                <p className="text-xs text-[#175CD3] bg-[#EFF8FF] border border-[#B2DDFF] rounded-sm p-2" data-testid="inventory-deep-backfill-stopped-early-warning">
                  Time limit reached for this run - {backfillResult.still_missing} item(s) still need checking. Click "Resolve Missing Values" again to continue where this left off.
                </p>
              )}
              {backfillResult.material_lookup_unauthorized && (
                <p className="text-xs text-[#B54708] bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-2" data-testid="inventory-deep-backfill-unauthorized-warning">
                  SAP rejected the direct Material lookup (missing authorization for "QueryMaterialIn"). Ask your SAP admin to activate the "Query Materials" communication arrangement for our technical user, then run this again to resolve the remaining items.
                </p>
              )}
              {!backfillResult.stopped_early && !backfillResult.material_lookup_unauthorized && backfillResult.still_missing > 0 && (
                <p className="text-xs text-[#98A2B3]">
                  {backfillResult.still_missing} item(s) still have no match in SAP at all - these will keep showing "—" for value.
                </p>
              )}
              {backfillResult.total === 0 && (
                <p className="text-xs text-[#98A2B3]">Every item already has a SAP link - nothing left to resolve.</p>
              )}
              <p className="text-xs text-[#667085] font-sans">Inventory values have been refreshed automatically.</p>
            </div>
          )}
          {backfillStatus === "failed" && (
            <div className="py-2 text-sm text-[#B42318]" data-testid="inventory-deep-backfill-error">
              {backfillError}
            </div>
          )}
          <div className="flex justify-end">
            <Button
              type="button"
              variant="outline"
              onClick={() => setBackfillOpen(false)}
              disabled={backfillStatus === "running"}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="inventory-deep-backfill-close-button"
            >
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
