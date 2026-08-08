import { useCallback, useEffect, useState } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import {
  MagnifyingGlass,
  Circle,
  CheckCircle,
  XCircle,
  X,
  Package,
  Stack,
  CheckSquare,
  ClockCounterClockwise,
  WarningCircle,
  CaretRight,
  CaretDown,
  CaretUp,
  ArrowsOutSimple,
  ArrowsInSimple,
  FileArrowDown,
  Database,
  CurrencyCircleDollar,
  Sparkle,
  ShoppingCartSimple,
} from "@phosphor-icons/react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster, toast } from "@/components/ui/sonner";
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

const collectAllUuids = (nodes) => {
  let uuids = [];
  nodes.forEach((node) => {
    if (node.product_uuid) uuids.push(node.product_uuid);
    if (node.children && node.children.length > 0) {
      uuids = uuids.concat(collectAllUuids(node.children));
    }
  });
  return uuids;
};

const collectAllItems = (nodes) => {
  const items = new Map();
  const walk = (list) => {
    list.forEach((node) => {
      const hasChildren = node.children && node.children.length > 0;
      if (node.product_id && !hasChildren && !items.has(node.product_id)) {
        items.set(node.product_id, { product_id: node.product_id, description: node.description, product_uuid: node.product_uuid });
      }
      if (hasChildren) walk(node.children);
    });
  };
  walk(nodes);
  return Array.from(items.values());
};

const computeTotalCost = (nodes, costs) => {
  // A parent's own Standard Cost already reflects its fully-loaded value
  // (including whatever went into making it, if it's a manufactured
  // sub-assembly) - adding its children's costs on top would double-count.
  // Only fall back to summing a node's children when the node itself has
  // no direct cost of its own.
  const totals = {};
  const directCost = (node) => {
    const cost = node.product_uuid ? costs[node.product_uuid.toUpperCase()] : null;
    if (cost && node.quantity != null) {
      return { currency: cost.currency || "—", amount: cost.amount * node.quantity };
    }
    return null;
  };
  const rollup = (list) => {
    list.forEach((node) => {
      const direct = directCost(node);
      if (direct) {
        totals[direct.currency] = (totals[direct.currency] || 0) + direct.amount;
      } else if (node.children && node.children.length > 0) {
        rollup(node.children);
      }
    });
  };
  rollup(nodes);
  return totals;
};

const nodeKey = (node, prefix) => (prefix ? `${prefix}>${node.product_id}` : node.product_id);

const collectExpandableKeys = (nodes, prefix = "") => {
  let keys = [];
  nodes.forEach((node) => {
    const key = nodeKey(node, prefix);
    if (node.children && node.children.length > 0) {
      keys.push(key);
      keys = keys.concat(collectExpandableKeys(node.children, key));
    }
  });
  return keys;
};

const flattenFullTree = (nodes, depth = 0) => {
  let out = [];
  nodes.forEach((node) => {
    out.push({
      Level: depth + 1,
      "Product ID": node.product_id,
      Description: node.description || "",
      Quantity: node.quantity ?? "",
      UOM: node.unit_of_measure || "",
      ECO: node.eco_id || "",
      Active: node.active ? "Yes" : "No",
    });
    if (node.children && node.children.length > 0) {
      out = out.concat(flattenFullTree(node.children, depth + 1));
    }
  });
  return out;
};

// Keys are derived from each node's product_id chain (not sibling index) so
// expand/collapse state stays anchored to the correct node even after
// sortTree() reorders siblings.
const flattenVisibleTree = (nodes, expandedKeys, depth = 0, prefix = "") => {
  let out = [];
  nodes.forEach((node) => {
    const path = nodeKey(node, prefix);
    const hasChildren = node.children && node.children.length > 0;
    out.push({ node, path, depth, hasChildren });
    if (hasChildren && expandedKeys.has(path)) {
      out = out.concat(flattenVisibleTree(node.children, expandedKeys, depth + 1, path));
    }
  });
  return out;
};

