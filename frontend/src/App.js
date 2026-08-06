import { useCallback, useEffect, useState } from "react";
import "@/App.css";
import axios from "axios";
import {
  MagnifyingGlass,
  Circle,
  CheckCircle,
  XCircle,
  Package,
  Stack,
  CheckSquare,
  ClockCounterClockwise,
  WarningCircle,
  CaretRight,
  CaretDown,
  ArrowsOutSimple,
  ArrowsInSimple,
} from "@phosphor-icons/react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Toaster, toast } from "@/components/ui/sonner";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const StatCard = ({ icon: Icon, label, value, testId }) => (
  <div
    className="border border-border/40 bg-white p-5 flex flex-col gap-2"
    data-testid={testId}
  >
    <div className="flex items-center gap-2 text-[#0A2540]/60">
      <Icon size={16} weight="regular" />
      <span className="font-heading text-xs uppercase tracking-wide">{label}</span>
    </div>
    <span className="font-data text-2xl tabular-nums font-semibold text-[#0A2540]">
      {value}
    </span>
  </div>
);

const collectExpandableKeys = (nodes, prefix = "") => {
  let keys = [];
  nodes.forEach((node, i) => {
    const key = prefix ? `${prefix}-${i}` : `${i}`;
    if (node.children && node.children.length > 0) {
      keys.push(key);
      keys = keys.concat(collectExpandableKeys(node.children, key));
    }
  });
  return keys;
};

const flattenVisibleTree = (nodes, expandedKeys, depth = 0, prefix = "") => {
  let out = [];
  nodes.forEach((node, i) => {
    const path = prefix ? `${prefix}-${i}` : `${i}`;
    const hasChildren = node.children && node.children.length > 0;
    out.push({ node, path, depth, hasChildren });
    if (hasChildren && expandedKeys.has(path)) {
      out = out.concat(flattenVisibleTree(node.children, expandedKeys, depth + 1, path));
    }
  });
  return out;
};

