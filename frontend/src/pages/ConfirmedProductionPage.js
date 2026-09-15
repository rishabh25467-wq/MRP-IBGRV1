import { useState, useEffect, useCallback, useMemo } from "react";
import * as XLSX from "xlsx";
import "@/App.css";
import axios from "axios";
import { Shield, ArrowClockwise, DownloadSimple, WarningCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const formatQty = (v) => (v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const formatUnit = (u) => (u === "MASS" ? "KG" : u || "");
const formatAt = (iso) => (iso ? new Date(iso).toLocaleString("en-IN") : "—");
// Sep 15 2026, user's explicit ask: show Operation ID + description
// together WITH the Reporting Point ID, same combined format already
// used on the operational page's own "Reporting Point" column (e.g.
// "OP_10 - BLANK (RP_10)") instead of picking only one of the two.
const formatReportingPoint = (r) => {
  if (!r.reporting_point_description) return r.reporting_point_id || "—";
  return r.reporting_point_id ? `${r.reporting_point_description} (${r.reporting_point_id})` : r.reporting_point_description;
};

// Sep 15 2026, user's explicit ask - the reporting counterpart of the
// now purely operational Production Confirmation page: transaction-level
// history from production_confirmation_history (success=True only),
// with its own Date/Output Product/Site/By-product/WIP filters and
// Excel export. Reuses the existing "production_confirmation" access
// right - no new grantable permission was introduced for this page.
export default function ConfirmedProductionPage() {
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [siteFilter, setSiteFilter] = useState("all");
  const [byproductFilter, setByproductFilter] = useState("all");
  const [wipFilter, setWipFilter] = useState("all");
  const [outputProductFilter, setOutputProductFilter] = useState("");
  const [reportingPointFilter, setReportingPointFilter] = useState("");
  const [knownSites, setKnownSites] = useState([]);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    axios.get(`${API}/store-requests/known-sites`).then(({ data }) => setKnownSites(data.sites || [])).catch(() => {});
  }, []);

  const loadReport = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const { data } = await axios.get(`${API}/production-confirmation/confirmed-report`, {
        params: {
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
          site_ids: siteFilter !== "all" ? siteFilter : undefined,
          byproduct: byproductFilter,
          wip: wipFilter,
        },
      });
      setRows(data.rows);
    } catch (e) {
      setLoadError(e.response?.data?.detail || "Failed to load Confirmed Production report");
    } finally {
      setLoading(false);
    }
  }, [dateFrom, dateTo, siteFilter, byproductFilter, wipFilter]);

  useEffect(() => { loadReport(); }, [loadReport]);

  const visibleRows = useMemo(() => {
    let out = rows;
    if (outputProductFilter.trim()) {
      const q = outputProductFilter.trim().toLowerCase();
      out = out.filter((r) => (r.main_output_product || "").toLowerCase().includes(q));
    }
    if (reportingPointFilter.trim()) {
      const q = reportingPointFilter.trim().toLowerCase();
      out = out.filter((r) => (r.reporting_point_id || "").toLowerCase().includes(q) || (r.reporting_point_description || "").toLowerCase().includes(q));
    }
    return out;
  }, [rows, outputProductFilter, reportingPointFilter]);

  const clearFilters = () => {
    setDateFrom("");
    setDateTo("");
    setSiteFilter("all");
    setByproductFilter("all");
    setWipFilter("all");
    setOutputProductFilter("");
    setReportingPointFilter("");
  };

  const filtersActive = dateFrom || dateTo || siteFilter !== "all" || byproductFilter !== "all" || wipFilter !== "all" || outputProductFilter.trim() || reportingPointFilter.trim();

  const exportToExcel = () => {
    const header = ["Date/Time", "Lot ID", "Output Product", "Description", "Reporting Point", "Model ID", "Site", "Confirmed Qty", "UOM", "Scrap", "By-product Qty", "By-product Unit", "WIP Clearing", "Confirmed By"];
    const dataRows = visibleRows.map((r) => {
      const wipLabel = !r.wip_clearing ? "—" : r.wip_clearing.skipped ? "Pending" : r.wip_clearing.success ? "Posted" : "Error";
      return [
        formatAt(r.at), r.production_lot_id, r.main_output_product || "—", r.main_output_product_description || "—",
        formatReportingPoint(r), r.production_model_id || "—", r.site_id || "—",
        r.confirmed_quantity, formatUnit(r.uom || undefined) || "—", r.confirmed_scrap ?? 0,
        r.byproduct_confirmed_quantity ?? "—", formatUnit(r.byproduct_unit_code) || "—", wipLabel, r.actor || "—",
      ];
    });
    const sheet = XLSX.utils.aoa_to_sheet([header, ...dataRows]);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, sheet, "Confirmed Production");
    XLSX.writeFile(workbook, `confirmed_production_${new Date().toISOString().slice(0, 10)}.xlsx`);
  };

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4 flex-wrap">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Confirmed Production</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4">
        <div>
          <h1 className="font-heading text-lg font-bold text-[#101828]" data-testid="confirmed-production-title">Confirmed Production</h1>
          <p className="text-sm text-[#667085] font-sans mt-0.5">Transaction-level history of every successful production confirmation posted to SAP.</p>
        </div>

        {loadError && (
          <Alert className="bg-[#FEF3F2] border-[#FECDCA]" data-testid="load-error-banner">
            <WarningCircle size={16} className="text-[#B42318]" />
            <AlertDescription className="text-[#B42318] text-sm">{loadError}</AlertDescription>
          </Alert>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-heading font-bold uppercase tracking-wide text-[#667085]">Date:</span>
          <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-36 bg-white h-9 text-xs" data-testid="report-date-from-input" />
          <span className="text-[#98A2B3] text-xs">to</span>
          <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-36 bg-white h-9 text-xs" data-testid="report-date-to-input" />
          <Select value={siteFilter} onValueChange={setSiteFilter}>
            <SelectTrigger className="w-40 bg-white h-9 text-xs" data-testid="report-site-filter-select"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Sites</SelectItem>
              {knownSites.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
            </SelectContent>
          </Select>
          <Input
            placeholder="Filter by Output Product..."
            value={outputProductFilter}
            onChange={(e) => setOutputProductFilter(e.target.value)}
            className="w-52 bg-white h-9 text-xs"
            data-testid="report-output-product-filter-input"
          />
          <Input
            placeholder="Filter by Reporting Point..."
            value={reportingPointFilter}
            onChange={(e) => setReportingPointFilter(e.target.value)}
            className="w-52 bg-white h-9 text-xs"
            data-testid="report-reporting-point-filter-input"
          />
          <Select value={byproductFilter} onValueChange={setByproductFilter}>
            <SelectTrigger className="w-44 bg-white h-9 text-xs" data-testid="report-byproduct-filter-select"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">By-product: All</SelectItem>
              <SelectItem value="yes">By-product: Yes</SelectItem>
              <SelectItem value="no">By-product: No</SelectItem>
            </SelectContent>
          </Select>
          <Select value={wipFilter} onValueChange={setWipFilter}>
            <SelectTrigger className="w-36 bg-white h-9 text-xs" data-testid="report-wip-filter-select"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">WIP: All</SelectItem>
              <SelectItem value="wip">WIP: Cleared</SelectItem>
              <SelectItem value="non_wip">WIP: Not Cleared</SelectItem>
            </SelectContent>
          </Select>
          {filtersActive && (
            <Button variant="outline" size="sm" className="h-9 text-xs" onClick={clearFilters} data-testid="report-clear-filters-button">
              Clear Filters
            </Button>
          )}
          <Button variant="outline" size="sm" className="h-9 text-xs" onClick={loadReport} data-testid="report-refresh-button">
            <ArrowClockwise size={14} className="mr-1.5" /> Refresh
          </Button>
          <div className="flex-1" />
          <Button variant="outline" size="sm" className="h-9 text-xs" onClick={exportToExcel} disabled={visibleRows.length === 0} data-testid="report-export-excel-button">
            <DownloadSimple size={14} className="mr-1.5" /> Export Excel
          </Button>
        </div>

        {loading ? (
          <div className="space-y-2">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full rounded-sm" />)}</div>
        ) : (
          <div className="border border-[#D0D5DD] rounded-sm overflow-auto bg-white">
            <table className="w-full text-[13px] border-collapse" data-testid="confirmed-production-table">
              <thead>
                <tr>
                  {["Date/Time", "Lot ID", "Output Product", "Description", "Reporting Point", "Model ID", "Site", "Confirmed Qty", "UOM", "Scrap", "By-product Qty", "By-product Unit", "WIP Clearing", "Confirmed By"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((r, i) => (
                  <tr key={`${r.production_lot_id}-${r.at}-${i}`} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`confirmed-report-row-${i}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 whitespace-nowrap text-[#475467]">{formatAt(r.at)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium text-[#101828]">{r.production_lot_id}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">{r.main_output_product || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.main_output_product_description || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{formatReportingPoint(r)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.production_model_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.site_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.confirmed_quantity)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{formatUnit(r.uom) || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(r.confirmed_scrap)}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{r.byproduct_confirmed_quantity != null ? formatQty(r.byproduct_confirmed_quantity) : "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{formatUnit(r.byproduct_unit_code) || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      {!r.wip_clearing ? "—" : r.wip_clearing.skipped ? (
                        <Badge variant="outline" className="bg-[#F9FAFB] text-[#667085] border-[#EAECF0]">Pending</Badge>
                      ) : r.wip_clearing.success ? (
                        <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]">Cleared</Badge>
                      ) : (
                        <Badge variant="outline" className="bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]">Error</Badge>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#475467]">{r.actor || "—"}</td>
                  </tr>
                ))}
                {visibleRows.length === 0 && (
                  <tr><td colSpan={14} className="text-center py-8 text-[#98A2B3] border border-[#D0D5DD]" data-testid="confirmed-report-empty-state">No confirmed production transactions match these filters.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
