import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { Database, Shield, LockSimple, CloudArrowUp, WarningCircle, CheckCircle, Stop, PencilSimple } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

// Client-side passcode gate - this app has no end-user login/auth system at
// all (see /app/memory/test_credentials.md), so this is a lightweight
// "confirm before entering" lock for the SAP write actions, not a real
// authentication system. Session-scoped only (cleared on tab close).
const SAP_WRITE_PASSCODE = "admin";
const SESSION_KEY = "sap_write_unlocked";

const inputCls =
  "h-9 px-3 text-sm rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87] w-full";

export default function SapWritePage() {
  const [unlocked, setUnlocked] = useState(() => sessionStorage.getItem(SESSION_KEY) === "true");
  const [passcodeInput, setPasscodeInput] = useState("");
  const [passcodeError, setPasscodeError] = useState(null);

  const [status, setStatus] = useState("idle"); // idle | running | done | failed
  const [progress, setProgress] = useState({ processed: 0, total: 0 });
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [currentJobId, setCurrentJobId] = useState(null);
  const [stopping, setStopping] = useState(false);

  const [manualForm, setManualForm] = useState({ product_id: "", supplier_internal_id: "", price: "", currency: "INR" });
  const [manualWriting, setManualWriting] = useState(false);
  const [manualError, setManualError] = useState(null);

  const [readProductId, setReadProductId] = useState("");
  const [readSpecs, setReadSpecs] = useState(null);
  const [readLoading, setReadLoading] = useState(false);
  const [readError, setReadError] = useState(null);

  const readFromSap = async (productId) => {
    const pid = productId.trim();
    if (!pid) return;
    setReadLoading(true);
    setReadError(null);
    try {
      const { data } = await axios.get(`${API}/suppliers/sap-price-specs/${pid}`);
      setReadSpecs(data);
    } catch (err) {
      setReadError(err?.response?.data?.detail || err.message || "Failed to read from SAP");
      setReadSpecs(null);
    } finally {
      setReadLoading(false);
    }
  };

  const writeManualPriceSpec = async (e) => {
    e.preventDefault();
    setManualWriting(true);
    setManualError(null);
    try {
      const { data } = await axios.post(`${API}/suppliers/sap-price-specs`, {
        product_id: manualForm.product_id.trim(),
        supplier_internal_id: manualForm.supplier_internal_id.trim(),
        price: parseFloat(manualForm.price),
        currency: manualForm.currency.trim() || "INR",
      });
      toast.success("Price spec written to SAP", {
        description: `${manualForm.product_id} / ${manualForm.supplier_internal_id} @ ${manualForm.price}`,
      });
      setReadProductId(manualForm.product_id.trim());
      setReadSpecs(data);
      setReadError(null);
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Failed to write price spec";
      setManualError(detail);
      toast.error("Write to SAP failed", { description: detail });
    } finally {
      setManualWriting(false);
    }
  };

  const unlock = (e) => {
    e.preventDefault();
    if (passcodeInput === SAP_WRITE_PASSCODE) {
      sessionStorage.setItem(SESSION_KEY, "true");
      setUnlocked(true);
      setPasscodeError(null);
    } else {
      setPasscodeError("Incorrect passcode");
    }
  };

  const startBulkPush = async () => {
    setStatus("running");
    setError(null);
    setResult(null);
    setProgress({ processed: 0, total: 0 });
    try {
      const { data } = await axios.post(`${API}/suppliers/bulk-push-erp-to-sap`);
      const jobId = data.job_id;
      setCurrentJobId(jobId);
      let consecutiveFailures = 0;
      const poll = async () => {
        try {
          const { data: job } = await axios.get(`${API}/suppliers/bulk-push-erp-to-sap/${jobId}`);
          consecutiveFailures = 0;
          if (job.progress) setProgress(job.progress);
          if (job.status === "running") {
            setTimeout(poll, 2000);
          } else if (job.status === "done") {
            setStatus("done");
            setResult(job.result);
            setCurrentJobId(null);
            if (job.result.cancelled) {
              toast.info("Bulk push stopped", {
                description: `${job.result.pushed} pushed before stopping, ${job.result.failed.length} failed`,
              });
            } else {
              toast.success("Bulk push to SAP complete", {
                description: `${job.result.pushed} pushed, ${job.result.failed.length} failed`,
              });
            }
          } else if (job.status === "failed") {
            setStatus("failed");
            setError(job.error || "Bulk push failed");
            setCurrentJobId(null);
            toast.error("Bulk push to SAP failed", { description: job.error });
          }
        } catch (err) {
          consecutiveFailures += 1;
          if (consecutiveFailures >= 5) {
            setStatus("failed");
            setError("Lost connection while checking job progress. Please try again.");
            setCurrentJobId(null);
          } else {
            setTimeout(poll, 2000);
          }
        }
      };
      poll();
    } catch (err) {
      setStatus("failed");
      const detail = err?.response?.data?.detail || err.message || "Failed to start bulk push";
      setError(detail);
      toast.error("Could not start bulk push", { description: detail });
    }
  };

  const stopBulkPush = async () => {
    if (!currentJobId) return;
    setStopping(true);
    try {
      await axios.post(`${API}/suppliers/bulk-push-erp-to-sap/${currentJobId}/stop`);
      toast.info("Stopping...", { description: "Finishing already-started items, no new ones will start." });
    } catch (err) {
      toast.error("Could not stop the push", { description: err?.response?.data?.detail || err.message });
    } finally {
      setStopping(false);
    }
  };

  useEffect(() => {
    document.title = "SAP Write - Admin";
  }, []);

  if (!unlocked) {
    return (
      <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
        <Toaster position="top-right" />
        <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
          <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
            <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
              <Shield size={18} weight="fill" className="text-white" />
            </div>
            <div className="flex flex-col leading-tight">
              <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
              <span className="font-sans text-[12px] text-white/70 hidden sm:inline">SAP Write</span>
            </div>
          </div>
          <div className="w-px h-7 bg-white/25 shrink-0" />
          <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
            <NavTabs />
          </div>
          <SapConnectionStatus />
          <ErpConnectionStatus />
        </header>
        <main className="flex-1 flex items-center justify-center">
          <form
            onSubmit={unlock}
            className="bg-white border border-[#D0D5DD] rounded-sm p-6 w-full max-w-sm flex flex-col items-center gap-3 shadow-[0_1px_3px_0_rgba(16,24,40,0.1)]"
            data-testid="sap-write-passcode-form"
          >
            <div className="w-10 h-10 rounded-full bg-[#FFFAEB] flex items-center justify-center">
              <LockSimple size={18} weight="bold" className="text-[#B54708]" />
            </div>
            <h1 className="font-heading text-sm font-bold text-[#1D2939]">Restricted Area</h1>
            <p className="text-[13px] text-[#475467] text-center">
              This page writes live data into SAP. Enter the admin passcode to continue.
            </p>
            <input
              type="password"
              autoFocus
              placeholder="Passcode"
              value={passcodeInput}
              onChange={(e) => {
                setPasscodeInput(e.target.value);
                setPasscodeError(null);
              }}
              className={inputCls}
              data-testid="sap-write-passcode-input"
            />
            {passcodeError && (
              <p className="text-xs text-[#B42318]" data-testid="sap-write-passcode-error">
                {passcodeError}
              </p>
            )}
            <Button
              type="submit"
              className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm"
              data-testid="sap-write-passcode-submit"
            >
              Unlock
            </Button>
          </form>
        </main>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">SAP Write</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <div className="shrink-0 w-8" />
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4 max-w-3xl mx-auto w-full">
        <div
          className="bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-3 flex items-start gap-2.5"
          data-testid="sap-write-warning-banner"
        >
          <WarningCircle size={16} weight="fill" className="text-[#B54708] mt-0.5 shrink-0" />
          <div className="text-[13px] text-[#7A4504]">
            <span className="font-bold">Careful: </span>
            Actions on this page write directly into the live SAP tenant. Only proceed if you understand what will
            be pushed.
          </div>
        </div>

        <section className="bg-white border border-[#D0D5DD] rounded-sm p-4" data-testid="manual-price-spec-section">
          <div className="flex items-center gap-2 mb-2">
            <PencilSimple size={16} weight="bold" className="text-[#004B87]" />
            <h2 className="font-heading text-sm font-bold text-[#1D2939]">Write Single Price Spec to SAP</h2>
          </div>
          <p className="text-[13px] text-[#475467] mb-3">
            Manually write one Product ID + Supplier + Price into SAP as a Procurement Price Specification (SOAP
            write) - useful for one-off corrections or testing a specific item/supplier combination outside the
            bulk ERP push.
          </p>
          <form onSubmit={writeManualPriceSpec} className="grid grid-cols-2 sm:grid-cols-5 gap-2 items-end">
            <div>
              <label className="text-xs text-[#667085] block mb-1">Product ID</label>
              <input
                required
                value={manualForm.product_id}
                onChange={(e) => setManualForm((f) => ({ ...f, product_id: e.target.value }))}
                className={inputCls}
                data-testid="manual-price-spec-product-id"
              />
            </div>
            <div>
              <label className="text-xs text-[#667085] block mb-1">Supplier Internal ID</label>
              <input
                required
                placeholder="e.g. S1822"
                value={manualForm.supplier_internal_id}
                onChange={(e) => setManualForm((f) => ({ ...f, supplier_internal_id: e.target.value }))}
                className={inputCls}
                data-testid="manual-price-spec-supplier-id"
              />
            </div>
            <div>
              <label className="text-xs text-[#667085] block mb-1">Price</label>
              <input
                required
                type="number"
                step="0.01"
                value={manualForm.price}
                onChange={(e) => setManualForm((f) => ({ ...f, price: e.target.value }))}
                className={inputCls}
                data-testid="manual-price-spec-price"
              />
            </div>
            <div>
              <label className="text-xs text-[#667085] block mb-1">Currency</label>
              <input
                value={manualForm.currency}
                onChange={(e) => setManualForm((f) => ({ ...f, currency: e.target.value }))}
                className={inputCls}
                data-testid="manual-price-spec-currency"
              />
            </div>
            <Button
              type="submit"
              disabled={manualWriting}
              className="h-9 bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm"
              data-testid="manual-price-spec-write-button"
            >
              {manualWriting ? "Writing..." : "Write to SAP"}
            </Button>
          </form>
          {manualError && (
            <div className="mt-3 flex items-start gap-2 bg-[#FEF3F2] border border-[#FECDCA] rounded-sm p-3" data-testid="manual-price-spec-error">
              <WarningCircle size={14} weight="fill" className="text-[#B42318] mt-0.5 shrink-0" />
              <span className="text-[13px] text-[#B42318]">{manualError}</span>
            </div>
          )}
        </section>

        <section className="bg-white border border-[#D0D5DD] rounded-sm p-4" data-testid="read-price-spec-section">
          <div className="flex items-center gap-2 mb-2">
            <Database size={16} weight="bold" className="text-[#004B87]" />
            <h2 className="font-heading text-sm font-bold text-[#1D2939]">Read Price Specs from SAP</h2>
          </div>
          <div className="flex items-center gap-2 mb-3">
            <input
              placeholder="Product ID"
              value={readProductId}
              onChange={(e) => setReadProductId(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && readFromSap(readProductId)}
              className={`${inputCls} w-48`}
              data-testid="read-price-spec-product-id"
            />
            <Button
              type="button"
              variant="outline"
              onClick={() => readFromSap(readProductId)}
              disabled={readLoading || !readProductId.trim()}
              className="h-9 text-sm rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="read-price-spec-button"
            >
              {readLoading ? "Reading..." : "Read from SAP"}
            </Button>
          </div>
          {readError && (
            <div className="flex items-start gap-2 bg-[#FEF3F2] border border-[#FECDCA] rounded-sm p-3" data-testid="read-price-spec-error">
              <WarningCircle size={14} weight="fill" className="text-[#B42318] mt-0.5 shrink-0" />
              <span className="text-[13px] text-[#B42318]">{readError}</span>
            </div>
          )}
          {readSpecs && (
            <div className="border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="read-price-spec-table">
              <table className="w-full text-[13px] border-collapse">
                <thead>
                  <tr>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Internal ID</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">Price</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Status</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Valid From</th>
                  </tr>
                </thead>
                <tbody>
                  {readSpecs.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="border border-[#D0D5DD] text-center py-4 text-[#475467]" data-testid="read-price-spec-empty">
                        No price specs found in SAP for this Product ID
                      </td>
                    </tr>
                  ) : (
                    readSpecs.map((s, i) => (
                      <tr key={s.sap_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`read-price-spec-row-${i}`}>
                        <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{s.supplier_name || "—"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.supplier_internal_id || "—"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]">
                          {s.currency} {s.price}
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1">
                          <Badge
                            variant="outline"
                            className={
                              s.release_status_code === "3"
                                ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] text-xs"
                                : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs"
                            }
                          >
                            {s.release_status_code === "3" ? "Released" : s.release_status_code || "—"}
                          </Badge>
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.start_date || "—"}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="bg-white border border-[#D0D5DD] rounded-sm p-4" data-testid="bulk-push-erp-section">
          <div className="flex items-center gap-2 mb-2">
            <CloudArrowUp size={16} weight="bold" className="text-[#004B87]" />
            <h2 className="font-heading text-sm font-bold text-[#1D2939]">Bulk Push ERP Prices to SAP</h2>
          </div>
          <p className="text-[13px] text-[#475467] mb-3">
            For every known component, pushes the ERP's most recent real billed price + supplier into SAP as a new
            Procurement Price Specification - only for parts that don't already have a{" "}
            <span className="font-bold">Released</span> price in SAP. Parts with no ERP history, or whose ERP
            supplier isn't a known synced SAP supplier, are skipped.
          </p>

          <div className="flex items-center gap-2">
            <Button
              type="button"
              onClick={startBulkPush}
              disabled={status === "running"}
              className="h-9 bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm"
              data-testid="bulk-push-erp-start-button"
            >
              <CloudArrowUp size={14} className={`mr-1.5 ${status === "running" ? "animate-pulse" : ""}`} />
              {status === "running" ? "Pushing to SAP..." : "Push ERP Prices to SAP"}
            </Button>
            {status === "running" && (
              <Button
                type="button"
                variant="outline"
                onClick={stopBulkPush}
                disabled={stopping}
                className="h-9 text-sm rounded-sm border-[#FECDCA] text-[#B42318] hover:bg-[#FEF3F2]"
                data-testid="bulk-push-erp-stop-button"
              >
                <Stop size={14} weight="fill" className="mr-1.5" />
                {stopping ? "Stopping..." : "Stop"}
              </Button>
            )}
          </div>

          {status === "running" && (
            <div className="mt-4" data-testid="bulk-push-erp-progress">
              <div className="flex items-center justify-between text-xs text-[#475467] mb-1">
                <span>Processing components...</span>
                <span>
                  {progress.processed} / {progress.total || "?"}
                </span>
              </div>
              <div className="h-2 bg-[#EAECF0] rounded-full overflow-hidden">
                <div
                  className="h-full bg-[#004B87] transition-all duration-300"
                  style={{
                    width: progress.total ? `${Math.min(100, (progress.processed / progress.total) * 100)}%` : "5%",
                  }}
                />
              </div>
            </div>
          )}

          {status === "failed" && error && (
            <div
              className="mt-4 flex items-start gap-2 bg-[#FEF3F2] border border-[#FECDCA] rounded-sm p-3"
              data-testid="bulk-push-erp-error"
            >
              <WarningCircle size={14} weight="fill" className="text-[#B42318] mt-0.5 shrink-0" />
              <span className="text-[13px] text-[#B42318]">{error}</span>
            </div>
          )}

          {status === "done" && result && (
            <div className="mt-4 space-y-2" data-testid="bulk-push-erp-result">
              <div className={`flex items-center gap-2 ${result.cancelled ? "text-[#B54708]" : "text-[#027A48]"}`}>
                <CheckCircle size={16} weight="fill" />
                <span className="text-[13px] font-bold">
                  {result.cancelled ? "Bulk push stopped by user (partial result)" : "Bulk push complete"}
                </span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
                <Badge variant="outline" className="bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] justify-center py-1.5" data-testid="bulk-push-erp-result-total">
                  Total: {result.total}
                </Badge>
                <Badge variant="outline" className="bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] justify-center py-1.5" data-testid="bulk-push-erp-result-pushed">
                  Pushed: {result.pushed}
                </Badge>
                <Badge variant="outline" className="bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] justify-center py-1.5" data-testid="bulk-push-erp-result-skipped-released">
                  Already Released: {result.skipped_already_released}
                </Badge>
                <Badge variant="outline" className="bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] justify-center py-1.5" data-testid="bulk-push-erp-result-skipped-no-erp">
                  No ERP Data: {result.skipped_no_erp_data}
                </Badge>
                <Badge variant="outline" className="bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] justify-center py-1.5" data-testid="bulk-push-erp-result-skipped-unknown">
                  Unknown Supplier: {result.skipped_unknown_supplier}
                </Badge>
              </div>

              {result.pushed_items.length > 0 && (
                <div className="border border-[#ABEFC6] rounded-sm overflow-hidden mt-2">
                  <div className="bg-[#ECFDF3] px-2.5 py-1.5 text-xs font-bold text-[#027A48]">
                    Pushed to SAP ({result.pushed_items.length})
                  </div>
                  <div className="max-h-64 overflow-auto">
                    <table className="w-full text-[13px] border-collapse min-w-[500px]" data-testid="bulk-push-erp-pushed-table">
                      <thead>
                        <tr>
                          <th className="bg-[#F0FDF6] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase sticky top-0">Product ID</th>
                          <th className="bg-[#F0FDF6] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase sticky top-0">Supplier</th>
                          <th className="bg-[#F0FDF6] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase sticky top-0">Price</th>
                          <th className="bg-[#F0FDF6] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase sticky top-0">Bill Date</th>
                        </tr>
                      </thead>
                      <tbody>
                        {result.pushed_items.map((p, i) => (
                          <tr key={p.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`bulk-push-erp-pushed-row-${i}`}>
                            <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{p.product_id}</td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{p.supplier}</td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]">
                              {p.currency} {p.price}
                            </td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{p.bill_date || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {result.failed.length > 0 && (
                <div className="border border-[#FECDCA] rounded-sm overflow-hidden mt-2">
                  <div className="bg-[#FEF3F2] px-2.5 py-1.5 text-xs font-bold text-[#B42318]">
                    Failed ({result.failed.length})
                  </div>
                  <div className="max-h-48 overflow-auto">
                    <table className="w-full text-[13px] border-collapse min-w-[400px]" data-testid="bulk-push-erp-failed-table">
                      <tbody>
                        {result.failed.map((f, i) => (
                          <tr key={f.product_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"}>
                            <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828] w-32">{f.product_id}</td>
                            <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{f.error}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