const filterTree = (nodes, query) => {
  if (!query) return nodes;
  const q = query.toLowerCase();
  const result = [];
  nodes.forEach((node) => {
    const selfMatch =
      (node.product_id || "").toLowerCase().includes(q) || (node.description || "").toLowerCase().includes(q);
    if (selfMatch) {
      result.push(node);
      return;
    }
    if (node.children && node.children.length > 0) {
      const filteredChildren = filterTree(node.children, query);
      if (filteredChildren.length > 0) {
        result.push({ ...node, children: filteredChildren });
      }
    }
  });
  return result;
};

const SORT_FIELD_GETTERS = {
  product_id: (node) => (node.product_id || "").toLowerCase(),
  quantity: (node) => (node.quantity != null ? node.quantity : -Infinity),
  std_cost: (node, costs) => {
    const cost = node.product_uuid ? costs[node.product_uuid.toUpperCase()] : null;
    return cost ? cost.amount : -Infinity;
  },
  ext_cost: (node, costs) => {
    const cost = node.product_uuid ? costs[node.product_uuid.toUpperCase()] : null;
    return cost && node.quantity != null ? cost.amount * node.quantity : -Infinity;
  },
};

const sortTree = (nodes, sortConfig, costs) => {
  const { field, direction } = sortConfig;
  if (!field) return nodes;
  const getValue = SORT_FIELD_GETTERS[field];
  const sorted = [...nodes].sort((a, b) => {
    const va = getValue(a, costs);
    const vb = getValue(b, costs);
    if (va < vb) return direction === "asc" ? -1 : 1;
    if (va > vb) return direction === "asc" ? 1 : -1;
    return 0;
  });
  return sorted.map((node) => ({
    ...node,
    children: node.children && node.children.length > 0 ? sortTree(node.children, sortConfig, costs) : node.children,
  }));
};

