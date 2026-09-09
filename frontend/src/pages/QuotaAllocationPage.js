import { useState, useEffect, useRef, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import {
  Database,
  Shield,
  ShieldCheck,
  MagnifyingGlass,
  WarningCircle,
  Sparkle,
  ArrowsClockwise,
  ClockCounterClockwise,
  XCircle,
  Plus,
  ArrowBendDownRight,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandInput, CommandList, CommandEmpty, CommandGroup, CommandItem } from "@/components/ui/command";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const inputCls =
  "h-8 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87] w-full";
const labelCls = "font-heading text-xs font-bold text-[#475467] uppercase tracking-wide";
const RELEASE_STATUS_LABELS = { "1": "Not Released", "2": "Partially Released", "3": "Released", "5": "Release Canceled" };

// Raw ERP source data occasionally has bill-date typos (e.g. year 2027)
// which would otherwise look like a valid, very recent price. Flag them
// instead of trusting them at face value.
const isFutureBillDate = (dateStr) => {
  if (!dateStr) return false;
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return false;
  return d.getTime() > Date.now();
};

// Vendor code (SAP internal supplier ID, e.g. "S2560") shown alongside the
// name so buyers can cross-reference against SAP screens directly.
const withVendorCode = (name, code) => {
  if (!name) return code ? `SAP Supplier ${code}` : "Unknown Supplier";
  return code ? `${name} (${code})` : name;
};

const DOC_TYPE_BADGE_CLS = {
  "Invoice": "bg-[#EFF4FF] text-[#004B87] border-[#B8D4ED]",
  "Credit Memo": "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA]",
  "Debit Memo": "bg-[#FFF6ED] text-[#B54708] border-[#FDDCAB]",
};
const docTypeBadgeCls = (docType) => DOC_TYPE_BADGE_CLS[docType] || "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD]";

// A Credit Memo that reverses a specific Invoice shares that Invoice's own
// vendor document reference (supplier_invoice_number) - the backend already
// resolves this into `reverses_invoice_id`. Here we turn the flat row list
// into a display list where a reversing Credit Memo is grouped right under
// the Invoice it reverses (never shown as a standalone look-alike
// "duplicate" row) - see sap_supplier_invoice_client.py for the matching
// logic. Rows are returned as {row, creditMemos: []} pairs, in the same
// order as the input, minus any Credit Memo rows that got grouped under
// an earlier/later Invoice in the same list.
const buildPurchaseHistoryGroups = (rows) => {
  const byInvoiceId = new Set(rows.map((r) => r.invoice_id));
  const creditsByInvoiceId = {};
  const groupedCreditIds = new Set();
  rows.forEach((row) => {
    if (row.document_type === "Credit Memo" && row.reverses_invoice_id && byInvoiceId.has(row.reverses_invoice_id)) {
      groupedCreditIds.add(row.invoice_id);
      if (!creditsByInvoiceId[row.reverses_invoice_id]) creditsByInvoiceId[row.reverses_invoice_id] = [];
      creditsByInvoiceId[row.reverses_invoice_id].push(row);
    }
  });
  return rows
    .filter((row) => !groupedCreditIds.has(row.invoice_id))
    .map((row) => ({ row, creditMemos: creditsByInvoiceId[row.invoice_id] || [] }));
};

export default function QuotaAllocationPage() {
  const [suppliers, setSuppliers] = useState([]);

  const [productIdInput, setProductIdInput] = useState("");
  const [activeProductId, setActiveProductId] = useState(null);
  const [sapPriceSpecs, setSapPriceSpecs] = useState([]);
  const [sapPriceSpecsLoading, setSapPriceSpecsLoading] = useState(false);
  const [sapPriceSpecsError, setSapPriceSpecsError] = useState(null);
  const [erpPrices, setErpPrices] = useState([]);
  const [erpPricesLoading, setErpPricesLoading] = useState(false);
  const [erpPricesError, setErpPricesError] = useState(null);
  const [sapPurchaseHistory, setSapPurchaseHistory] = useState([]);
  const [sapPurchaseHistoryLoading, setSapPurchaseHistoryLoading] = useState(false);
  const [sapPurchaseHistoryError, setSapPurchaseHistoryError] = useState(null);
  const [sapReceiptDates, setSapReceiptDates] = useState([]);
  const [sapReceiptDatesLoading, setSapReceiptDatesLoading] = useState(false);
  const [sapReceiptDatesError, setSapReceiptDatesError] = useState(null);
  const productLoadRequestRef = useRef(null);

  const [productSuggestions, setProductSuggestions] = useState([]);
  const [showProductSuggestions, setShowProductSuggestions] = useState(false);
  const suggestionRequestRef = useRef(null);
  const suggestionDebounceRef = useRef(null);
  const productInputWrapperRef = useRef(null);

  // Quota Arrangement - AI-suggested, buyer-editable, revision-tracked.
  const [quotaArrangementId, setQuotaArrangementId] = useState(null);
  const [quotaRevisionNo, setQuotaRevisionNo] = useState(0);
  const [quotaAllocations, setQuotaAllocations] = useState([]);
  const [quotaSource, setQuotaSource] = useState("ai"); // "ai" | "user" - flips to "user" on any manual edit
  const [quotaOverallRationale, setQuotaOverallRationale] = useState(null);
  const [quotaRevisions, setQuotaRevisions] = useState([]);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaSuggesting, setQuotaSuggesting] = useState(false);
  const [quotaError, setQuotaError] = useState(null);
  const [quotaRemarks, setQuotaRemarks] = useState("");
  const [quotaCreatedBy, setQuotaCreatedBy] = useState("");
  const [quotaSaving, setQuotaSaving] = useState(false);
  const [showQuotaHistory, setShowQuotaHistory] = useState(false);
  const [addSupplierPickerOpen, setAddSupplierPickerOpen] = useState(false);

  useEffect(() => {
    axios
      .get(`${API}/suppliers`)
      .then(({ data }) => setSuppliers(data))
      .catch((err) => toast.error("Could not load supplier list", { description: err?.response?.data?.detail || err.message }));
  }, []);

  // Goods Receipt responses from SAP only carry the supplier's internal
  // SAP ID (no name snapshot on that document) - resolve it against the
  // Supplier Master list we already have loaded.
  const nameForSapId = (sapInternalId) => suppliers.find((s) => s.sap_internal_id === sapInternalId)?.name;

  const applySuggestion = (data) => {
    setQuotaAllocations(data.allocations || []);
    setQuotaOverallRationale(data.overall_rationale || null);
    setQuotaSource("ai");
  };

  const suggestQuota = async (productId) => {
    setQuotaSuggesting(true);
    setQuotaError(null);
    try {
      const { data } = await axios.post(`${API}/quota-arrangements/${encodeURIComponent(productId)}/suggest`);
      if (productLoadRequestRef.current !== productId) return;
      applySuggestion(data);
    } catch (err) {
      if (productLoadRequestRef.current !== productId) return;
      setQuotaError(err?.response?.data?.detail || err.message || "Could not generate AI suggestion");
    } finally {
      if (productLoadRequestRef.current === productId) setQuotaSuggesting(false);
    }
  };

  const loadQuotaArrangement = async (productId) => {
    setQuotaLoading(true);
    setQuotaError(null);
    setQuotaAllocations([]);
    setQuotaOverallRationale(null);
    setQuotaRemarks("");
    setShowQuotaHistory(false);
    try {
      const { data } = await axios.get(`${API}/quota-arrangements/${encodeURIComponent(productId)}`);
      if (productLoadRequestRef.current !== productId) return;
      setQuotaArrangementId(data.arrangement_id);
      setQuotaRevisionNo(data.current_revision_no);
      setQuotaRevisions(data.revisions || []);
      if (data.latest_revision) {
        setQuotaAllocations(data.latest_revision.allocations || []);
        setQuotaSource("user");
        setQuotaOverallRationale(null);
      } else {
        // Never arranged before for this part - default straight to an AI suggestion.
        await suggestQuota(productId);
      }
    } catch (err) {
      if (productLoadRequestRef.current !== productId) return;
      setQuotaError(err?.response?.data?.detail || err.message || "Could not load quota arrangement");
    } finally {
      if (productLoadRequestRef.current === productId) setQuotaLoading(false);
    }
  };

  const loadSapPriceSpecs = async (productId) => {
    setSapPriceSpecsLoading(true);
    setSapPriceSpecsError(null);
    try {
      const { data } = await axios.get(`${API}/suppliers/sap-price-specs/${encodeURIComponent(productId)}`);
      if (productLoadRequestRef.current !== productId) return;
      setSapPriceSpecs(data);
    } catch (err) {
      if (productLoadRequestRef.current !== productId) return;
      setSapPriceSpecs([]);
      setSapPriceSpecsError(err?.response?.data?.detail || err.message || "Could not read SAP purchasing prices");
    } finally {
      if (productLoadRequestRef.current === productId) setSapPriceSpecsLoading(false);
    }
  };

  const loadErpPrices = async (productId) => {
    setErpPricesLoading(true);
    setErpPricesError(null);
    try {
      const { data } = await axios.get(`${API}/suppliers/erp-prices/${encodeURIComponent(productId)}`);
      if (productLoadRequestRef.current !== productId) return;
      setErpPrices(data);
    } catch (err) {
      if (productLoadRequestRef.current !== productId) return;
      setErpPrices([]);
      setErpPricesError(err?.response?.data?.detail || err.message || "Could not read ERP purchase history");
    } finally {
      if (productLoadRequestRef.current === productId) setErpPricesLoading(false);
    }
  };

  const loadSapPurchaseHistory = async (productId) => {
    setSapPurchaseHistoryLoading(true);
    setSapPurchaseHistoryError(null);
    try {
      const { data } = await axios.get(`${API}/suppliers/sap-purchase-history/${encodeURIComponent(productId)}`);
      if (productLoadRequestRef.current !== productId) return;
      setSapPurchaseHistory(data);
    } catch (err) {
      if (productLoadRequestRef.current !== productId) return;
      setSapPurchaseHistory([]);
      setSapPurchaseHistoryError(err?.response?.data?.detail || err.message || "Could not read SAP Supplier Invoice history");
    } finally {
      if (productLoadRequestRef.current === productId) setSapPurchaseHistoryLoading(false);
    }
  };

  const loadSapReceiptDates = async (productId) => {
    setSapReceiptDatesLoading(true);
    setSapReceiptDatesError(null);
    try {
      const { data } = await axios.get(`${API}/suppliers/sap-receipt-dates/${encodeURIComponent(productId)}`);
      if (productLoadRequestRef.current !== productId) return;
      setSapReceiptDates(data);
    } catch (err) {
      if (productLoadRequestRef.current !== productId) return;
      setSapReceiptDates([]);
      setSapReceiptDatesError(err?.response?.data?.detail || err.message || "Could not read SAP Goods Receipt history");
    } finally {
      if (productLoadRequestRef.current === productId) setSapReceiptDatesLoading(false);
    }
  };

  const searchProduct = (pidOverride) => {
    const pid = (pidOverride ?? productIdInput).trim().toUpperCase();
    if (!pid) return;
    setProductIdInput(pid);
    setShowProductSuggestions(false);
    productLoadRequestRef.current = pid;
    setActiveProductId(pid);
    loadSapPriceSpecs(pid);
    loadErpPrices(pid);
    loadSapPurchaseHistory(pid);
    loadSapReceiptDates(pid);
    loadQuotaArrangement(pid);
  };

  useEffect(() => {
    if (suggestionDebounceRef.current) clearTimeout(suggestionDebounceRef.current);
    const q = productIdInput.trim();
    if (q.length < 2) {
      setProductSuggestions([]);
      return;
    }
    suggestionDebounceRef.current = setTimeout(async () => {
      const requestId = q;
      suggestionRequestRef.current = requestId;
      try {
        const { data } = await axios.get(`${API}/products/search`, { params: { q, limit: 10 } });
        if (suggestionRequestRef.current === requestId) setProductSuggestions(data);
      } catch {
        if (suggestionRequestRef.current === requestId) setProductSuggestions([]);
      }
    }, 250);
    return () => clearTimeout(suggestionDebounceRef.current);
  }, [productIdInput]);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (productInputWrapperRef.current && !productInputWrapperRef.current.contains(e.target)) {
        setShowProductSuggestions(false);
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  // Suppliers with a Released SAP price or real ERP purchase history for the
  // currently-loaded product are surfaced first in the "+ Add Supplier"
  // picker, since those are far more likely to be the right pick. "others"
  // is the full remaining supplier list (searchable via the picker's search
  // box, so no need to cap it - buyers can type to find any of them).
  const getRecommendedSuppliers = () => {
    const releasedSapIds = new Set(
      sapPriceSpecs.filter((s) => s.release_status_code === "3" && s.supplier_internal_id).map((s) => s.supplier_internal_id)
    );
    const erpIds = new Set(
      erpPrices.flatMap((item) => [item.lowest?.pcode, item.last?.pcode]).filter(Boolean)
    );
    const alreadyIn = new Set(quotaAllocations.map((a) => a.supplier_id).filter(Boolean));
    const recommended = suppliers.filter(
      (s) => !alreadyIn.has(s.id) && s.sap_internal_id && (releasedSapIds.has(s.sap_internal_id) || erpIds.has(s.sap_internal_id))
    );
    const recommendedIds = new Set(recommended.map((s) => s.id));
    const others = suppliers.filter((s) => !alreadyIn.has(s.id) && !recommendedIds.has(s.id));
    return { recommended, others };
  };

  const updateAllocation = (index, field, value) => {
    setQuotaAllocations((prev) => prev.map((a, i) => (i === index ? { ...a, [field]: value } : a)));
    setQuotaSource("user");
  };

  const removeAllocationRow = (index) => {
    setQuotaAllocations((prev) => prev.filter((_, i) => i !== index));
    setQuotaSource("user");
  };

  const addAllocationRow = (supplierId) => {
    const sup = suppliers.find((s) => s.id === supplierId);
    if (!sup) return;
    setQuotaAllocations((prev) => [
      ...prev,
      { supplier_id: sup.id, supplier_name: sup.name, sap_internal_id: sup.sap_internal_id, price: null, currency: null, price_source: "Manually added", lead_time_days: null, quota_percent: 0, rationale: "Manually added by buyer." },
    ]);
    setQuotaSource("user");
    setAddSupplierPickerOpen(false);
  };

  const quotaTotal = quotaAllocations.reduce((sum, a) => sum + (Number(a.quota_percent) || 0), 0);
  const quotaTotalValid = quotaAllocations.length === 0 || Math.abs(quotaTotal - 100) < 0.5;

  const confirmQuotaArrangement = async () => {
    if (!quotaTotalValid) {
      toast.error("Quota percentages must sum to 100", { description: `Currently ${quotaTotal.toFixed(1)}%` });
      return;
    }
    setQuotaSaving(true);
    try {
      const { data } = await axios.post(`${API}/quota-arrangements/${encodeURIComponent(activeProductId)}/confirm`, {
        allocations: quotaAllocations.map((a) => ({ ...a, quota_percent: Number(a.quota_percent) || 0 })),
        remarks: quotaRemarks || null,
        source: quotaSource,
        created_by: quotaCreatedBy || null,
      });
      setQuotaArrangementId(data.arrangement_id);
      setQuotaRevisionNo(data.current_revision_no);
      setQuotaRevisions(data.revisions || []);
      setQuotaAllocations(data.latest_revision?.allocations || []);
      setQuotaSource("user");
      toast.success("Quota arrangement saved", { description: `${data.arrangement_id} - revision ${data.current_revision_no}` });
    } catch (err) {
      toast.error("Failed to save quota arrangement", { description: err?.response?.data?.detail || err.message });
    } finally {
      setQuotaSaving(false);
    }
  };

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
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Quota Allocation</span>
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
        {/* Part <-> Supplier Assignments */}
        <section className="bg-white border border-[#D0D5DD] rounded-sm" data-testid="part-supplier-section">
          <div className="p-2.5 border-b border-[#D0D5DD] flex items-center gap-2 flex-wrap">
            <ShieldCheck size={16} weight="bold" className="text-[#004B87]" />
            <h2 className="font-heading text-sm font-bold text-[#1D2939]">Part ↔ Supplier Assignments</h2>
            <div className="flex items-center gap-1.5 ml-2">
              <label htmlFor="part-supplier-product-id-input" className={labelCls}>Product ID</label>
              <div className="relative" ref={productInputWrapperRef}>
                <input
                  id="part-supplier-product-id-input"
                  type="text"
                  placeholder="e.g. SPC5WM"
                  value={productIdInput}
                  onChange={(e) => {
                    setProductIdInput(e.target.value);
                    setShowProductSuggestions(true);
                  }}
                  onFocus={() => setShowProductSuggestions(true)}
                  onKeyDown={(e) => e.key === "Enter" && searchProduct()}
                  autoComplete="off"
                  className={`${inputCls} w-40`}
                  data-testid="part-supplier-product-id-input"
                />
                {showProductSuggestions && productSuggestions.length > 0 && (
                  <div
                    className="absolute z-20 top-full left-0 mt-1 w-72 bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-64 overflow-y-auto"
                    data-testid="product-id-suggestions"
                  >
                    {productSuggestions.map((s) => (
                      <button
                        type="button"
                        key={s.product_id}
                        onClick={() => searchProduct(s.product_id)}
                        className="w-full text-left px-2.5 py-1.5 hover:bg-[#F2F4F7] border-b border-[#F2F4F7] last:border-b-0"
                        data-testid={`product-id-suggestion-${s.product_id}`}
                      >
                        <div className="text-[13px] font-bold text-[#101828]">{s.product_id}</div>
                        {s.description && <div className="text-xs text-[#667085] truncate">{s.description}</div>}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <Button
                type="button"
                variant="outline"
                onClick={() => searchProduct()}
                disabled={!productIdInput.trim()}
                className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
                data-testid="part-supplier-search-button"
              >
                <MagnifyingGlass size={13} className="mr-1.5" />
                Load
              </Button>
            </div>
          </div>

          {!activeProductId ? (
            <div className="py-10 text-center text-[13px] text-[#98A2B3]" data-testid="part-supplier-empty-state">
              Enter a Product ID above and click "Load" to view/manage its supplier assignments
            </div>
          ) : (
            <div>
              {/* Real SAP purchasing data - read-only */}
              <div className="p-2.5 bg-[#F9FAFB] border-b border-[#D0D5DD]" data-testid="sap-price-specs-panel">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <Badge variant="outline" className="bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] text-xs">SAP</Badge>
                  <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                    Existing Purchasing Prices in SAP (read-only)
                  </span>
                </div>
                {sapPriceSpecsLoading ? (
                  <div className="text-[13px] text-[#475467] py-2">Reading from SAP...</div>
                ) : sapPriceSpecsError ? (
                  <div className="text-[13px] text-[#B54708] py-1" data-testid="sap-price-specs-error">{sapPriceSpecsError}</div>
                ) : sapPriceSpecs.length === 0 ? (
                  <div className="text-[13px] text-[#98A2B3] py-1" data-testid="sap-price-specs-empty">
                    No purchasing price records found in SAP for "{activeProductId}"
                  </div>
                ) : (
                  <div>
                    {!sapPriceSpecs.some((p) => p.release_status_code === "3") && (
                      <div
                        className="mb-1.5 flex items-start gap-1.5 bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-2"
                        data-testid="sap-price-specs-no-released-warning"
                      >
                        <WarningCircle size={14} weight="fill" className="text-[#B54708] mt-0.5 shrink-0" />
                        <span className="text-[13px] text-[#7A4504]">
                          No <span className="font-bold">Released</span> purchasing price is on file in SAP for this part.
                          The record(s) below are still drafts/not released and should NOT be relied on for sourcing decisions.
                        </span>
                      </div>
                    )}
                    <div className="overflow-x-auto">
                      <table className="w-full text-[13px] border-collapse" data-testid="sap-price-specs-table">
                        <thead>
                          <tr>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase">Price</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Valid From</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Valid To</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {[...sapPriceSpecs]
                            .sort((a, b) => (a.release_status_code === "3" ? -1 : 1) - (b.release_status_code === "3" ? -1 : 1))
                            .map((p, i) => {
                              const isReleased = p.release_status_code === "3";
                              const statusLabel = RELEASE_STATUS_LABELS[p.release_status_code] || `Code ${p.release_status_code || "—"}`;
                              return (
                                <tr key={p.sap_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`sap-price-spec-row-${i}`}>
                                  <td className={`border border-[#D0D5DD] px-1.5 py-1 ${isReleased ? "text-[#101828]" : "text-[#98A2B3]"}`}>
                                    {withVendorCode(p.supplier_name, p.supplier_internal_id)}
                                  </td>
                                  <td className={`border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums ${isReleased && p.price ? "text-[#101828]" : "text-[#98A2B3]"}`}>
                                    {p.price != null ? `${p.currency || ""} ${p.price}` : "—"}
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{p.start_date || "—"}</td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{p.end_date === "9999-12-31" ? "Open" : p.end_date || "—"}</td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1">
                                    <Badge
                                      variant="outline"
                                      className={
                                        isReleased
                                          ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] text-xs"
                                          : "bg-[#FEF3F2] text-[#B42318] border-[#FECDCA] text-xs"
                                      }
                                    >
                                      {statusLabel}
                                    </Badge>
                                  </td>
                                </tr>
                              );
                            })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>

              {/* Real ERP purchase history - read-only, most reliable source */}
              <div className="p-2.5 bg-[#F9FAFB] border-b border-[#D0D5DD]" data-testid="erp-prices-panel">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <Badge variant="outline" className="bg-[#FDF2FA] text-[#9E165F] border-[#F3B6D9] text-xs">ERP</Badge>
                  <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                    Real Purchase History (last 6 months, from billed invoices)
                  </span>
                </div>
                {erpPricesLoading ? (
                  <div className="text-[13px] text-[#475467] py-2">Reading from ERP...</div>
                ) : erpPricesError ? (
                  <div className="text-[13px] text-[#B54708] py-1" data-testid="erp-prices-error">{erpPricesError}</div>
                ) : erpPrices.length === 0 ? (
                  <div className="text-[13px] text-[#98A2B3] py-1" data-testid="erp-prices-empty">
                    No billed purchase history found in the ERP for "{activeProductId}"
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-[13px] border-collapse" data-testid="erp-prices-table">
                      <thead>
                        <tr>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Item</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Most Recent</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Lowest Seen</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase">6-Mo Avg</th>
                        </tr>
                      </thead>
                      <tbody>
                        {erpPrices.map((item, i) => (
                          <tr key={item.icode} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`erp-price-row-${i}`}>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">{item.iname || item.icode}</td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">
                              {item.last ? (
                                isFutureBillDate(item.last.bill_date) ? (
                                  <span className="text-[#98A2B3] inline-flex items-center gap-1" data-testid={`erp-price-row-${i}-last-future-flag`} title="This bill date is in the future - likely a data entry error in the source ERP. Not a reliable 'most recent' price.">
                                    <WarningCircle size={12} weight="fill" className="text-[#B54708] shrink-0" />
                                    {item.last.supplier} @ ₹{item.last.rate} ({item.last.bill_date})
                                  </span>
                                ) : (
                                  `${item.last.supplier} @ ₹${item.last.rate} (${item.last.bill_date})`
                                )
                              ) : "—"}
                            </td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">
                              {item.lowest ? (
                                isFutureBillDate(item.lowest.bill_date) ? (
                                  <span className="text-[#98A2B3] inline-flex items-center gap-1" data-testid={`erp-price-row-${i}-lowest-future-flag`} title="This bill date is in the future - likely a data entry error in the source ERP. Not a reliable 'lowest seen' price.">
                                    <WarningCircle size={12} weight="fill" className="text-[#B54708] shrink-0" />
                                    {item.lowest.supplier} @ ₹{item.lowest.rate} ({item.lowest.bill_date})
                                  </span>
                                ) : (
                                  `${item.lowest.supplier} @ ₹${item.lowest.rate} (${item.lowest.bill_date})`
                                )
                              ) : "—"}
                            </td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[#101828]">
                              {item.average ? `₹${item.average.rate} (${item.average.bill_count} bills)` : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Real SAP-native Supplier Invoice history - authoritative cross-check */}
              <div className="p-2.5 bg-[#F9FAFB] border-b border-[#D0D5DD]" data-testid="sap-purchase-history-panel">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <Badge variant="outline" className="bg-[#EFF4FF] text-[#004B87] border-[#B8D4ED] text-xs">SAP</Badge>
                  <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                    Purchase History from SAP (real posted Supplier Invoices)
                  </span>
                </div>
                {sapPurchaseHistoryLoading ? (
                  <div className="text-[13px] text-[#475467] py-2">Reading from SAP...</div>
                ) : sapPurchaseHistoryError ? (
                  <div className="text-[13px] text-[#B54708] py-1" data-testid="sap-purchase-history-error">{sapPurchaseHistoryError}</div>
                ) : sapPurchaseHistory.length === 0 ? (
                  <div className="text-[13px] text-[#98A2B3] py-1" data-testid="sap-purchase-history-empty">
                    No posted Supplier Invoices found in SAP for "{activeProductId}"
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-[13px] border-collapse" data-testid="sap-purchase-history-table">
                      <thead>
                        <tr>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Type</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Invoice</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Invoice Date</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase">Qty</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase">Price</th>
                        </tr>
                      </thead>
                      <tbody>
                        {buildPurchaseHistoryGroups(sapPurchaseHistory).map(({ row, creditMemos }, i) => {
                          const netQty = creditMemos.length
                            ? (row.quantity || 0) - creditMemos.reduce((sum, c) => sum + (c.quantity || 0), 0)
                            : null;
                          return (
                            <Fragment key={`${row.invoice_id}-${i}`}>
                              <tr className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`sap-purchase-history-row-${i}`}>
                                <td className="border border-[#D0D5DD] px-1.5 py-1">
                                  <Badge variant="outline" className={`text-[11px] ${docTypeBadgeCls(row.document_type)}`} data-testid={`sap-purchase-history-doctype-${i}`}>
                                    {row.document_type || "—"}
                                  </Badge>
                                </td>
                                <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">
                                  <div>{row.supplier_invoice_number || "—"}</div>
                                  {row.invoice_id && (
                                    <div className="text-[11px] text-[#98A2B3]" data-testid={`sap-purchase-history-sapdoc-${i}`}>
                                      SAP Doc: {row.invoice_id}
                                    </div>
                                  )}
                                </td>
                                <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{row.date || "—"}</td>
                                <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">
                                  {withVendorCode(row.supplier_name, row.supplier_internal_id)}
                                </td>
                                <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[#475467]">
                                  {row.quantity != null ? `${row.quantity.toLocaleString()} ${row.unit_of_measure || ""}` : "—"}
                                </td>
                                <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[#101828]">
                                  {row.price != null ? `${row.currency || ""} ${row.price}` : "—"}
                                </td>
                              </tr>
                              {creditMemos.map((cm, ci) => (
                                <tr key={`${cm.invoice_id}-${i}-${ci}`} className="bg-[#FEF3F2]" data-testid={`sap-purchase-history-creditmemo-row-${i}-${ci}`}>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1">
                                    <Badge variant="outline" className={`text-[11px] ${docTypeBadgeCls(cm.document_type)}`}>
                                      {cm.document_type}
                                    </Badge>
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#B42318]">
                                    <span className="inline-flex items-center gap-1">
                                      <ArrowBendDownRight size={12} weight="bold" />
                                      {cm.supplier_invoice_number || "—"}
                                    </span>
                                    {cm.invoice_id && (
                                      <div className="text-[11px] text-[#B54748]" data-testid={`sap-purchase-history-creditmemo-sapdoc-${i}-${ci}`}>
                                        SAP Doc: {cm.invoice_id}
                                      </div>
                                    )}
                                    <div className="text-[11px] text-[#912018] italic">
                                      Reverses Invoice {row.supplier_invoice_number || row.invoice_id}
                                      {row.invoice_id ? ` (SAP Doc: ${row.invoice_id})` : ""}
                                    </div>
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#B42318]">{cm.date || "—"}</td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#B42318]">
                                    {withVendorCode(cm.supplier_name, cm.supplier_internal_id)}
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[#B42318]">
                                    -{cm.quantity != null ? `${cm.quantity.toLocaleString()} ${cm.unit_of_measure || ""}` : "—"}
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[#B42318]">
                                    {cm.price != null ? `${cm.currency || ""} ${cm.price}` : "—"}
                                  </td>
                                </tr>
                              ))}
                              {creditMemos.length > 0 && (
                                <tr key={`${row.invoice_id}-${i}-net`} className="bg-[#F9FAFB]" data-testid={`sap-purchase-history-net-row-${i}`}>
                                  <td colSpan={4} className="border border-[#D0D5DD] px-1.5 py-1 text-right text-[11px] font-bold text-[#475467] uppercase font-heading">
                                    Net Purchase Qty ({row.supplier_invoice_number || row.invoice_id}):
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[11px] font-bold text-[#101828]">
                                    {netQty != null ? `${netQty.toLocaleString()} ${row.unit_of_measure || ""}` : "—"}
                                  </td>
                                  <td className="border border-[#D0D5DD] px-1.5 py-1"></td>
                                </tr>
                              )}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Real SAP-native Goods Receipt (Goods & Service Acknowledgement)
                  history - the actual physical delivery date, a genuinely
                  different document from the Supplier Invoice (billing) above. */}
              <div className="p-2.5 bg-[#F9FAFB] border-b border-[#D0D5DD]" data-testid="sap-receipt-dates-panel">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <Badge variant="outline" className="bg-[#EFF4FF] text-[#004B87] border-[#B8D4ED] text-xs">SAP</Badge>
                  <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                    Goods Receipts from SAP (real posted delivery dates)
                  </span>
                </div>
                {sapReceiptDatesLoading ? (
                  <div className="text-[13px] text-[#475467] py-2">Reading from SAP...</div>
                ) : sapReceiptDatesError ? (
                  <div className="text-[13px] text-[#B54708] py-1" data-testid="sap-receipt-dates-error">{sapReceiptDatesError}</div>
                ) : sapReceiptDates.length === 0 ? (
                  <div className="text-[13px] text-[#98A2B3] py-1" data-testid="sap-receipt-dates-empty">
                    No posted Goods Receipts found in SAP for "{activeProductId}"
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-[13px] border-collapse" data-testid="sap-receipt-dates-table">
                      <thead>
                        <tr>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Goods Receipt</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Receipt Date</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">PO</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase">Qty</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sapReceiptDates.map((row, i) => (
                          <tr key={`${row.gsa_id}-${i}`} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`sap-receipt-dates-row-${i}`}>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">{row.gsa_id || "—"}</td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{row.posting_date || "—"}</td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">
                              {withVendorCode(nameForSapId(row.supplier_internal_id), row.supplier_internal_id)}
                            </td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{row.po_id || "—"}</td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums text-[#475467]">
                              {row.quantity != null ? `${row.quantity.toLocaleString()} ${row.unit_of_measure || ""}` : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="px-2.5 pt-2.5 pb-1.5 flex items-center gap-1.5 justify-between flex-wrap">
                <div className="flex items-center gap-1.5">
                  <Badge variant="outline" className="bg-[#EEF4FF] text-[#4338CA] border-[#C7D2FE] text-xs inline-flex items-center gap-1">
                    <Sparkle size={11} weight="fill" /> AI
                  </Badge>
                  <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                    Quota Arrangement (replaces manual assignments - AI-suggested, buyer-adjustable)
                  </span>
                  {quotaArrangementId && (
                    <span className="text-xs text-[#667085]" data-testid="quota-arrangement-id">
                      {quotaArrangementId} · revision {quotaRevisionNo}
                    </span>
                  )}
                </div>
                {quotaRevisions.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setShowQuotaHistory((v) => !v)}
                    className="text-xs text-[#004B87] hover:underline inline-flex items-center gap-1"
                    data-testid="toggle-quota-history-button"
                  >
                    <ClockCounterClockwise size={13} />
                    {showQuotaHistory ? "Hide" : "Show"} Revision History ({quotaRevisions.length})
                  </button>
                )}
              </div>

              {showQuotaHistory && (
                <div className="mx-2.5 mb-2 border border-[#D0D5DD] rounded-sm overflow-hidden overflow-x-auto" data-testid="quota-history-panel">
                  <table className="w-full text-[13px] border-collapse">
                    <thead>
                      <tr>
                        <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Rev</th>
                        <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Date</th>
                        <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Source</th>
                        <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">By</th>
                        <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Remarks</th>
                        <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Allocations</th>
                      </tr>
                    </thead>
                    <tbody>
                      {quotaRevisions.map((r, i) => (
                        <tr key={r.id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`quota-history-row-${r.revision_no}`}>
                          <td className="border border-[#D0D5DD] px-1.5 py-1 font-bold text-[#101828]">{r.revision_no}</td>
                          <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{new Date(r.created_at).toLocaleString()}</td>
                          <td className="border border-[#D0D5DD] px-1.5 py-1">
                            <Badge variant="outline" className={r.source === "ai" ? "bg-[#EEF4FF] text-[#4338CA] border-[#C7D2FE] text-xs" : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs"}>
                              {r.source === "ai" ? "AI" : "Buyer"}
                            </Badge>
                          </td>
                          <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{r.created_by || "—"}</td>
                          <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{r.remarks || "—"}</td>
                          <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">
                            {r.allocations.map((a) => `${a.supplier_name} ${a.quota_percent}%`).join(", ")}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="px-2.5 pb-3">
                {quotaLoading || quotaSuggesting ? (
                  <div className="py-6 text-center text-[13px] text-[#475467] flex items-center justify-center gap-2" data-testid="quota-loading-state">
                    <Sparkle size={14} className="animate-pulse text-[#4338CA]" />
                    {quotaSuggesting ? "Generating AI suggestion (weighing price + lead time)..." : "Loading..."}
                  </div>
                ) : quotaError ? (
                  <div className="text-[13px] text-[#B42318] py-2" data-testid="quota-error">{quotaError}</div>
                ) : (
                  <>
                    <div className="overflow-x-auto">
                      <table className="w-full text-[13px] border-collapse block md:table" data-testid="quota-allocation-table">
                        <thead className="hidden md:table-header-group">
                          <tr>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase w-24">Quota %</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase w-28">Lead Time (d)</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">Price</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Rationale</th>
                            <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 w-8"></th>
                          </tr>
                        </thead>
                        <tbody className="block md:table-row-group">
                          {quotaAllocations.length === 0 ? (
                            <tr className="block md:table-row">
                              <td colSpan={6} className="block md:table-cell border border-[#D0D5DD] text-center py-6 text-[13px] text-[#475467]" data-testid="quota-allocation-empty">
                                No known suppliers found for "{activeProductId}" yet - use "+ Add Supplier" below.
                              </td>
                            </tr>
                          ) : (
                            quotaAllocations.map((a, i) => (
                              <tr
                                key={i}
                                className={`flex flex-col md:table-row mb-3 md:mb-0 last:mb-0 rounded-lg md:rounded-none border md:border-0 border-[#D0D5DD] overflow-hidden shadow-sm md:shadow-none ${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"}`}
                                data-testid={`quota-allocation-row-${i}`}
                              >
                                <td
                                  data-label="Supplier"
                                  className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 font-medium text-[#101828] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                                >
                                  <span className="text-right md:text-left">
                                    {withVendorCode(a.supplier_name, a.sap_internal_id)}
                                    {a.price_source && <div className="text-xs text-[#98A2B3]">{a.price_source}</div>}
                                  </span>
                                </td>
                                <td
                                  data-label="Quota %"
                                  className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-1.5 md:px-1 md:py-1 before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                                >
                                  <input
                                    type="number"
                                    step="0.1"
                                    value={a.quota_percent}
                                    onChange={(e) => updateAllocation(i, "quota_percent", e.target.value)}
                                    className={`${inputCls} h-7 text-right w-24 md:w-full`}
                                    data-testid={`quota-percent-input-${i}`}
                                  />
                                </td>
                                <td
                                  data-label="Lead Time (d)"
                                  className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-1.5 md:px-1 md:py-1 before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                                >
                                  <input
                                    type="number"
                                    placeholder="—"
                                    value={a.lead_time_days ?? ""}
                                    onChange={(e) => updateAllocation(i, "lead_time_days", e.target.value === "" ? null : e.target.value)}
                                    className={`${inputCls} h-7 text-right w-24 md:w-full`}
                                    data-testid={`quota-lead-time-input-${i}`}
                                  />
                                </td>
                                <td
                                  data-label="Price"
                                  className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 md:text-right tabular-nums text-[#475467] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                                >
                                  {a.price != null ? `${a.currency || ""} ${a.price}` : "—"}
                                </td>
                                <td
                                  data-label="Rationale"
                                  className="flex md:table-cell justify-between items-center gap-3 border-b md:border border-[#D0D5DD] px-3 py-2 md:px-2 md:py-1 text-xs text-[#667085] before:content-[attr(data-label)] before:font-bold before:text-[10px] before:uppercase before:text-[#667085] before:shrink-0 md:before:content-none"
                                >
                                  <span className="text-right md:text-left">{a.rationale || "—"}</span>
                                </td>
                                <td className="flex md:table-cell justify-end border-0 md:border border-[#D0D5DD] px-3 py-2 md:px-1 md:py-1 text-center">
                                  <button
                                    type="button"
                                    onClick={() => removeAllocationRow(i)}
                                    className="text-[#98A2B3] hover:text-[#B42318]"
                                    data-testid={`remove-quota-row-${i}`}
                                  >
                                    <XCircle size={15} />
                                  </button>
                                </td>
                              </tr>
                            ))
                          )}
                        </tbody>
                      </table>
                    </div>

                    <div className="flex items-center justify-between mt-2 flex-wrap gap-2">
                      <div className={`text-xs font-bold ${quotaTotalValid ? "text-[#027A48]" : "text-[#B42318]"}`} data-testid="quota-total-indicator">
                        Total: {quotaTotal.toFixed(1)}% {!quotaTotalValid && "(must equal 100%)"}
                      </div>
                      <Popover open={addSupplierPickerOpen} onOpenChange={setAddSupplierPickerOpen}>
                        <PopoverTrigger asChild>
                          <button
                            type="button"
                            className="text-xs text-[#004B87] hover:underline inline-flex items-center gap-1"
                            data-testid="open-add-quota-supplier-button"
                          >
                            <Plus size={12} /> Add Supplier
                          </button>
                        </PopoverTrigger>
                        <PopoverContent align="end" className="w-72 p-0" data-testid="add-quota-supplier-popover">
                          <Command shouldFilter={true}>
                            <CommandInput
                              placeholder="Search suppliers by name..."
                              className="text-[13px]"
                              data-testid="add-quota-supplier-search-input"
                              autoFocus
                            />
                            <CommandList>
                              <CommandEmpty className="text-[13px] text-[#98A2B3] py-4 text-center">No supplier found.</CommandEmpty>
                              {(() => {
                                const { recommended, others } = getRecommendedSuppliers();
                                return (
                                  <>
                                    {recommended.length > 0 && (
                                      <CommandGroup heading="Recommended (SAP/ERP price on file)">
                                        {recommended.map((s) => (
                                          <CommandItem
                                            key={s.id}
                                            value={s.name}
                                            onSelect={() => addAllocationRow(s.id)}
                                            className="text-[13px] cursor-pointer"
                                            data-testid={`add-quota-supplier-option-${s.id}`}
                                          >
                                            {s.name}
                                          </CommandItem>
                                        ))}
                                      </CommandGroup>
                                    )}
                                    {others.length > 0 && (
                                      <CommandGroup heading={recommended.length > 0 ? "All Suppliers" : undefined}>
                                        {others.map((s) => (
                                          <CommandItem
                                            key={s.id}
                                            value={s.name}
                                            onSelect={() => addAllocationRow(s.id)}
                                            className="text-[13px] cursor-pointer"
                                            data-testid={`add-quota-supplier-option-${s.id}`}
                                          >
                                            {s.name}
                                          </CommandItem>
                                        ))}
                                      </CommandGroup>
                                    )}
                                  </>
                                );
                              })()}
                            </CommandList>
                          </Command>
                        </PopoverContent>
                      </Popover>
                    </div>

                    {quotaOverallRationale && (
                      <div className="text-xs text-[#667085] italic mt-1.5" data-testid="quota-overall-rationale">{quotaOverallRationale}</div>
                    )}

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-3">
                      <div>
                        <label className={labelCls}>Remarks (why this split / change)</label>
                        <textarea
                          value={quotaRemarks}
                          onChange={(e) => setQuotaRemarks(e.target.value)}
                          rows={2}
                          className={`${inputCls} mt-1 h-auto py-1.5`}
                          data-testid="quota-remarks-input"
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Set By</label>
                        <input
                          type="text"
                          value={quotaCreatedBy}
                          onChange={(e) => setQuotaCreatedBy(e.target.value)}
                          placeholder="Your name"
                          className={`${inputCls} mt-1`}
                          data-testid="quota-created-by-input"
                        />
                      </div>
                    </div>

                    <div className="flex items-center gap-2 mt-3">
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => suggestQuota(activeProductId)}
                        disabled={quotaSuggesting}
                        className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
                        data-testid="regenerate-quota-suggestion-button"
                      >
                        <ArrowsClockwise size={13} className="mr-1.5" />
                        Regenerate AI Suggestion
                      </Button>
                      <Button
                        type="button"
                        onClick={confirmQuotaArrangement}
                        disabled={quotaSaving || !quotaTotalValid || quotaAllocations.length === 0}
                        className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm"
                        data-testid="confirm-quota-arrangement-button"
                      >
                        {quotaSaving ? "Saving..." : "Confirm Quota Arrangement"}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
