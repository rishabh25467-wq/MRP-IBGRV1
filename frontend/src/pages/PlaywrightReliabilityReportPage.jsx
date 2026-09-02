import { useState, useEffect } from "react";
import axios from "axios";
import { ChartBar, ArrowsClockwise, WarningCircle } from "@phosphor-icons/react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const KIND_LABELS = {
  supplier_grn: "Supplier GRN (vendor deliveries)",
  inbound_receipt: "Inbound STO Receipt",
  submit_sto: "STO Outbound Creation",
};

// Sep 2 2026 (user's explicit ask: "not need to be visible to users, but
// something that can be discussed between you and me") - reachable ONLY
// by direct URL, deliberately not linked from NavTabs or any menu.
export default function PlaywrightReliabilityReportPage() {
  const [days, setDays] = useState(7);
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async (d) => {
    setLoading(true);
    setError("");
    try {
      const { data } = await axios.get(`${API}/admin/playwright-reliability-report`, { params: { days: d } });
      setReport(data);
    } catch (err) {
      setError(err?.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(days);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  return (
    <div className="min-h-screen bg-[#0B0F14] text-[#D7DCE1] font-mono p-6" data-testid="playwright-reliability-report-page">
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-2">
            <ChartBar size={18} weight="bold" className="text-[#5EE6A0]" />
            <h1 className="text-lg font-bold text-white">Playwright Reliability Report</h1>
          </div>
          <button
            onClick={() => load(days)}
            className="flex items-center gap-1 text-xs text-[#5EE6A0] hover:text-white transition-colors"
            data-testid="playwright-report-refresh-button"
          >
            <ArrowsClockwise size={13} /> Refresh
          </button>
        </div>
        <p className="text-xs text-[#7A8794] mb-5">
          Internal only - not linked anywhere in the app. Job history is TTL-purged after 7 days, so this can never show a longer window.
        </p>

        <div className="flex items-center gap-2 mb-5">
          {[1, 3, 7].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              data-testid={`playwright-report-range-${d}`}
              className={`px-3 py-1 rounded-sm text-xs border transition-colors ${days === d ? "bg-[#5EE6A0]/15 border-[#5EE6A0] text-[#5EE6A0]" : "border-[#2A323C] text-[#7A8794] hover:border-[#5EE6A0]/50"}`}
            >
              Last {d === 1 ? "24h" : `${d}d`}
            </button>
          ))}
        </div>

        {loading && <p className="text-sm text-[#7A8794]" data-testid="playwright-report-loading">Loading...</p>}
        {error && (
          <div className="flex items-center gap-2 text-sm text-[#F97066] bg-[#F97066]/10 border border-[#F97066]/30 rounded-sm px-3 py-2" data-testid="playwright-report-error">
            <WarningCircle size={16} weight="fill" /> {error}
          </div>
        )}

        {!loading && !error && report && (
          <div className="space-y-4" data-testid="playwright-report-body">
            <p className="text-[11px] text-[#7A8794]">Since {new Date(report.since).toLocaleString()}</p>
            {Object.entries(report.by_kind).map(([kind, stats]) => (
              <div key={kind} className="bg-[#111720] border border-[#2A323C] rounded-sm p-4" data-testid={`playwright-report-kind-${kind}`}>
                <div className="flex items-baseline justify-between mb-2">
                  <h2 className="text-sm font-bold text-white">{KIND_LABELS[kind] || kind}</h2>
                  <span
                    className={`text-xl font-bold ${stats.real_success_rate_pct === null ? "text-[#7A8794]" : stats.real_success_rate_pct >= 95 ? "text-[#5EE6A0]" : stats.real_success_rate_pct >= 85 ? "text-[#F0C674]" : "text-[#F97066]"}`}
                    data-testid={`playwright-report-success-rate-${kind}`}
                  >
                    {stats.real_success_rate_pct === null ? "-" : `${stats.real_success_rate_pct}%`}
                  </span>
                </div>
                <div className="grid grid-cols-4 gap-3 text-xs mb-3">
                  <div><div className="text-[#7A8794]">Total Jobs</div><div className="text-white font-semibold">{stats.total_jobs}</div></div>
                  <div><div className="text-[#7A8794]">Done</div><div className="text-[#5EE6A0] font-semibold">{stats.done}</div></div>
                  <div><div className="text-[#7A8794]">Real Failures</div><div className="text-[#F97066] font-semibold">{stats.real_failures}</div></div>
                  <div><div className="text-[#7A8794]">Restart Noise (excluded)</div><div className="text-[#7A8794] font-semibold">{stats.restart_interrupted}</div></div>
                </div>
                {stats.real_failure_reasons.length > 0 && (
                  <div className="border-t border-[#2A323C] pt-2 mt-2">
                    <div className="text-[10px] text-[#7A8794] uppercase mb-1">Real failure reasons</div>
                    {stats.real_failure_reasons.map((r, i) => (
                      <div key={i} className="text-[11px] text-[#D7DCE1] flex justify-between gap-2 py-0.5" data-testid={`playwright-report-reason-${kind}-${i}`}>
                        <span className="truncate">{r.error}</span>
                        <span className="text-[#F97066] shrink-0">x{r.count}</span>
                      </div>
                    ))}
                  </div>
                )}
                {stats.total_jobs === 0 && <p className="text-[11px] text-[#7A8794]">No jobs in this window.</p>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
