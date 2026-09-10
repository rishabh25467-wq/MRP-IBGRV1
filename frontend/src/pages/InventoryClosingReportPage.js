import { useState } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import { toast } from "sonner";
import { ArrowsClockwise, DownloadSimple, Package } from "@phosphor-icons/react";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_MS = 20 * 60 * 1000;

const fmtNum = (n) => Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });

export default function InventoryClosingReportPage() {
  const [keyDate, setKeyDate] = useState("2026-08-31");
  const [rows, setRows] = useState([]);
  const [totalQty, setTotalQty] = useState(0);
  const [totalValue, setTotalValue] = useState(0);
  const [generatedAt, setGeneratedAt] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [error, setError] = useState(null);

  const generateReport = async () => {
    if (!keyDate) {
      toast.error("Pick a date first");
      return;
    }
    setGenerating(true);
    setError(null);
    setElapsedSeconds(0);
    const startedAt = Date.now();
    try {
      const { data } = await axios.post(`${API}/admin/inventory-closing-report/generate`, { key_date: keyDate });
      const jobId = data.job_id;

      // SAP's own historical balance report takes 60-90s PER SITE even
      // queried in parallel - poll a status endpoint instead of holding
      // one HTTP request open.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        setElapsedSeconds(Math.round((Date.now() - startedAt) / 1000));
        const { data: job } = await axios.get(`${API}/admin/inventory-closing-report/generate/${jobId}`);
        if (job.status === "done") {
          setRows(job.result.rows);
          setTotalQty(job.result.total_qty);
          setTotalValue(job.result.total_value);
          setGeneratedAt(job.result.generated_at);
          toast.success("Closing inventory report generated", { description: `${job.result.rows.length} site/item rows as of ${keyDate}` });
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
      toast.error("Closing inventory report failed", { description: detail });
    } finally {
      setGenerating(false);
    }
  };

  const exportExcel = () => {
    const headerRows = [
      [`Closing Inventory as of: ${keyDate}`, "", "", "", "", "", "Generated On:", generatedAt ? new Date(generatedAt).toLocaleString() : ""],
      [],
      ["Plant/Site", "Item Code", "Item Name", "Qty", "UOM", "Value", "Currency"],
    ];
    const dataRows = rows.map((r) => [r.site_name, r.product_id, r.description, r.qty, r.uom, r.value, r.currency]);
    const totalRow = ["Total", "", "", totalQty, "", totalValue, ""];
    const sheet = XLSX.utils.aoa_to_sheet([...headerRows, ...dataRows, totalRow]);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, sheet, "Closing Inventory");
    XLSX.writeFile(workbook, `closing_inventory_${keyDate}.xlsx`);
  };

  return (
    <div className="min-h-screen bg-[#F7F8FA]">
      <div className="bg-[#0E7C86] px-4 sm:px-6 py-3 flex items-center gap-3">
        <Package size={22} weight="bold" className="text-white shrink-0" />
        <span className="text-white font-heading font-bold text-base shrink-0">Materials Hub</span>
        <NavTabs />
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </div>

      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 py-6">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
          <div>
            <h1 className="text-2xl font-bold text-[#1D2939] font-heading" data-testid="closing-inventory-title">
              Closing Inventory Report
            </h1>
            <p className="text-sm text-[#667085] font-sans mt-0.5">
              Historical "as of" closing stock straight from SAP - Plant/Site, Item Code, Item Name, Qty, Value.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Input
              type="date"
              value={keyDate}
              onChange={(e) => setKeyDate(e.target.value)}
              className="h-9 w-40"
              data-testid="closing-inventory-date-input"
            />
            <Button
              variant="outline"
              className="gap-2"
              onClick={exportExcel}
              disabled={rows.length === 0}
              data-testid="closing-inventory-export-button"
            >
              <DownloadSimple size={15} weight="bold" />
              Export Excel
            </Button>
            <Button
              className="gap-2 bg-[#0E7C86] hover:bg-[#0B6B74]"
              onClick={generateReport}
              disabled={generating}
              data-testid="closing-inventory-generate-button"
            >
              <ArrowsClockwise size={15} weight="bold" className={generating ? "animate-spin" : ""} />
              {generating ? `Fetching from SAP... (${elapsedSeconds}s)` : "Fetch from SAP"}
            </Button>
          </div>
        </div>

        {error && (
          <div className="bg-[#FEF3F2] border border-[#FDA29B] text-[#B42318] text-sm rounded-md px-4 py-3 mb-4" data-testid="closing-inventory-error">
            {error}
          </div>
        )}

        <div className="bg-white border border-[#D0D5DD] rounded-md overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[#EAECF0] text-left text-xs text-[#667085] font-sans">
                <th className="px-3 py-2 font-medium">Plant/Site</th>
                <th className="px-3 py-2 font-medium">Item Code</th>
                <th className="px-3 py-2 font-medium">Item Name</th>
                <th className="px-3 py-2 font-medium text-right">Qty</th>
                <th className="px-3 py-2 font-medium">UOM</th>
                <th className="px-3 py-2 font-medium text-right">Value</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-[#667085] text-sm" data-testid="closing-inventory-empty">
                    {generating ? "Pulling site by site from SAP - this can take a few minutes..." : 'Pick a date and click "Fetch from SAP".'}
                  </td>
                </tr>
              )}
              {rows.slice(0, 2000).map((r, idx) => (
                <tr key={`${r.site_id}-${r.product_id}-${idx}`} className="border-b border-[#F2F4F7] hover:bg-slate-50" data-testid={`closing-inventory-row-${idx}`}>
                  <td className="px-3 py-2 text-[#475467]">{r.site_name}</td>
                  <td className="px-3 py-2 font-mono text-xs font-semibold text-[#1D2939]">{r.product_id}</td>
                  <td className="px-3 py-2 text-[#475467]">{r.description || "\u2014"}</td>
                  <td className="px-3 py-2 text-right text-[#1D2939]">{fmtNum(r.qty)}</td>
                  <td className="px-3 py-2 text-[#475467]">{r.uom || "\u2014"}</td>
                  <td className="px-3 py-2 text-right text-[#1D2939]">{r.currency} {fmtNum(r.value)}</td>
                </tr>
              ))}
            </tbody>
            {rows.length > 0 && (
              <tfoot>
                <tr className="border-t-2 border-[#1D2939] font-bold bg-[#F2F4F7]" data-testid="closing-inventory-total-row">
                  <td className="px-3 py-2" colSpan={3}>Total</td>
                  <td className="px-3 py-2 text-right">{fmtNum(totalQty)}</td>
                  <td className="px-3 py-2"></td>
                  <td className="px-3 py-2 text-right">{fmtNum(totalValue)}</td>
                </tr>
              </tfoot>
            )}
          </table>
          {rows.length > 2000 && (
            <div className="px-3 py-2 text-xs text-[#667085] border-t border-[#EAECF0]">
              Showing first 2,000 of {rows.length.toLocaleString()} rows - export to Excel for the full set.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