function App() {
  const [bomId, setBomId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [lastSynced, setLastSynced] = useState(null);
  const [connection, setConnection] = useState({ connected: null, message: "Checking connection..." });
  const [expandedKeys, setExpandedKeys] = useState(new Set());

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

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!bomId.trim()) return;

    setLoading(true);
    setError(null);
    setResult(null);
    setExpandedKeys(new Set());

    try {
      const response = await axios.get(`${API}/bom/search`, { params: { bom_id: bomId.trim() } });
      setResult(response.data);
      setLastSynced(new Date());
      toast.success(`BOM ${response.data.bom_id} loaded`, {
        description: `${response.data.total_components} components across ${response.data.max_level} levels`,
      });
    } catch (err) {
      const detail = err?.response?.data?.detail || "Failed to fetch BOM from SAP";
      setError(detail);
      toast.error("BOM lookup failed", { description: detail });
    } finally {
      setLoading(false);
    }
  };

  const activeCount = result ? result.total_components : 0;

  return (
    <div className="min-h-screen bg-[#F8F9FA] text-[#0A2540]">
      <Toaster position="top-right" />

      <header className="border-b border-border/40 bg-white">
        <div className="max-w-6xl mx-auto px-8 py-6 flex items-center justify-between gap-6">
          <div>
            <h1 className="font-heading text-4xl font-bold tracking-tight" data-testid="app-title">
              SAP BOM Lookup
            </h1>
            <p className="font-data text-sm text-[#0A2540]/60 mt-1">
              Business ByDesign · Production Bill of Material
            </p>
          </div>

          <div
            className="flex items-center gap-2 border border-border/40 px-3 py-2 rounded-full shrink-0"
            data-testid="connection-status-indicator"
          >
            {connection.connected === null ? (
              <Circle size={10} weight="fill" className="text-[#D97706] animate-pulse" />
            ) : connection.connected ? (
              <Circle size={10} weight="fill" className="text-[#16A34A] animate-pulse" />
            ) : (
              <Circle size={10} weight="fill" className="text-[#DC2626]" />
            )}
            <span className="font-data text-xs whitespace-nowrap">
              {connection.connected === null
                ? "Checking SAP..."
                : connection.connected
                ? "SAP Connected"
                : "SAP Disconnected"}
            </span>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-8 py-10">
        <form onSubmit={handleSearch} className="flex items-center gap-3 mb-10">
          <div className="relative flex-1 max-w-md">
            <MagnifyingGlass
              size={18}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-[#0A2540]/40"
            />
            <Input
              value={bomId}
              onChange={(e) => setBomId(e.target.value)}
              placeholder="Enter Part/BOM ID e.g. P26584 or FLT2_4.1"
              className="pl-10 font-data border-[#0A2540]/20 focus-visible:ring-[#0052FF] focus-visible:ring-2"
              data-testid="bom-id-search-input"
            />
          </div>
          <Button
            type="submit"
            disabled={loading || !bomId.trim()}
            className="bg-[#0052FF] hover:bg-[#0040CC] text-white rounded-full px-6 font-heading font-medium transition-colors"
            data-testid="bom-search-submit-button"
          >
            {loading ? "Searching..." : "Pull BOM"}
          </Button>
        </form>

        {result && (
          <div className="mb-6 -mt-4">
            <Badge
              variant="outline"
              className="bg-[#0052FF]/5 text-[#0052FF] border-[#0052FF]/30 font-data"
              data-testid="resolved-bom-id-badge"
            >
              Resolved to: {result.bom_id}
            </Badge>
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
          <StatCard
            icon={Stack}
            label="Max Level"
            value={result ? result.max_level : "—"}
            testId="stat-total-groups"
          />
          <StatCard
            icon={Package}
            label="Total Components"
            value={result ? result.total_components : "—"}
            testId="stat-total-components"
          />
          <StatCard
            icon={CheckSquare}
            label="Active Materials"
            value={result ? activeCount : "—"}
            testId="stat-active-materials"
          />
          <StatCard
            icon={ClockCounterClockwise}
            label="Last Synced"
            value={lastSynced ? lastSynced.toLocaleTimeString() : "—"}
            testId="stat-last-synced"
          />
        </div>

        {error && (
          <Alert
            variant="destructive"
            className="mb-8 border-[#DC2626]/40 bg-[#DC2626]/5"
            data-testid="bom-search-error-alert"
          >
            <WarningCircle size={18} />
            <AlertTitle className="font-heading">Lookup failed</AlertTitle>
            <AlertDescription className="font-data text-sm">{error}</AlertDescription>
          </Alert>
        )}

        {loading && (
          <div className="space-y-2" data-testid="bom-loading-skeleton">
            {[...Array(6)].map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        )}

        {!loading && result && (
          <div className="border border-border/40 bg-white" data-testid="bom-results-table-container">
            <div className="flex items-center justify-between border-b border-border/40 px-4 py-3">
              <span className="font-heading text-xs uppercase tracking-wide text-[#0A2540]/60">
                Drill down or expand the full tree
              </span>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={expandAll}
                  className="font-heading text-xs rounded-full border-[#0A2540]/20"
                  data-testid="expand-all-button"
                >
                  <ArrowsOutSimple size={14} className="mr-1.5" />
                  Expand All
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={collapseAll}
                  className="font-heading text-xs rounded-full border-[#0A2540]/20"
                  data-testid="collapse-all-button"
                >
                  <ArrowsInSimple size={14} className="mr-1.5" />
                  Collapse All
                </Button>
              </div>
            </div>
            <Table>
              <TableHeader>
                <TableRow className="border-border/40">
                  <TableHead className="font-heading text-xs uppercase tracking-wide">Level</TableHead>
                  <TableHead className="font-heading text-xs uppercase tracking-wide">Product ID</TableHead>
                  <TableHead className="font-heading text-xs uppercase tracking-wide">Description</TableHead>
                  <TableHead className="font-heading text-xs uppercase tracking-wide">Quantity</TableHead>
                  <TableHead className="font-heading text-xs uppercase tracking-wide">UOM</TableHead>
                  <TableHead className="font-heading text-xs uppercase tracking-wide">ECO</TableHead>
                  <TableHead className="font-heading text-xs uppercase tracking-wide">Active</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {result &&
                  flattenVisibleTree(result.tree, expandedKeys).map(({ node, path, depth, hasChildren }) => (
                    <TableRow
                      key={path}
                      className="border-border/40 hover:bg-[#F8F9FA] transition-colors"
                      data-testid={`bom-row-${path}`}
                    >
                      <TableCell className="font-data text-sm tabular-nums py-2 px-3">{node.level}</TableCell>
                      <TableCell
                        className="font-data text-sm tabular-nums py-2 px-3"
                        style={{ paddingLeft: `${depth * 20 + 12}px` }}
                      >
                        <span className="inline-flex items-center gap-1.5">
                          {hasChildren ? (
                            <button
                              type="button"
                              onClick={() => toggleKey(path)}
                              className="text-[#0052FF] hover:text-[#0040CC] transition-colors"
                              data-testid={`bom-toggle-${path}`}
                            >
                              {expandedKeys.has(path) ? (
                                <CaretDown size={12} weight="bold" />
                              ) : (
                                <CaretRight size={12} weight="bold" />
                              )}
                            </button>
                          ) : (
                            <span className="w-3" />
                          )}
                          {node.product_id}
                        </span>
                      </TableCell>
                      <TableCell className="font-data text-sm py-2 px-3">{node.description || "—"}</TableCell>
                      <TableCell className="font-data text-sm tabular-nums py-2 px-3">{node.quantity ?? "—"}</TableCell>
                      <TableCell className="font-data text-sm py-2 px-3">{node.unit_of_measure || "—"}</TableCell>
                      <TableCell className="font-data text-sm py-2 px-3">{node.eco_id || "—"}</TableCell>
                      <TableCell className="py-2 px-3">
                        <Badge
                          className={
                            node.active
                              ? "bg-[#16A34A]/10 text-[#16A34A] border-[#16A34A]/30"
                              : "bg-[#DC2626]/10 text-[#DC2626] border-[#DC2626]/30"
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
                      </TableCell>
                    </TableRow>
                  ))}
                {result && result.total_components === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center py-8 font-data text-sm text-[#0A2540]/50">
                      No components found for this BOM
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        )}

        {!loading && !result && !error && (
          <div
            className="border border-dashed border-border/40 py-16 flex flex-col items-center gap-3 text-[#0A2540]/40"
            data-testid="bom-empty-state"
          >
            <Package size={32} weight="regular" />
            <p className="font-data text-sm">Enter a BOM ID above and click "Pull BOM" to fetch data from SAP</p>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
