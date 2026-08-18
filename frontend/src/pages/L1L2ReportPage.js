import { useState, useEffect, useMemo } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import { toast } from "sonner";
import { ArrowsClockwise, DownloadSimple, MagnifyingGlass, Stack } from "@phosphor-icons/react";
import { NavTabs } from "@/components/NavTabs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_MS = 20 * 60 * 1000;

const StatCard = ({ label, value, testId }) => (
  <div className="bg-white border border-[#D0D5DD] rounded-md px-4 py-3" data-testid={testId}>
    <div className="text-xs text-[#667085] font-sans">{label}</div>
    <div className="text-xl font-bold text-[#1D2939] font-heading mt-0.5">{value}</div>
  </div>
);

export default function L1L2ReportPage() {
  const [items, setItems] = useState([]);
  const [rootsScanned, setRootsScanned] = useState(0);
  const [rootsWithBom, setRootsWithBom] = useState(0);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState("all");
  const [showAllItems, setShowAllItems] = useState(false);

  const PACKAGING_KEYWORDS = /POLYBAG|POLYTHENE|LAMINATED/i;

  const loadCached = async () => {
    setLoading(true);
    setError(null);
    try {
      const { data } = await axios.get(`${API}/admin/l1-l2-report`);
      setItems(data.items);
      setRootsScanned(data.roots_scanned);
      setRootsWithBom(data.roots_with_bom);
      setUpdatedAt(data.updated_at);
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || "Failed to load report");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCached();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const generateReport = async () => {
    setGenerating(true);
    setError(null);
    setElapsedSeconds(0);
    const startedAt = Date.now();
    try {
      const { data } = await axios.post(`${API}/admin/l1-l2-report/generate`);
      const jobId = data.job_id;

      // Full live sweep across the whole catalog can take several minutes -
      // poll a status endpoint instead of holding one HTTP request open.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        setElapsedSeconds(Math.round((Date.now() - startedAt) / 1000));
        const { data: job } = await axios.get(`${API}/admin/l1-l2-report/generate/${jobId}`);
        if (job.status === "done") {
          setItems(job.result.items);
          setRootsScanned(job.result.roots_scanned);
          setRootsWithBom(job.result.roots_with_bom);
          setUpdatedAt(job.result.updated_at);
          toast.success("L1/L2 report generated", {
            description: `${job.result.roots_with_bom} of ${job.result.roots_scanned} materials have a BOM - ${job.result.items.length} line items`,
          });
          break;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "Failed to generate report");
        }
        if (Date.now() - startedAt > MAX_POLL_MS) {
          throw new Error("Report generation is taking too long. Please try again.");
        }
      }
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Failed to generate report";
      setError(detail);
      toast.error("L1/L2 report generation failed", { description: detail });
    } finally {
      setGenerating(false);
    }
  };

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((it) => {
      const matchesLevel = levelFilter === "all" || String(it.level) === levelFilter;
      const matchesSearch =
        !q ||
        it.product_id.toLowerCase().includes(q) ||
        it.root_product_id.toLowerCase().includes(q) ||
        (it.description || "").toLowerCase().includes(q);
      // Default view: only weight-based (kg) input items, excluding
      // packaging (polybag/polythene/laminated film) even though it's
      // also measured in kg - user-confirmed rule, "Show All Items"
      // toggle bypasses this to see the unfiltered set.
      const matchesWeightFilter = showAllItems || (it.unit_of_measure === "kg" && !PACKAGING_KEYWORDS.test(it.description || ""));
      return matchesLevel && matchesSearch && matchesWeightFilter;
    });
  }, [items, search, levelFilter, showAllItems]);

  const exportExcel = () => {
    const headerRows = [
      [`Last Updated On: ${updatedAt ? new Date(updatedAt).toLocaleString() : ""}`, "", "", "", "", "", "Timezone:", "INDIA"],
      [],
      ["Product ID", "Product Description", "Product Specification ID", "Quantity", "Quantity (Unit)", "Fixed Quantity", "Line Item Group ID", "Line Item ID"],
    ];
    const dataRows = filtered.map((it) => [
      it.product_id,
      it.description || "",
      "",
      it.quantity ?? "",
      it.unit_of_measure || "",
      "No",
      it.line_item_group_id || "",
      it.line_item_id || "",
    ]);
    const sheet = XLSX.utils.aoa_to_sheet([...headerRows, ...dataRows]);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, sheet, "L1-L2 Items");
    XLSX.writeFile(workbook, `l1_l2_item_report_${new Date().toISOString().slice(0, 10)}.xlsx`);
  };

  return (
    <div className="min-h-screen bg-[#F7F8FA]">
      <div className="bg-[#0E7C86] px-4 sm:px-6 py-3 flex items-center gap-3">
        <Stack size={22} weight="bold" className="text-white shrink-0" />
        <span className="text-white font-heading font-bold text-base shrink-0">Materials Hub</span>
        <NavTabs />
      </div>

      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 py-6">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
          <div>
            <h1 className="text-2xl font-bold text-[#1D2939] font-heading" data-testid="l1l2-report-title">
              L1/L2 Item Report
            </h1>
            <p className="text-sm text-[#667085] font-sans mt-0.5">
              Level 1 and Level 2 BOM line items across every material in SAP - item, description, and quantity/weight.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button
              variant="outline"
              className="gap-2"
              onClick={exportExcel}
              disabled={filtered.length === 0}
              data-testid="l1l2-report-export-button"
            >
              <DownloadSimple size={15} weight="bold" />
              Export Excel
            </Button>
            <Button
              className="gap-2 bg-[#0E7C86] hover:bg-[#0B6B74]"
              onClick={generateReport}
              disabled={generating}
              data-testid="l1l2-report-generate-button"
            >
              <ArrowsClockwise size={15} weight="bold" className={generating ? "animate-spin" : ""} />
              {generating ? `Generating... (${elapsedSeconds}s)` : "Generate Fresh Report"}
            </Button>
          </div>
        </div>

        {error && (
          <div className="bg-[#FEF3F2] border border-[#FDA29B] text-[#B42318] text-sm rounded-md px-4 py-3 mb-4" data-testid="l1l2-report-error">
            {error}
          </div>
        )}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
          <StatCard label="Materials Scanned" value={rootsScanned.toLocaleString()} testId="l1l2-report-stat-scanned" />
          <StatCard label="Materials With a BOM" value={rootsWithBom.toLocaleString()} testId="l1l2-report-stat-with-bom" />
          <StatCard label="L1/L2 Line Items" value={items.length.toLocaleString()} testId="l1l2-report-stat-line-items" />
          <StatCard
            label="Last Generated"
            value={updatedAt ? new Date(updatedAt).toLocaleString() : "Never"}
            testId="l1l2-report-stat-updated-at"
          />
        </div>

        <div className="flex flex-wrap items-center gap-2 mb-3">
          <div className="relative flex-1 min-w-[220px] max-w-sm">
            <MagnifyingGlass size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search product ID, root, or description"
              className="pl-9 h-9"
              data-testid="l1l2-report-search-input"
            />
          </div>
          <div className="flex items-center gap-1" data-testid="l1l2-report-level-filter">
            {["all", "1", "2"].map((lvl) => (
              <button
                key={lvl}
                type="button"
                onClick={() => setLevelFilter(lvl)}
                className={`h-9 px-3 rounded-sm text-xs font-medium border transition-colors ${
                  levelFilter === lvl ? "bg-[#0E7C86] text-white border-[#0E7C86]" : "bg-white text-[#475467] border-[#D0D5DD] hover:bg-slate-50"
                }`}
                data-testid={`l1l2-report-level-filter-${lvl}`}
              >
                {lvl === "all" ? "All Levels" : `Level ${lvl}`}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setShowAllItems((v) => !v)}
            className={`h-9 px-3 rounded-sm text-xs font-medium border transition-colors ${
              showAllItems ? "bg-white text-[#475467] border-[#D0D5DD] hover:bg-slate-50" : "bg-[#DC6803] text-white border-[#DC6803]"
            }`}
            data-testid="l1l2-report-weight-filter-toggle"
          >
            {showAllItems ? "Show All Items" : "Weight Items Only (kg, no packaging)"}
          </button>
          <span className="text-xs text-[#475467] ml-auto font-sans" data-testid="l1l2-report-item-count">
            {filtered.length.toLocaleString()} of {items.length.toLocaleString()} rows
          </span>
        </div>

        <div className="bg-white border border-[#D0D5DD] rounded-md overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[#EAECF0] text-left text-xs text-[#667085] font-sans">
                <th className="px-3 py-2 font-medium">Root Product</th>
                <th className="px-3 py-2 font-medium">Level</th>
                <th className="px-3 py-2 font-medium">Parent</th>
                <th className="px-3 py-2 font-medium">Product ID</th>
                <th className="px-3 py-2 font-medium">Description</th>
                <th className="px-3 py-2 font-medium text-right">Quantity</th>
                <th className="px-3 py-2 font-medium">Unit</th>
                <th className="px-3 py-2 font-medium">Line Item Group</th>
                <th className="px-3 py-2 font-medium">Line Item ID</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={9} className="px-3 py-8 text-center text-[#667085] text-sm" data-testid="l1l2-report-loading">
                    Loading...
                  </td>
                </tr>
              )}
              {!loading && filtered.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-3 py-8 text-center text-[#667085] text-sm" data-testid="l1l2-report-empty">
                    {items.length === 0
                      ? 'No report generated yet - click "Generate Fresh Report" to pull L1/L2 items live from SAP.'
                      : "No rows match your filters."}
                  </td>
                </tr>
              )}
              {!loading &&
                filtered.slice(0, 1000).map((it, idx) => (
                  <tr key={`${it.root_product_id}-${it.level}-${it.product_id}-${idx}`} className="border-b border-[#F2F4F7] hover:bg-slate-50" data-testid={`l1l2-report-row-${idx}`}>
                    <td className="px-3 py-2 font-mono text-xs text-[#475467]">{it.root_product_id}</td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold ${it.level === 1 ? "bg-[#EFF8FF] text-[#175CD3]" : "bg-[#F4F3FF] text-[#5925DC]"}`}>
                        L{it.level}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-[#475467]">{it.parent_product_id}</td>
                    <td className="px-3 py-2 font-mono text-xs font-semibold text-[#1D2939]">{it.product_id}</td>
                    <td className="px-3 py-2 text-[#475467]">{it.description || "—"}</td>
                    <td className="px-3 py-2 text-right text-[#1D2939]">{it.quantity ?? "—"}</td>
                    <td className="px-3 py-2 text-[#475467]">{it.unit_of_measure || "—"}</td>
                    <td className="px-3 py-2 text-[#475467]">{it.line_item_group_id || "—"}</td>
                    <td className="px-3 py-2 text-[#475467]">{it.line_item_id || "—"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
          {filtered.length > 1000 && (
            <div className="px-3 py-2 text-xs text-[#667085] border-t border-[#EAECF0]" data-testid="l1l2-report-truncated-notice">
              Showing first 1,000 of {filtered.length.toLocaleString()} matching rows - narrow your search or export to Excel for the full set.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