const highlightMatch = (text, query) => {
  if (!query || !text) return text;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-[#FEF0C7] text-[#B54708] rounded-sm px-0.5">{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  );
};

export default function BomExplorerPage() {
  const [bomId, setBomId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [itemInfo, setItemInfo] = useState(null);
  const [lastSynced, setLastSynced] = useState(null);
  const [connection, setConnection] = useState({ connected: null, message: "Checking connection..." });
  const [expandedKeys, setExpandedKeys] = useState(new Set());
  const [costs, setCosts] = useState({});
  const [loadingCosts, setLoadingCosts] = useState(false);
  const [costsLoaded, setCostsLoaded] = useState(false);
  const [categories, setCategories] = useState({});
  const [loadingCategories, setLoadingCategories] = useState(false);
  const [categoriesLoaded, setCategoriesLoaded] = useState(false);
  const [treeSearch, setTreeSearch] = useState("");
  const [sortConfig, setSortConfig] = useState({ field: null, direction: "asc" });

  const toggleSort = (field) => {
    setSortConfig((prev) =>
      prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }
    );
  };

  const checkConnection = useCallback(async () => {
    try {
      const response = await axios.get(`${API}/bom/connection-status`);
      setConnection(response.data);
    } catch (e) {
      setConnection({ connected: false, message: "Unable to reach backend" });
    }
  }, []);

  useEffect(() => {
    checkConnection();
  }, [checkConnection]);

  const toggleKey = (key) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const expandAll = () => {
    if (!result) return;
    setExpandedKeys(new Set(collectExpandableKeys(result.tree)));
  };

  const collapseAll = () => {
    setExpandedKeys(new Set());
  };

  const exportToExcel = () => {
    if (!result) return;
    const data = flattenFullTree(result.tree);
    const worksheet = XLSX.utils.json_to_sheet(data);
    worksheet["!cols"] = [{ wch: 7 }, { wch: 20 }, { wch: 40 }, { wch: 10 }, { wch: 8 }, { wch: 16 }, { wch: 8 }];
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, "BOM");
    XLSX.writeFile(workbook, `BOM_${result.bom_id}.xlsx`);
    toast.success("Excel file downloaded", { description: `BOM_${result.bom_id}.xlsx` });
  };

  const loadStandardCosts = async () => {
    if (!result) return;
    const uuids = Array.from(new Set(collectAllUuids(result.tree)));
    if (uuids.length === 0) {
      toast.info("No costable components found in this BOM");
      return;
    }
    setLoadingCosts(true);
    try {
      const response = await axios.post(`${API}/bom/standard-costs`, { product_uuids: uuids });
      setCosts(response.data.costs || {});
      setCostsLoaded(true);
      const found = Object.values(response.data.costs || {}).filter(Boolean).length;
      toast.success("Standard costs loaded", { description: `${found} of ${uuids.length} components have a live SAP cost` });
    } catch (err) {
      const detail = err?.response?.data?.detail || "Failed to fetch standard costs from SAP";
      toast.error("Cost lookup failed", { description: detail });
    } finally {
      setLoadingCosts(false);
    }
  };

  const loadCategories = async (treeOverride) => {
    const tree = treeOverride || result?.tree;
    if (!tree) return;
    const items = collectAllItems(tree);
    if (items.length === 0) {
      toast.info("No components found in this BOM");
      return;
    }
    setLoadingCategories(true);
    try {
      const response = await axios.post(`${API}/bom/categorize`, { items });
      setCategories(response.data.categories || {});
      setCategoriesLoaded(true);
      toast.success("AI categorization complete", { description: `${Object.keys(response.data.categories || {}).length} components classified` });
    } catch (err) {
      const detail = err?.response?.data?.detail || "Failed to categorize components with AI";
      toast.error("Categorization failed", { description: detail });
    } finally {
      setLoadingCategories(false);
    }
  };

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!bomId.trim()) return;

    setLoading(true);
    setError(null);
    setResult(null);
    setItemInfo(null);
    setExpandedKeys(new Set());
    setCosts({});
    setCostsLoaded(false);
    setCategories({});
    setCategoriesLoaded(false);
    setTreeSearch("");
    setSortConfig({ field: null, direction: "asc" });

    try {
      const response = await axios.get(`${API}/bom/search`, { params: { bom_id: bomId.trim() } });
      if (response.data.has_bom === false) {
        setItemInfo(response.data.item_info);
        toast.info(`${response.data.item_info.product_id} has no BOM`, {
          description: response.data.item_info.note,
        });
        return;
      }
      setResult(response.data);
      setLastSynced(new Date());
      toast.success(`BOM ${response.data.bom_id} loaded`, {
        description: `${response.data.total_components} components across ${response.data.max_level} levels`,
      });
      loadCategories(response.data.tree);
    } catch (err) {
      const detail = err?.response?.data?.detail || "Failed to fetch BOM from SAP";
      setError(detail);
      toast.error("BOM lookup failed", { description: detail });
    } finally {
      setLoading(false);
    }
  };

  const activeCount = result ? result.total_components : 0;
  const displayTree = result ? sortTree(filterTree(result.tree, treeSearch), sortConfig, costs) : [];
  const effectiveExpandedKeys = treeSearch ? new Set(collectExpandableKeys(displayTree)) : expandedKeys;
  const visibleRows = flattenVisibleTree(displayTree, effectiveExpandedKeys);

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />

      {/* Header */}
      <header className="h-12 bg-[#004B87] shadow-[0_1px_3px_0_rgba(16,24,40,0.1)] flex items-center justify-between px-4 shrink-0 z-10">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2.5" data-testid="app-title">
            <Database size={18} weight="bold" className="text-white" />
            <span className="font-heading text-sm font-bold text-white tracking-tight">SAP BOM Explorer</span>
            <span className="font-sans text-xs text-white/60 hidden sm:inline">| Production Bill of Material</span>
          </div>
          <NavTabs />
        </div>

        <div
          className="flex items-center gap-2 bg-white/10 border border-white/20 px-2.5 py-1 rounded-sm shrink-0"
          data-testid="connection-status-indicator"
        >
          {connection.connected === null ? (
            <Circle size={8} weight="fill" className="text-[#F79009] animate-pulse" />
          ) : connection.connected ? (
            <Circle size={8} weight="fill" className="text-[#12B76A] animate-pulse" />
          ) : (
            <Circle size={8} weight="fill" className="text-[#F04438]" />
          )}
          <span className="font-sans text-xs text-white whitespace-nowrap">
            {connection.connected === null
              ? "Checking SAP..."
              : connection.connected
              ? "SAP PRD Connected"
              : "SAP Disconnected"}
          </span>
        </div>
      </header>

      {/* Toolbar */}
      <div className="bg-white border-b border-[#D0D5DD] p-2 flex items-center gap-3 shrink-0 flex-wrap">
        <form onSubmit={handleSearch} className="flex items-center gap-2">
          <div className="relative">
            <MagnifyingGlass size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input
              value={bomId}
              onChange={(e) => setBomId(e.target.value)}
              placeholder="Part / BOM ID e.g. P26584 or FLT2_4.1"
              className="h-8 pl-7 w-72 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
              data-testid="bom-id-search-input"
            />
          </div>
          <Button
            type="submit"
            disabled={loading || !bomId.trim()}
            className="h-8 bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D] text-white rounded-sm px-4 text-[13px] font-bold transition-colors"
            data-testid="bom-search-submit-button"
          >
            {loading ? "Searching..." : "Pull BOM"}
          </Button>
        </form>

        {result && (
          <Badge
            variant="outline"
            className="bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] rounded font-sans text-xs h-8 flex items-center"
            data-testid="resolved-bom-id-badge"
          >
            Resolved: {result.bom_id}
          </Badge>
        )}

        <div className="flex items-center gap-2 ml-auto">
          <div className="relative">
            <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input
              value={treeSearch}
              onChange={(e) => setTreeSearch(e.target.value)}
              placeholder="Filter tree by ID or description"
              disabled={!result}
              className="h-8 pl-7 pr-7 w-56 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
              data-testid="bom-tree-search-input"
            />
            {treeSearch && (
              <button
                type="button"
                onClick={() => setTreeSearch("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-[#98A2B3] hover:text-[#344054]"
                data-testid="bom-tree-search-clear-button"
              >
                <X size={13} />
              </button>
            )}
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={expandAll}
            disabled={!result}
            className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054] transition-colors"
            data-testid="expand-all-button"
          >
            <ArrowsOutSimple size={13} className="mr-1.5" />
            Expand All
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={collapseAll}
            disabled={!result}
            className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054] transition-colors"
            data-testid="collapse-all-button"
          >
            <ArrowsInSimple size={13} className="mr-1.5" />
            Collapse All
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={loadStandardCosts}
            disabled={!result || loadingCosts}
            className="h-8 bg-[#7A271A] hover:bg-[#611C13] text-white text-xs rounded-sm transition-colors"
            data-testid="load-standard-costs-button"
          >
            <CurrencyCircleDollar size={13} className="mr-1.5" />
            {loadingCosts ? "Loading Costs..." : costsLoaded ? "Refresh Costs" : "Load Standard Costs"}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={loadCategories}
            disabled={!result || loadingCategories}
            className="h-8 bg-[#5925DC] hover:bg-[#4A1FB8] text-white text-xs rounded-sm transition-colors"
            data-testid="ai-categorize-button"
          >
            <Sparkle size={13} className="mr-1.5" />
            {loadingCategories ? "Categorizing..." : categoriesLoaded ? "Re-Categorize" : "AI Categorize"}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={exportToExcel}
            disabled={!result}
            className="h-8 bg-[#027A48] hover:bg-[#02623A] text-white text-xs rounded-sm transition-colors"
            data-testid="export-excel-button"
          >
            <FileArrowDown size={13} className="mr-1.5" />
            Export to Excel
          </Button>
        </div>
      </div>

      {/* Content */}
      <main className="flex-1 overflow-auto p-4">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
          <StatCard icon={Stack} label="Max Level" value={result ? result.max_level : "—"} testId="stat-total-groups" />
          <StatCard
            icon={Package}
            label="Total Components"
            value={result ? result.total_components : "—"}
            testId="stat-total-components"
          />
          <StatCard icon={CheckSquare} label="Active Materials" value={result ? activeCount : "—"} testId="stat-active-materials" />
          <StatCard
            icon={CurrencyCircleDollar}
            label="Total BOM Cost"
            value={
              costsLoaded && result
                ? Object.entries(computeTotalCost(result.tree, costs))
                    .map(([currency, total]) => `${currency} ${total.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`)
                    .join(" + ") || "No cost data"
                : "—"
            }
            testId="stat-total-bom-cost"
          />
          <StatCard
            icon={ClockCounterClockwise}
            label="Last Synced"
            value={lastSynced ? lastSynced.toLocaleTimeString() : "—"}
            testId="stat-last-synced"
          />
        </div>

        {error && (
          <Alert variant="destructive" className="mb-4 rounded-sm border-[#F04438]/40 bg-[#FEF3F2]" data-testid="bom-search-error-alert">
            <WarningCircle size={16} />
            <AlertTitle className="font-heading text-sm">Lookup failed</AlertTitle>
            <AlertDescription className="font-sans text-[13px]">{error}</AlertDescription>
          </Alert>
        )}

        {loading && (
          <div className="space-y-1.5" data-testid="bom-loading-skeleton">
            {[...Array(8)].map((_, i) => (
              <Skeleton key={i} className="h-8 w-full rounded-sm" />
            ))}
          </div>
        )}

        {!loading && result && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="bom-results-table-container">
            <table className="border-collapse w-full" data-testid="bom-tree-table">
              <thead>
                <tr>
                  {[
                    { label: "Level", field: null },
                    { label: "Product ID", field: "product_id" },
                    { label: "Description", field: null },
                    { label: "Category", field: null },
                    { label: "Quantity", field: "quantity" },
                    { label: "UOM", field: null },
                    { label: "ECO", field: null },
                    { label: "Active", field: null },
                    { label: "Std Cost", field: "std_cost" },
                    { label: "Ext Cost", field: "ext_cost" },
                  ].map(({ label, field }) => (
                    <th
                      key={label}
                      onClick={field ? () => toggleSort(field) : undefined}
                      className={`bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide ${
                        field ? "cursor-pointer hover:bg-[#DDE1E8] select-none" : ""
                      }`}
                      data-testid={field ? `bom-sort-header-${field}` : undefined}
                    >
                      <span className="inline-flex items-center gap-1">
                        {label}
                        {field &&
                          sortConfig.field === field &&
                          (sortConfig.direction === "asc" ? (
                            <CaretUp size={10} weight="bold" />
                          ) : (
                            <CaretDown size={10} weight="bold" />
                          ))}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleRows.map(({ node, path, depth, hasChildren }, i) => (
                  <tr
                    key={path}
                    className={`${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] transition-colors duration-150`}
                    data-testid={`bom-row-${path}`}
                  >
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828]">{node.level}</td>
                    <td
                      className={`border border-[#D0D5DD] py-1 text-[13px] tabular-nums text-[#101828] ${
                        hasChildren ? "cursor-pointer hover:bg-[#E5F0FA]" : ""
                      }`}
                      style={{ paddingLeft: `${depth * 24 + 8}px`, paddingRight: "8px" }}
                      onClick={hasChildren ? () => toggleKey(path) : undefined}
                      data-testid={`bom-toggle-${path}`}
                    >
                      <span className="inline-flex items-center gap-1.5">
                        {hasChildren ? (
                          <span className="text-[#004B87]">
                            {effectiveExpandedKeys.has(path) ? (
                              <CaretDown size={12} weight="bold" />
                            ) : (
                              <CaretRight size={12} weight="bold" />
                            )}
                          </span>
                        ) : (
                          <span className="w-3" />
                        )}
                        {highlightMatch(node.product_id, treeSearch)}
                      </span>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">
                      {highlightMatch(node.description || "—", treeSearch)}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px]" data-testid={`bom-category-${path}`}>
                      {hasChildren ? (
                        <span className="text-[#98A2B3] italic">Sub-Assembly</span>
                      ) : loadingCategories ? (
                        <span className="text-[#98A2B3]">…</span>
                      ) : categoriesLoaded ? (
                        categories[node.product_id] ? (
                          <Badge className="bg-[#F4F3FF] text-[#5925DC] border-[#D9D6FE] rounded" variant="outline">
                            {categories[node.product_id]}
                          </Badge>
                        ) : (
                          <span className="text-[#98A2B3]">—</span>
                        )
                      ) : (
                        <span className="text-[#98A2B3]">—</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828]">{node.quantity ?? "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">{node.unit_of_measure || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] text-[#101828]">{node.eco_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1">
                      <Badge
                        className={
                          node.active
                            ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] rounded"
                            : "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA] rounded"
                        }
                        variant="outline"
                      >
                        {node.active ? (
                          <CheckCircle size={12} weight="fill" className="mr-1" />
                        ) : (
                          <XCircle size={12} weight="fill" className="mr-1" />
                        )}
                        {node.active ? "Yes" : "No"}
                      </Badge>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828]" data-testid={`bom-std-cost-${path}`}>
                      {loadingCosts ? (
                        <span className="text-[#98A2B3]">…</span>
                      ) : costsLoaded ? (
                        costs[(node.product_uuid || "").toUpperCase()] ? (
                          `${costs[node.product_uuid.toUpperCase()].currency || ""} ${costs[node.product_uuid.toUpperCase()].amount.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                        ) : (
                          <span className="text-[#98A2B3]">No cost</span>
                        )
                      ) : (
                        <span className="text-[#98A2B3]">—</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] font-medium" data-testid={`bom-ext-cost-${path}`}>
                      {costsLoaded && costs[(node.product_uuid || "").toUpperCase()] && node.quantity != null
                        ? `${costs[node.product_uuid.toUpperCase()].currency || ""} ${(costs[node.product_uuid.toUpperCase()].amount * node.quantity).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                        : <span className="text-[#98A2B3]">—</span>}
                    </td>
                  </tr>
                ))}
                {result.total_components === 0 && (
                  <tr>
                    <td colSpan={10} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]">
                      No components found for this BOM
                    </td>
                  </tr>
                )}
                {result.total_components > 0 && visibleRows.length === 0 && (
                  <tr>
                    <td
                      colSpan={10}
                      className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]"
                      data-testid="bom-search-no-matches"
                    >
                      No components match "{treeSearch}"
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {!loading && itemInfo && (
          <div
            className="border border-[#D0D5DD] rounded-sm bg-white p-5 flex flex-col gap-3"
            data-testid="bom-item-lookup-card"
          >
            <div className="flex items-center gap-2">
              <Package size={18} weight="bold" className="text-[#B54708]" />
              <span className="font-heading text-sm font-bold text-[#101828]">{itemInfo.product_id}</span>
              <Badge className="bg-[#FFFAEB] text-[#B54708] border-[#FEDF89] rounded" variant="outline">
                No Bill of Materials
              </Badge>
            </div>
            <p className="font-sans text-[13px] text-[#475467]">{itemInfo.note}</p>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4 pt-2 border-t border-[#EAECF0]">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-[#98A2B3] font-heading">Description</p>
                <p className="text-[13px] text-[#101828]" data-testid="bom-item-lookup-description">{itemInfo.description || "—"}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-[#98A2B3] font-heading">Category</p>
                <p className="text-[13px] text-[#101828]" data-testid="bom-item-lookup-category">{itemInfo.category || "Uncategorized"}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-[#98A2B3] font-heading">On-Hand Qty</p>
                <p className="text-[13px] text-[#101828] tabular-nums" data-testid="bom-item-lookup-qty">
                  {itemInfo.on_hand_qty != null ? `${itemInfo.on_hand_qty.toLocaleString()} ${itemInfo.uom || ""}` : "—"}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-[#98A2B3] font-heading">Unit Cost</p>
                <p className="text-[13px] text-[#101828] tabular-nums" data-testid="bom-item-lookup-cost">
                  {itemInfo.unit_cost != null ? `${itemInfo.currency || ""} ${itemInfo.unit_cost.toLocaleString()}` : "—"}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-[#98A2B3] font-heading">Data Source</p>
                <p className="text-[13px] text-[#101828]">
                  {itemInfo.cost_source === "live" ? "Cost: live SAP" : "Cost: unavailable"} · Other fields: cached
                </p>
              </div>
            </div>
          </div>
        )}

        {!loading && !result && !error && !itemInfo && (
          <div
            className="border border-dashed border-[#D0D5DD] rounded-sm py-16 flex flex-col items-center gap-3 text-[#98A2B3] bg-white"
            data-testid="bom-empty-state"
          >
            <Package size={28} weight="regular" />
            <p className="font-sans text-[13px]">Enter a Part/BOM ID above and click "Pull BOM" to fetch data from SAP</p>
          </div>
        )}
      </main>
    </div>
  );
}
