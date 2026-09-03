import { useCallback, useEffect, useState } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import {
  MagnifyingGlass,
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
  Shield,
  CurrencyCircleDollar,
  Sparkle,
  ShoppingCartSimple,
  FileImage,
  PlayCircle,
  ArrowsClockwise,
  Scales,
  ArrowSquareOut,
} from "@phosphor-icons/react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster, toast } from "@/components/ui/sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

// Sep 3 2026, user's explicit ask: "Latest Drawing" column, sourced from
// the sister QMS Emergent app's External Drawings API (server-side
// proxy at /bom/qms-drawing/*, see server.py + qms_drawings_client.py -
// the API key never reaches the browser, per that API's own design
// principle). Distinct from the existing drawingUrls pill next to the
// Product ID above (SAP attachments, cached) - this is QMS's own
// engineering drawing + revision history, fetched live on click.
function QmsDrawingModal({ partNo, onClose }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [history, setHistory] = useState({ open: false, loading: false, error: null, rows: [] });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, error: null, data: null });
    axios.get(`${API}/bom/qms-drawing/${encodeURIComponent(partNo)}`)
      .then(({ data }) => { if (!cancelled) setState({ loading: false, error: null, data }); })
      .catch((e) => {
        if (cancelled) return;
        const msg = e?.response?.status === 404 ? "No drawing published in QMS for this part yet." : "Could not load this drawing.";
        setState({ loading: false, error: msg, data: null });
      });
    return () => { cancelled = true; };
  }, [partNo]);

  const loadHistory = () => {
    setHistory({ open: true, loading: true, error: null, rows: [] });
    axios.get(`${API}/bom/qms-drawing/${encodeURIComponent(partNo)}/history`)
      .then(({ data }) => setHistory({ open: true, loading: false, error: null, rows: data.revisions || [] }))
      .catch(() => setHistory({ open: true, loading: false, error: "Could not load revision history.", rows: [] }));
  };

  const current = state.data?.current;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md" data-testid="qms-drawing-modal">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-[#101828]">
            <FileImage size={18} weight="fill" className="text-[#004B87]" />
            Drawing · {partNo}
          </DialogTitle>
        </DialogHeader>
        <p className="text-xs text-[#667085] -mt-2">Sourced live from Radish QMS</p>
        {state.loading && (
          <div className="flex items-center gap-2 py-6 text-sm text-[#667085]" data-testid="qms-drawing-loading">
            <ArrowsClockwise size={16} className="animate-spin" /> Fetching latest drawing…
          </div>
        )}
        {state.error && (
          <div className="flex items-start gap-2 rounded-sm border border-[#FDA29B] bg-[#FEF3F2] p-3 text-sm text-[#912018]" data-testid="qms-drawing-error">
            <WarningCircle size={16} className="shrink-0 mt-0.5" /> {state.error}
          </div>
        )}
        {current && (
          <div className="space-y-3" data-testid="qms-drawing-current">
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <div className="text-[10px] uppercase tracking-wide font-bold text-[#667085]">Part name</div>
                <div className="text-sm text-[#101828]">{current.part_name || "—"}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wide font-bold text-[#667085]">Revision</div>
                <div className="text-sm text-[#101828] font-mono">{current.revision_number || "—"}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wide font-bold text-[#667085]">Inspection type</div>
                <div className="text-sm text-[#101828] capitalize">{current.inspection_type || "—"}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wide font-bold text-[#667085]">Last updated</div>
                <div className="text-sm text-[#101828]">{current.updated_at ? new Date(current.updated_at).toLocaleDateString("en-IN") : "—"}</div>
              </div>
            </div>
            {current.drawing_filename && (
              <p className="text-[11px] text-[#667085] font-mono truncate">{current.drawing_filename}</p>
            )}
            <div className="flex flex-wrap gap-2 justify-end pt-2 border-t border-[#EAECF0]">
              <Button variant="outline" size="sm" onClick={loadHistory} data-testid="qms-drawing-see-history">
                <ClockCounterClockwise size={14} className="mr-1.5" /> See previous versions
              </Button>
              <Button asChild size="sm" data-testid="qms-drawing-view-latest">
                <a href={current.drawing_url} target="_blank" rel="noopener noreferrer">
                  <ArrowSquareOut size={14} className="mr-1.5" /> View latest drawing
                </a>
              </Button>
            </div>
          </div>
        )}
        {history.open && (
          <div className="mt-2 pt-3 border-t border-[#EAECF0] space-y-2" data-testid="qms-drawing-history-panel">
            <p className="text-xs font-bold text-[#344054] uppercase tracking-wide">Previous Versions</p>
            {history.loading && <p className="text-xs text-[#667085]">Loading revision history…</p>}
            {history.error && <p className="text-xs text-[#912018]">{history.error}</p>}
            {!history.loading && !history.error && history.rows.length === 0 && (
              <p className="text-xs text-[#98A2B3]">No revisions recorded.</p>
            )}
            <ul className="divide-y divide-[#EAECF0] max-h-56 overflow-y-auto">
              {history.rows.map((r) => (
                <li key={r.template_id} className="flex items-center justify-between gap-2 py-2" data-testid={`qms-drawing-history-row-${r.template_id}`}>
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-sm font-semibold text-[#101828] font-mono">Rev {r.revision_number || "—"}</span>
                      <Badge variant="outline" className={
                        r.status === "published" ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] text-[10px] px-1.5 py-0"
                        : r.status === "draft" ? "bg-[#FFFAEB] text-[#B54708] border-[#FEC84B] text-[10px] px-1.5 py-0"
                        : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-[10px] px-1.5 py-0"
                      }>{r.status}</Badge>
                      <span className="text-[11px] text-[#667085] capitalize">{r.inspection_type}</span>
                    </div>
                    <div className="text-[11px] text-[#667085]">{r.updated_at ? new Date(r.updated_at).toLocaleDateString("en-IN") : "—"}</div>
                  </div>
                  {r.has_drawing ? (
                    <a href={r.drawing_url} target="_blank" rel="noopener noreferrer" className="shrink-0 text-xs font-semibold text-[#004B87] hover:underline inline-flex items-center gap-1">
                      <ArrowSquareOut size={12} /> Open
                    </a>
                  ) : (
                    <span className="shrink-0 text-[11px] text-[#98A2B3] italic">No file</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}


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

// Every product_id in the tree (leaf AND sub-assembly/parent) - unlike
// collectAllItems above (leaf-only, for AI categorization), a drawing/
// documentation link can exist on any Material master, parent or not.
const collectAllProductIds = (nodes) => {
  const ids = new Set();
  const walk = (list) => {
    list.forEach((node) => {
      if (node.product_id) ids.add(node.product_id);
      if (node.children && node.children.length > 0) walk(node.children);
    });
  };
  walk(nodes);
  return Array.from(ids);
};

// SAP occasionally has an EXPLICIT standard-cost record of exactly 0.00 for
// a sub-assembly that was created but never actually run through a Cost
// Estimate/Cost Roll-up in SAP - in practice that means "not yet costed",
// never a genuinely free component. Treating that 0 the same as a real
// cost (as opposed to treating it like "no cost on file") meant a single
// un-costed sub-assembly could silently zero out - and understate - both
// its own row AND the whole BOM's total. Fix: treat missing AND exactly-0
// the same way, falling back to summing the node's own children (their
// quantities are already expressed per 1 unit of this node, so the sum
// IS this node's own per-unit cost) - only reporting "no cost" if that
// rollup also comes up empty.
const getEffectiveCost = (node, costs) => {
  const cost = node.product_uuid ? costs[(node.product_uuid || "").toUpperCase()] : null;
  if (cost && cost.amount > 0) {
    return { amount: cost.amount, currency: cost.currency || "—", isRollup: false };
  }
  if (node.children && node.children.length > 0) {
    let total = 0;
    let currency = null;
    let anyFound = false;
    node.children.forEach((child) => {
      const childCost = getEffectiveCost(child, costs);
      if (childCost && child.quantity != null) {
        total += childCost.amount * child.quantity;
        currency = currency || childCost.currency;
        anyFound = true;
      }
    });
    if (anyFound) return { amount: total, currency: currency || "—", isRollup: true };
  }
  return null;
};

const computeTotalCost = (nodes, costs) => {
  const totals = {};
  nodes.forEach((node) => {
    const effective = getEffectiveCost(node, costs);
    if (effective && node.quantity != null) {
      totals[effective.currency] = (totals[effective.currency] || 0) + effective.amount * node.quantity;
    }
  });
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

const flattenFullTree = (nodes, depth = 0, costs = null, costsLoaded = false) => {
  let out = [];
  nodes.forEach((node) => {
    const hasChildren = node.children && node.children.length > 0;
    const row = {
      Level: depth + 1,
      "Product ID": node.product_id,
      Description: node.description || "",
      Quantity: node.quantity ?? "",
      UOM: node.unit_of_measure || "",
      ECO: node.eco_id || "",
      Active: node.active ? "Yes" : "No",
    };
    if (costsLoaded) {
      // Sep 3 2026, user's explicit ask: this export lists EVERY level
      // (assembly rollup rows AND their own drill-down children) in one
      // flat sheet. Showing a cost on an assembly row alongside its own
      // children's costs double-counts if anyone sums the column -
      // Total BOM Cost above only ever sums level-1 rows (see
      // computeTotalCost), never the full flattened list. So: only leaf
      // rows (no children) carry a cost here; assembly rows are blank -
      // the authoritative TOTAL row appended in exportToExcel is the
      // only number that should be trusted for a grand total.
      const effective = hasChildren ? null : getEffectiveCost(node, costs);
      row["Std Cost"] = effective ? Number(effective.amount.toFixed(2)) : "";
      row["Currency"] = effective ? effective.currency || "" : "";
      row["Ext. Cost"] = effective && node.quantity != null ? Number((effective.amount * node.quantity).toFixed(2)) : "";
      row["Cost Source"] = hasChildren ? "" : effective ? (effective.isRollup ? "Rolled-up from components" : "Direct SAP Standard Cost") : "No cost";
    }
    out.push(row);
    if (hasChildren) {
      out = out.concat(flattenFullTree(node.children, depth + 1, costs, costsLoaded));
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
    const cost = getEffectiveCost(node, costs);
    return cost ? cost.amount : -Infinity;
  },
  ext_cost: (node, costs) => {
    const cost = getEffectiveCost(node, costs);
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
  const [expandedKeys, setExpandedKeys] = useState(new Set());
  const [costs, setCosts] = useState({});
  const [loadingCosts, setLoadingCosts] = useState(false);
  const [costsLoaded, setCostsLoaded] = useState(false);
  const [categories, setCategories] = useState({});
  const [loadingCategories, setLoadingCategories] = useState(false);
  const [categoriesLoaded, setCategoriesLoaded] = useState(false);
  const [drawingUrls, setDrawingUrls] = useState({});
  const [comments, setComments] = useState({});
  const [netWeights, setNetWeights] = useState({});
  const [refreshingAttachments, setRefreshingAttachments] = useState(false);
  const [refreshProgress, setRefreshProgress] = useState("");
  const [treeSearch, setTreeSearch] = useState("");
  const [sortConfig, setSortConfig] = useState({ field: null, direction: "asc" });
  const [runningCostEstimate, setRunningCostEstimate] = useState(null);
  const [qmsDrawingPartNo, setQmsDrawingPartNo] = useState(null);
  const [qmsDrawingExists, setQmsDrawingExists] = useState({});
  const [loadingQmsDrawingExists, setLoadingQmsDrawingExists] = useState(false);

  const toggleSort = (field) => {
    setSortConfig((prev) =>
      prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }
    );
  };

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const searchParam = params.get("search");
    if (searchParam) {
      setBomId(searchParam);
      handleSearch({ preventDefault: () => {} }, searchParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    const data = flattenFullTree(result.tree, 0, costs, costsLoaded);
    if (costsLoaded) {
      // Sep 3 2026, user's explicit ask: one authoritative TOTAL row,
      // computed exactly the same way as the "Total BOM Cost" stat card
      // above (computeTotalCost sums ONLY level-1/root nodes - each
      // already carries SAP's own fully-rolled-up cost for everything
      // beneath it) - guaranteed to match the browser number, unlike a
      // naive spreadsheet SUM() over every flattened row.
      const totals = computeTotalCost(result.tree, costs);
      Object.entries(totals).forEach(([currency, total], idx) => {
        data.push({
          Level: "", "Product ID": "", Description: idx === 0 ? "TOTAL BOM COST (matches browser, level-1 rollup only)" : "",
          Quantity: "", UOM: "", ECO: "", Active: "",
          "Std Cost": "", Currency: currency, "Ext. Cost": Number(total.toFixed(2)), "Cost Source": "",
        });
      });
    }
    const worksheet = XLSX.utils.json_to_sheet(data);
    const baseCols = [{ wch: 7 }, { wch: 20 }, { wch: 40 }, { wch: 10 }, { wch: 8 }, { wch: 16 }, { wch: 8 }];
    worksheet["!cols"] = costsLoaded ? baseCols.concat([{ wch: 12 }, { wch: 10 }, { wch: 12 }, { wch: 24 }]) : baseCols;
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, "BOM");
    XLSX.writeFile(workbook, `BOM_${result.bom_id}.xlsx`);
    toast.success("Excel file downloaded", {
      description: costsLoaded ? `BOM_${result.bom_id}.xlsx (with standard costs)` : `BOM_${result.bom_id}.xlsx`,
    });
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

  const loadDrawingUrls = async (treeOverride) => {
    const tree = treeOverride || result?.tree;
    if (!tree) return;
    const productIds = collectAllProductIds(tree);
    if (productIds.length === 0) return;
    try {
      const [drawingResponse, commentsResponse, netWeightResponse] = await Promise.all([
        axios.get(`${API}/bom/drawing-urls`, { params: { product_ids: productIds.join(",") } }),
        axios.get(`${API}/bom/comments`, { params: { product_ids: productIds.join(",") } }),
        axios.get(`${API}/bom/net-weight`, { params: { product_ids: productIds.join(",") } }),
      ]);
      setDrawingUrls(drawingResponse.data || {});
      setComments(commentsResponse.data || {});
      setNetWeights(netWeightResponse.data || {});
    } catch {
      // Non-critical, read-only cache lookup - silently skip, drawings/comments just won't show this load.
    }
    // Sep 3 2026, user's explicit ask: check QMS drawing existence for
    // every visible part up front (own request, separate from the
    // Promise.all above - a slow/unreachable QMS shouldn't block the
    // other three cached lookups) so the cell can show "Drawing not
    // available" instead of an always-clickable button.
    setLoadingQmsDrawingExists(true);
    try {
      const { data } = await axios.get(`${API}/bom/qms-drawing-exists`, { params: { product_ids: productIds.join(",") } });
      setQmsDrawingExists(data || {});
    } catch {
      setQmsDrawingExists({});
    } finally {
      setLoadingQmsDrawingExists(false);
    }
  };

  const REFRESH_BATCH_SIZE = 20;

  const refreshAttachments = async () => {
    const tree = result?.tree;
    if (!tree) return;
    const productIds = collectAllProductIds(tree);
    if (productIds.length === 0) return;
    setRefreshingAttachments(true);
    const batches = [];
    for (let i = 0; i < productIds.length; i += REFRESH_BATCH_SIZE) {
      batches.push(productIds.slice(i, i + REFRESH_BATCH_SIZE));
    }
    let totalChecked = 0;
    let totalFound = 0;
    try {
      for (let i = 0; i < batches.length; i++) {
        setRefreshProgress(batches.length > 1 ? `${i + 1}/${batches.length}` : "");
        const response = await axios.post(`${API}/bom/refresh-attachments`, { product_ids: batches[i] });
        totalChecked += response.data.checked;
        totalFound += response.data.found;
        await loadDrawingUrls(tree);
      }
      toast.success("Attachments refreshed", {
        description: `Checked ${totalChecked} part(s) live from SAP, found drawings/ECNs on ${totalFound}.`,
      });
    } catch (err) {
      toast.error("Failed to refresh attachments", {
        description: err?.response?.data?.detail || "Please try again",
      });
    } finally {
      setRefreshingAttachments(false);
      setRefreshProgress("");
    }
  };

  const runCostEstimate = async (node) => {
    setRunningCostEstimate(node.product_id);
    try {
      const response = await axios.post(`${API}/sap/cost-estimate-run`, {
        product_id: node.product_id,
        product_uuid: node.product_uuid,
      });
      toast.success(`Cost Estimate Run submitted for ${node.product_id}`, {
        description: response.data.run_id
          ? `SAP Run ID: ${response.data.run_id}. Re-load Standard Costs shortly to see the updated value.`
          : "SAP accepted the run. Re-load Standard Costs shortly to see the updated value.",
      });
    } catch (err) {
      const detail = err?.response?.data?.detail || "Failed to submit Cost Estimate Run to SAP";
      toast.error(`Cost Estimate Run failed for ${node.product_id}`, { description: detail, duration: 15000 });
    } finally {
      setRunningCostEstimate(null);
    }
  };

  const handleSearch = async (e, overrideId) => {
    e.preventDefault();
    const searchId = (overrideId ?? bomId).trim();
    if (!searchId) return;

    setLoading(true);
    setError(null);
    setResult(null);
    setItemInfo(null);
    setExpandedKeys(new Set());
    setCosts({});
    setCostsLoaded(false);
    setCategories({});
    setCategoriesLoaded(false);
    setDrawingUrls({});
    setTreeSearch("");
    setSortConfig({ field: null, direction: "asc" });

    try {
      const response = await axios.get(`${API}/bom/search`, { params: { bom_id: searchId } });
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
      loadDrawingUrls(response.data.tree);
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

      {/* Header - "Materials Hub" redesign preview (Feb 2026 design pass) */}
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Production Bill of Material</span>
          </div>
        </div>

        <div className="w-px h-7 bg-white/25 shrink-0" />

        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>

        <SapConnectionStatus />
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
            onClick={refreshAttachments}
            disabled={!result || refreshingAttachments}
            className="h-8 bg-[#B54708] hover:bg-[#93370D] text-white text-xs rounded-sm transition-colors"
            data-testid="refresh-attachments-button"
          >
            <ArrowsClockwise size={13} className={`mr-1.5 ${refreshingAttachments ? "animate-spin" : ""}`} />
            {refreshingAttachments ? `Refreshing${refreshProgress ? ` ${refreshProgress}` : "..."}` : "Refresh Drawings/ECNs"}
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
                    { label: "Latest Drawing", field: null },
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
                      style={{ paddingLeft: `calc(8px + ${depth} * min(24px, 3.5vw))`, paddingRight: "8px" }}
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
                        {drawingUrls[node.product_id] && (
                          <a
                            href={drawingUrls[node.product_id]}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="text-[#9E165F] hover:text-[#7A1049] shrink-0"
                            title="View drawing / documentation"
                            data-testid={`bom-drawing-link-${path}`}
                          >
                            <FileImage size={13} weight="fill" />
                          </a>
                        )}
                        {comments[node.product_id]?.length > 0 && (
                          <Popover>
                            <PopoverTrigger asChild>
                              <button
                                type="button"
                                onClick={(e) => e.stopPropagation()}
                                className="shrink-0 px-1 py-0.5 rounded border border-[#B54708] text-[9px] font-bold uppercase tracking-wide text-[#B54708] bg-[#FFFAEB] hover:bg-[#FEF0C7] leading-none"
                                title="View Engineering Change Notice(s)"
                                data-testid={`bom-comment-link-${path}`}
                              >
                                ECN
                              </button>
                            </PopoverTrigger>
                            <PopoverContent
                              className="w-80 max-h-72 overflow-y-auto p-3"
                              onClick={(e) => e.stopPropagation()}
                              data-testid={`bom-comment-popover-${path}`}
                            >
                              <div className="text-xs font-bold text-[#344054] uppercase tracking-wide mb-2">
                                Engineering Change Notice(s) - {node.product_id}
                              </div>
                              <div className="space-y-3">
                                {[...comments[node.product_id]].reverse().map((c, ci) => (
                                  <div key={ci} className="border-l-2 border-[#B54708]/30 pl-2">
                                    <div className="flex items-center gap-2 text-[11px] text-[#667085] mb-0.5">
                                      <span className="font-semibold text-[#344054]">{c.title || "Document"}</span>
                                      {c.type_label && <Badge variant="outline" className="text-[10px] px-1.5 py-0">{c.type_label}</Badge>}
                                    </div>
                                    <p className="text-[13px] text-[#101828] whitespace-pre-wrap">{c.comment}</p>
                                  </div>
                                ))}
                              </div>
                            </PopoverContent>
                          </Popover>
                        )}
                        {netWeights[node.product_id] != null && (
                          <span
                            className="shrink-0 px-1 py-0.5 rounded border border-[#175CD3] text-[9px] font-bold text-[#175CD3] bg-[#EFF8FF] leading-none inline-flex items-center gap-0.5"
                            title="Net Weight"
                            data-testid={`bom-net-weight-${path}`}
                          >
                            <Scales size={9} weight="bold" />
                            {netWeights[node.product_id]} kg
                          </span>
                        )}
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
                        (() => {
                          const effective = getEffectiveCost(node, costs);
                          if (!effective) return <span className="text-[#98A2B3]">No cost</span>;
                          const text = `${effective.currency || ""} ${effective.amount.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
                          return effective.isRollup ? (
                            <span className="inline-flex items-center gap-1.5">
                              <span
                                className="inline-flex items-center gap-1 text-[#B54708]"
                                title="SAP has no direct Standard Cost on file for this sub-assembly (or shows it as exactly 0.00 - not yet cost-rolled) - showing the sum of its own components instead."
                                data-testid={`bom-std-cost-rollup-flag-${path}`}
                              >
                                <WarningCircle size={12} weight="fill" className="shrink-0" />
                                {text}
                              </span>
                              {node.product_uuid && (
                                <button
                                  type="button"
                                  onClick={() => runCostEstimate(node)}
                                  disabled={runningCostEstimate === node.product_id}
                                  title="Trigger a real SAP Cost Estimate Run for this material so SAP itself calculates a genuine Standard Cost"
                                  className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-[#004B87] border border-[#B8D4ED] bg-[#E5F0FA] hover:bg-[#D3E5F5] disabled:opacity-50 disabled:cursor-not-allowed rounded-sm px-1.5 py-0.5 transition-colors shrink-0"
                                  data-testid={`bom-run-cost-estimate-${path}`}
                                >
                                  <PlayCircle size={11} weight="fill" />
                                  {runningCostEstimate === node.product_id ? "Running..." : "Run Cost Estimate"}
                                </button>
                              )}
                            </span>
                          ) : (
                            text
                          );
                        })()
                      ) : (
                        <span className="text-[#98A2B3]">—</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px] tabular-nums text-[#101828] font-medium" data-testid={`bom-ext-cost-${path}`}>
                      {costsLoaded && node.quantity != null && getEffectiveCost(node, costs)
                        ? (() => {
                            const effective = getEffectiveCost(node, costs);
                            const text = `${effective.currency || ""} ${(effective.amount * node.quantity).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
                            return effective.isRollup ? (
                              <span className="inline-flex items-center gap-1 text-[#B54708]" title="Rolled up from this sub-assembly's own components - see Std Cost column.">
                                <WarningCircle size={12} weight="fill" className="shrink-0" />
                                {text}
                              </span>
                            ) : (
                              text
                            );
                          })()
                        : <span className="text-[#98A2B3]">—</span>}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1 text-[13px]" data-testid={`bom-qms-drawing-${path}`}>
                      {loadingQmsDrawingExists ? (
                        <span className="text-[#98A2B3]">Checking…</span>
                      ) : qmsDrawingExists[node.product_id] === false ? (
                        <span className="text-[11px] text-[#98A2B3] italic" data-testid={`bom-qms-drawing-unavailable-${path}`}>
                          Drawing not available
                        </span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setQmsDrawingPartNo(node.product_id)}
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm border border-[#B8D4ED] bg-[#E5F0FA] text-[10px] font-bold uppercase tracking-wide text-[#004B87] hover:bg-[#D3E5F5] transition-colors"
                          title="View latest drawing from QMS"
                          data-testid={`bom-qms-drawing-button-${path}`}
                        >
                          <FileImage size={11} weight="fill" /> View Drawing
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {result.total_components === 0 && (
                  <tr>
                    <td colSpan={11} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]">
                      No components found for this BOM
                    </td>
                  </tr>
                )}
                {result.total_components > 0 && visibleRows.length === 0 && (
                  <tr>
                    <td
                      colSpan={11}
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
      {qmsDrawingPartNo && (
        <QmsDrawingModal partNo={qmsDrawingPartNo} onClose={() => setQmsDrawingPartNo(null)} />
      )}
    </div>
  );
}
