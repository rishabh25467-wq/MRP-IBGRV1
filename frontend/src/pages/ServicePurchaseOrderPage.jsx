import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";
import {
  Trash, WarningCircle, CheckCircle, CircleNotch, MagnifyingGlass,
  Buildings, CreditCard, Calendar, Truck, ArrowRight, ShieldCheck, ListChecks,
  FileMagnifyingGlass, XCircle,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { Shield } from "@phosphor-icons/react";

// Sep 14 2026, user's explicit ask: "create a copy form of create
// purchase order then create a separate Name of: Service Purchase
// Order... we will apply some changes" later. This page is a FULLY
// INDEPENDENT copy of PurchaseOrderPage.jsx (own backend endpoints
// under /service-purchase-orders/*, own history collection) so future
// changes here never touch the real Create Purchase Order flow.

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const todayISO = () => new Date().toISOString().slice(0, 10);

const RI_SITES = new Set(["P1", "P8", "P5", "P1W", "W1"]);
const companyForSite = (site) => (RI_SITES.has((site || "").toUpperCase()) ? "RI" : "RT");

const BILL_TO_OPTIONS_BY_COMPANY = {
  RI: ["P1-FIN", "P8-FIN", "P1W-FIN", "P5-FIN"],
  RT: ["P2-FIN", "P3-FIN", "P2W-FIN", "P7-FIN", "P9-FIN", "P4-FIN"],
};

const fmtMoney = (amount, ccy) =>
  new Intl.NumberFormat(ccy === "USD" ? "en-US" : "en-IN", { style: "currency", currency: ccy === "USD" ? "USD" : "INR", maximumFractionDigits: 2 }).format(amount || 0);

const emptyLine = () => ({
  key: `line-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  description: "",
  gl_account_code: "",
  glAccountQuery: "",
  hsn_code: "",
  unit_of_measure: "EA",
  uomFromPr: null,
  uomMappingConfident: true,
  fromPr: false,
  prLineNo: null,
  prOriginalQty: null,
  quantity: "",
  unit_price: "",
  delivery_date: todayISO(),
  showGlSuggestions: false,
  // Sep 14 2026, Job Work PO ask - the finished good the vendor returns
  // after doing job work. NOT sent to SAP yet (see submit()'s note) -
  // captured for our own records only until we get the real custom
  // field ID from the user's SAP team.
  output_product_id: "",
  outputProductQuery: "",
  output_product_description: "",
  output_quantity: "",
  showOutputSuggestions: false,
});

const PO_TYPE_OPTIONS = [
  { value: "service", label: "Service", productCategory: "CONSUMABLES" },
  { value: "jobwork", label: "Job Work", productCategory: "JOBWORK" },
  { value: "capital", label: "Capital", productCategory: "FIXED_ASSETS" },
];

export default function ServicePurchaseOrderPage() {
  const navigate = useNavigate();
  const [poType, setPoType] = useState("service");
  const [sites, setSites] = useState([]);
  const [purchaseUnitSite, setPurchaseUnitSite] = useState("");
  const [billToCompany, setBillToCompany] = useState("");
  const [poDate, setPoDate] = useState(todayISO());
  const [currency, setCurrency] = useState("INR");

  const [prVocNo, setPrVocNo] = useState("");
  const [prFetching, setPrFetching] = useState(false);
  const [prFetched, setPrFetched] = useState(null);
  const [prSuggestions, setPrSuggestions] = useState([]);
  const [showPrSuggestions, setShowPrSuggestions] = useState(false);
  const prWrapperRef = useRef(null);
  const prDebounceRef = useRef(null);

  const [supplierQuery, setSupplierQuery] = useState("");
  const [supplierSuggestions, setSupplierSuggestions] = useState([]);
  const [showSupplierSuggestions, setShowSupplierSuggestions] = useState(false);
  const [selectedSupplier, setSelectedSupplier] = useState(null);
  const supplierWrapperRef = useRef(null);
  const supplierDebounceRef = useRef(null);
  const glWrapperRefs = useRef({});

  const [lines, setLines] = useState([emptyLine()]);
  const [glAccounts, setGlAccounts] = useState([]);
  const outputProductDebounceRef = useRef({});
  const outputWrapperRefs = useRef({});

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null); // { po_number } | { error }

  useEffect(() => {
    axios.get(`${API}/service-purchase-orders/sites`).then((r) => setSites(r.data.sites || [])).catch(() => toast.error("Could not load sites"));
    // GL Account list is small (~284, rarely changes) - fetch once, filter client-side per line.
    axios.get(`${API}/service-purchase-orders/gl-accounts`).then((r) => setGlAccounts(r.data || [])).catch(() => toast.error("Could not load GL Account list"));
  }, []);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (supplierWrapperRef.current && !supplierWrapperRef.current.contains(e.target)) setShowSupplierSuggestions(false);
      if (prWrapperRef.current && !prWrapperRef.current.contains(e.target)) setShowPrSuggestions(false);
      Object.entries(glWrapperRefs.current).forEach(([key, el]) => {
        if (el && !el.contains(e.target)) setLines((prev) => prev.map((l) => (l.key === key ? { ...l, showGlSuggestions: false } : l)));
      });
      Object.entries(outputWrapperRefs.current).forEach(([key, el]) => {
        if (el && !el.contains(e.target)) setLines((prev) => prev.map((l) => (l.key === key ? { ...l, showOutputSuggestions: false } : l)));
      });
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  // Sep 14 2026, Job Work PO ask - "then all page load" per type. A
  // switch reshapes which fields matter (Product Category, Account
  // Assignment, Output Product), so the safest thing is a clean reset
  // rather than carrying over lines shaped for a different type.
  const changePoType = (v) => {
    setPoType(v);
    setPrVocNo(""); setPrFetched(null);
    setSelectedSupplier(null); setSupplierQuery("");
    setPurchaseUnitSite(""); setBillToCompany("");
    setLines([emptyLine()]);
    setResult(null);
  };

  const onOutputProductQueryChange = (lineKey, v) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, outputProductQuery: v, output_product_id: "", showOutputSuggestions: true } : l)));
    clearTimeout(outputProductDebounceRef.current[lineKey]);
    if (!v.trim()) return;
    outputProductDebounceRef.current[lineKey] = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/purchase-orders/products/search`, { params: { q: v, limit: 15 } });
        setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, outputProductSuggestions: data } : l)));
      } catch { /* silent - user can keep typing */ }
    }, 300);
  };

  const pickOutputProduct = (lineKey, p) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey
      ? { ...l, output_product_id: p.product_id, outputProductQuery: `${p.product_id} - ${p.description || ""}`, output_product_description: l.output_product_description || p.description || "", showOutputSuggestions: false }
      : l)));
  };

  const onPrQueryChange = (v) => {
    setPrVocNo(v);
    setShowPrSuggestions(true);
    clearTimeout(prDebounceRef.current);
    if (!v.trim()) { setPrSuggestions([]); return; }
    prDebounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/service-purchase-orders/pr-available`, { params: { search: v, limit: 20 } });
        setPrSuggestions(data);
      } catch { /* silent - user can keep typing or press Fetch PR */ }
    }, 300);
  };

  const fetchPR = async (vocOverride) => {
    const voc = (vocOverride ?? prVocNo).trim();
    if (!voc) { toast.error("Enter a PR Number first"); return; }
    setShowPrSuggestions(false);
    setPrFetching(true);
    try {
      const { data } = await axios.get(`${API}/service-purchase-orders/pr-lookup/${encodeURIComponent(voc)}`);
      setPrFetched(data);
      setCurrency(data.currency || "INR");
      const prSite = (data.compcode && sites.includes(data.compcode)) ? data.compcode : "";
      setPurchaseUnitSite(prSite);
      if (prSite) {
        const prCompany = companyForSite(prSite);
        const billToOpt = `${prSite}-FIN`;
        setBillToCompany((BILL_TO_OPTIONS_BY_COMPANY[prCompany] || []).includes(billToOpt) ? billToOpt : "");
      } else {
        setBillToCompany("");
      }
      if (data.supplier_code) {
        setSelectedSupplier({
          supplier_code: data.supplier_code,
          name: data.supplier_name || data.supplier_code,
          cash_discount_terms_code: data.supplier_cash_discount_terms_code,
          cash_discount_terms_text: data.supplier_cash_discount_terms_text,
        });
        setSupplierQuery(`${data.supplier_code} - ${data.supplier_name || ""}`);
        if (!data.supplier_known) toast.warning("This PR's vendor isn't in the SAP Supplier Master yet - please confirm or search manually");
      } else {
        setSelectedSupplier(null);
        setSupplierQuery("");
      }
      setLines((data.items || []).map((it) => ({
        key: `line-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        description: it.iname || "",
        gl_account_code: "",
        glAccountQuery: "",
        hsn_code: "",
        unit_of_measure: it.sap_unit_of_measure || "EA",
        uomFromPr: it.unit,
        uomMappingConfident: it.unit_mapping_confident,
        fromPr: true,
        prLineNo: it.line_no,
        prOriginalQty: it.qty,
        quantity: it.qty,
        unit_price: it.rate,
        delivery_date: todayISO(),
        showGlSuggestions: false,
        output_product_id: "", outputProductQuery: "", output_product_description: "", output_quantity: "", showOutputSuggestions: false,
      })));
      toast.success(`PR ${data.voc_no} fetched - ${(data.items || []).length} line item(s) autofilled`);
    } catch (e) {
      setPrFetched(null);
      const detail = e?.response?.data?.detail;
      toast.error(typeof detail === "string" && detail.trim() ? detail : "Could not fetch PR");
    } finally {
      setPrFetching(false);
    }
  };

  const clearPR = () => {
    setPrFetched(null); setPrVocNo(""); setSelectedSupplier(null); setSupplierQuery("");
    setPurchaseUnitSite(""); setBillToCompany(""); setLines([emptyLine()]);
  };

  const onSupplierQueryChange = (v) => {
    setSupplierQuery(v);
    setSelectedSupplier(null);
    setShowSupplierSuggestions(true);
    clearTimeout(supplierDebounceRef.current);
    if (!v.trim()) { setSupplierSuggestions([]); return; }
    supplierDebounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/service-purchase-orders/suppliers/search`, { params: { q: v, limit: 15 } });
        setSupplierSuggestions(data);
      } catch { /* silent - user can keep typing */ }
    }, 300);
  };

  const pickSupplier = (s) => {
    setSelectedSupplier(s);
    setSupplierQuery(`${s.supplier_code} - ${s.name}`);
    setShowSupplierSuggestions(false);
  };

  const onGlAccountQueryChange = (lineKey, v) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, glAccountQuery: v, gl_account_code: "", showGlSuggestions: true } : l)));
  };

  const pickGlAccount = (lineKey, a) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey
      ? { ...l, gl_account_code: a.code, glAccountQuery: `${a.code} - ${a.description}`, showGlSuggestions: false }
      : l)));
  };

  const glAccountMatches = (query) => {
    const q = (query || "").trim().toLowerCase();
    if (!q) return glAccounts.slice(0, 20);
    return glAccounts.filter((a) => a.code.toLowerCase().includes(q) || a.description.toLowerCase().includes(q)).slice(0, 20);
  };

  const updateLine = (lineKey, field, value) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, [field]: value } : l)));
  };

  const removeLine = (lineKey) => setLines((prev) => (prev.length > 1 ? prev.filter((l) => l.key !== lineKey) : prev));

  const lineTotal = (l) => (Number(l.quantity) || 0) * (Number(l.unit_price) || 0);
  const totalUnits = lines.reduce((s, l) => s + (Number(l.quantity) || 0), 0);
  const grandTotal = lines.reduce((s, l) => s + lineTotal(l), 0);

  const validationErrors = () => {
    const errors = [];
    if (!prFetched) errors.push("A valid PR must be fetched first");
    if (!purchaseUnitSite) errors.push("Purchase Unit (Site) is required");
    if (!billToCompany) errors.push("Bill-To Company is required");
    if (!selectedSupplier) errors.push("Supplier must be selected from the list");
    if (!poDate) errors.push("PO Date is required");
    if (lines.length === 0) errors.push("At least one line item is required");
    lines.forEach((l, idx) => {
      if (!l.description.trim()) errors.push(`Line ${idx + 1}: Description is required`);
      if (!l.gl_account_code) errors.push(`Line ${idx + 1}: GL Account must be selected from the list`);
      if (!l.quantity || Number(l.quantity) <= 0) errors.push(`Line ${idx + 1}: Quantity must be greater than 0`);
      if (l.unit_price === "" || Number(l.unit_price) < 0) errors.push(`Line ${idx + 1}: Unit Price must be 0 or more`);
      if (!l.delivery_date) errors.push(`Line ${idx + 1}: Delivery Date is required`);
      if (l.delivery_date && poDate && l.delivery_date < poDate) errors.push(`Line ${idx + 1}: Delivery Date cannot be before PO Date`);
    });
    return errors;
  };

  const checklist = [
    { label: "PR fetched & line items autofilled", ok: !!prFetched },
    { label: "Purchase Unit & Bill-To selected", ok: !!purchaseUnitSite && !!billToCompany },
    { label: "Supplier selected from SAP Master", ok: !!selectedSupplier },
    { label: "Every line has a Description, GL Account, Qty & Price", ok: lines.every((l) => l.description.trim() && l.gl_account_code && Number(l.quantity) > 0 && l.unit_price !== "") },
    { label: "Delivery dates on/after PO Date", ok: lines.every((l) => !l.delivery_date || !poDate || l.delivery_date >= poDate) },
  ];
  const canSubmit = checklist.every((c) => c.ok);

  const openConfirm = () => {
    const errors = validationErrors();
    if (errors.length > 0) {
      toast.error(errors[0], { description: errors.length > 1 ? `+${errors.length - 1} more issue(s)` : undefined });
      return;
    }
    setConfirmOpen(true);
  };

  const submit = async () => {
    setSubmitting(true);
    try {
      const payload = {
        po_type: poType,
        supplier_code: selectedSupplier.supplier_code,
        purchase_unit_site: purchaseUnitSite,
        bill_to_company: billToCompany,
        po_date: poDate,
        currency,
        pr_number: prFetched.voc_no,
        items: lines.map((l) => ({
          description: l.description.trim(), gl_account_code: l.gl_account_code,
          hsn_code: l.hsn_code ? l.hsn_code.trim() : null,
          quantity: Number(l.quantity), unit_of_measure: l.unit_of_measure || "EA",
          unit_price: Number(l.unit_price), delivery_date: l.delivery_date,
          output_product_id: l.output_product_id || null,
          output_product_description: l.output_product_description ? l.output_product_description.trim() : null,
          output_quantity: l.output_quantity !== "" && l.output_quantity != null ? Number(l.output_quantity) : null,
        })),
      };
      const { data } = await axios.post(`${API}/service-purchase-orders/create`, payload);
      setResult({ po_number: data.po_number, sap_po_number: data.sap_po_number, outputSavedLocallyOnly: data.output_products_saved_locally_only });
      setConfirmOpen(false);
      toast.success(`Service Purchase Order ${data.po_number} created in SAP`, { duration: 10000 });
    } catch (e) {
      const detail = e?.response?.data?.detail;
      setResult({ error: typeof detail === "string" && detail.trim() ? detail : "Failed to create Service Purchase Order in SAP" });
      setConfirmOpen(false);
    } finally {
      setSubmitting(false);
    }
  };

  const resetForm = () => {
    setPurchaseUnitSite(""); setBillToCompany(""); setPoDate(todayISO());
    clearPR();
    setResult(null);
  };

  const company = companyForSite(purchaseUnitSite);
  const inputCls = "h-9 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]";
  const cardCls = "bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] p-4 space-y-3";
  const sectionHeadingCls = "text-[11px] font-bold uppercase tracking-wide text-[#344054] font-heading mb-1 flex items-center gap-2";

  return (
    <div className="min-h-screen bg-[#F2F4F7] flex flex-col font-sans">
      <Toaster position="top-right" />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-sm bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Service Purchase Order Creation</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto w-full max-w-[1400px] mx-auto px-4 sm:px-6 lg:px-8 py-4">
        <div className="flex items-center justify-between flex-wrap gap-3 mb-4">
          <div>
            <h1 className="font-heading text-xl sm:text-2xl font-bold text-[#101828] tracking-tight flex items-center gap-2" data-testid="service-po-page-title">
              <ShieldCheck size={20} className="text-[#004B87]" weight="fill" />
              Service Purchase Order Creation
            </h1>
            <p className="text-sm text-[#475467] mt-0.5">Builds and pushes a live Service Purchase Order into SAP Business ByDesign - submitted the moment you confirm below.</p>
          </div>
          <Badge className="bg-[#EFF8FF] text-[#175CD3] border border-[#B2DDFF] rounded-sm font-data text-xs px-3 py-1.5" data-testid="service-po-live-sap-badge">
            <span className="w-1.5 h-1.5 rounded-full bg-[#175CD3] mr-2 inline-block animate-pulse" />
            LIVE SAP OData Write Mode
          </Badge>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
          {/* MAIN FORM */}
          <div className="lg:col-span-8 xl:col-span-9 space-y-4">
            {/* 1. PO Type - determines Product Category, Account Assignment & Output fields */}
            <div className={cardCls} data-testid="service-po-type-card">
              <h2 className={sectionHeadingCls}>
                <ListChecks size={14} /> 1. Purchase Order Type
              </h2>
              <div className="flex flex-wrap gap-2" data-testid="service-po-type-select">
                {PO_TYPE_OPTIONS.map((opt) => {
                  const disabled = opt.value === "capital" || opt.value === "jobwork";
                  return (
                  <button
                    key={opt.value}
                    type="button"
                    disabled={disabled}
                    onClick={() => changePoType(opt.value)}
                    title={
                      opt.value === "capital" ? "Fixed Asset selection for Capital POs is coming soon"
                        : opt.value === "jobwork" ? "Paused - waiting on SAP technical field IDs for Purchase Order Type & Output Product (see your SAP admin)"
                        : undefined
                    }
                    className={`h-9 px-4 rounded-sm text-sm font-semibold border transition-colors ${
                      poType === opt.value
                        ? "bg-[#004B87] text-white border-[#004B87]"
                        : disabled
                          ? "bg-[#F9FAFB] text-[#98A2B3] border-[#EAECF0] cursor-not-allowed"
                          : "bg-white text-[#344054] border-[#D0D5DD] hover:bg-[#F2F4F7]"
                    }`}
                    data-testid={`service-po-type-option-${opt.value}`}
                  >
                    {opt.label}{opt.value === "capital" ? " (Coming Soon)" : opt.value === "jobwork" ? " (Paused)" : ""}
                  </button>
                  );
                })}
              </div>
              <p className="text-[11px] text-[#98A2B3]">
                {poType === "jobwork"
                  ? "Job Work: Product Category JOBWORK · GL Account + Cost Center entered manually below, same as Service."
                  : poType === "capital"
                    ? "Capital: Product Category FIXED_ASSETS with a Fixed Asset (Individual Material) picker - not available yet."
                    : "Service: Product Category CONSUMABLES · GL Account + Cost Center entered manually below."}
              </p>
              {(poType === "jobwork") && (
                <p className="text-[11px] text-[#B54708] bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-2">
                  Job Work is paused - "Purchase Order Type" and "Output Product" need technical field IDs from your SAP admin (via Adapt UI &rarr; "Show Technical Help") before this can push fully to SAP without manual work.
                </p>
              )}
            </div>

            {/* 2. PR Lookup - mandatory entry point */}
            <div className={cardCls} data-testid="service-po-pr-lookup-card">
              <h2 className={sectionHeadingCls}>
                <FileMagnifyingGlass size={14} /> 2. Purchase Requisition Lookup (Mandatory)
              </h2>
              {!prFetched ? (
                <div className="flex flex-col sm:flex-row gap-3 items-end">
                  <div className="space-y-1.5 flex-1 w-full relative" ref={prWrapperRef}>
                    <Label className="text-xs font-medium text-[#344054]">PR Number, Vendor Name or Code *</Label>
                    <Input
                      value={prVocNo}
                      onChange={(e) => onPrQueryChange(e.target.value)}
                      onFocus={() => setShowPrSuggestions(true)}
                      onKeyDown={(e) => e.key === "Enter" && fetchPR()}
                      placeholder="e.g. 124722, or search by vendor..."
                      className={`${inputCls} font-data`}
                      data-testid="service-po-pr-number-input"
                    />
                    {showPrSuggestions && prSuggestions.length > 0 && (
                      <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-64 overflow-y-auto" data-testid="service-po-pr-suggestions">
                        {prSuggestions.map((pr) => (
                          <button
                            key={pr.voc_no}
                            type="button"
                            className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0 flex items-center justify-between gap-2"
                            onClick={() => { setPrVocNo(pr.voc_no); fetchPR(pr.voc_no); }}
                            data-testid={`service-po-pr-suggestion-${pr.voc_no}`}
                          >
                            <span>
                              <span className="font-semibold text-[#101828] font-data">PR {pr.voc_no}</span>
                              <span className="text-[#667085]"> · {pr.supplier_name || "-"} ({pr.supplier_code || "-"}) · {pr.compcode}</span>
                            </span>
                            <span className="font-data font-semibold text-[#004B87] shrink-0">{fmtMoney(pr.amount, pr.currency)}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  <Button type="button" className="h-9 rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D] shrink-0" onClick={() => fetchPR()} disabled={prFetching} data-testid="service-po-pr-fetch-button">
                    {prFetching ? <><CircleNotch size={14} className="mr-1.5 animate-spin" /> Fetching...</> : <><FileMagnifyingGlass size={14} className="mr-1.5" /> Fetch PR</>}
                  </Button>
                </div>
              ) : (
                <div className="bg-[#ECFDF3] border border-[#ABEFC6] rounded-sm p-3 flex items-start justify-between gap-4" data-testid="service-po-pr-summary">
                  <div className="space-y-1 text-xs">
                    <div className="flex items-center gap-2">
                      <CheckCircle size={15} className="text-[#027A48]" weight="fill" />
                      <span className="font-data font-bold text-[#101828]">PR {prFetched.voc_no}</span>
                      <Badge className="bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6] rounded-sm text-[10px]">Approved</Badge>
                    </div>
                    <p className="text-[#344054] font-data">
                      Vendor: <b>{prFetched.supplier_name || "-"}</b> ({prFetched.supplier_code || "-"}) · Purchase Unit: <b>{prFetched.compcode}</b> · {(prFetched.items || []).length} line item(s) · {fmtMoney(prFetched.amount, prFetched.currency)}
                    </p>
                  </div>
                  <Button type="button" variant="ghost" size="sm" className="h-8 text-xs text-[#667085] hover:bg-[#D0D5DD]/40 hover:text-[#344054] rounded-sm" onClick={clearPR} data-testid="service-po-pr-clear-button">
                    <XCircle size={14} className="mr-1" /> Change PR
                  </Button>
                </div>
              )}
              <p className="text-[11px] text-[#98A2B3]">Vendor, Purchase Unit, Bill-To and Line Items are autofilled from the PR. Delivery Date isn't part of a PR - enter it per line below.</p>
            </div>

            {/* 3. Org context */}
            <div className={cardCls} data-testid="service-po-org-context-card">
              <h2 className={sectionHeadingCls}>
                <Buildings size={14} /> 3. Organization &amp; Routing Context
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054]">Purchase Unit (Site) *</Label>
                  <div data-testid="service-po-purchase-unit-select">
                    {purchaseUnitSite ? (
                      <Badge className="h-9 w-full flex items-center justify-center bg-[#F2F4F7] text-[#004B87] border border-[#D0D5DD] rounded-sm font-data text-sm">
                        {purchaseUnitSite} · Auto-derived
                      </Badge>
                    ) : (
                      <div className="h-9 flex items-center px-3 rounded-sm border border-dashed border-[#D0D5DD] bg-[#F9FAFB] text-sm text-[#98A2B3]">
                        Auto-set from PR
                      </div>
                    )}
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054]">Company (Business Residence)</Label>
                  <div data-testid="service-po-company-display">
                    {purchaseUnitSite ? (
                      <Badge className="h-9 w-full flex items-center justify-center bg-[#F2F4F7] text-[#004B87] border border-[#D0D5DD] rounded-sm font-data text-sm">
                        {company} · Auto-derived
                      </Badge>
                    ) : (
                      <div className="h-9 flex items-center px-3 rounded-sm border border-dashed border-[#D0D5DD] bg-[#F9FAFB] text-sm text-[#98A2B3]">
                        Auto-set from Purchase Unit
                      </div>
                    )}
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054]">Bill-To *</Label>
                  <div data-testid="service-po-bill-to-select">
                    {billToCompany ? (
                      <Badge className="h-9 w-full flex items-center justify-center bg-[#F2F4F7] text-[#004B87] border border-[#D0D5DD] rounded-sm font-data text-sm">
                        {billToCompany} · Auto-derived
                      </Badge>
                    ) : (
                      <div className="h-9 flex items-center px-3 rounded-sm border border-dashed border-[#D0D5DD] bg-[#F9FAFB] text-sm text-[#98A2B3]">
                        Auto-set from Purchase Unit
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* 4. Supplier & commercial terms */}
            <div className={cardCls} data-testid="service-po-supplier-terms-card">
              <h2 className={sectionHeadingCls}>
                <CreditCard size={14} /> 4. Supplier Master &amp; Commercial Terms
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="space-y-1.5 relative md:col-span-2" ref={supplierWrapperRef}>
                  <Label className="text-xs font-medium text-[#344054]">Supplier *</Label>
                  <div className="relative">
                    <MagnifyingGlass size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                    <Input
                      value={supplierQuery}
                      onChange={(e) => onSupplierQueryChange(e.target.value)}
                      onFocus={() => setShowSupplierSuggestions(true)}
                      placeholder="Search SAP supplier by name or code..."
                      className={`${inputCls} pl-9`}
                      data-testid="service-po-supplier-search-input"
                    />
                  </div>
                  {showSupplierSuggestions && supplierSuggestions.length > 0 && (
                    <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid="service-po-supplier-suggestions">
                      {supplierSuggestions.map((s) => (
                        <button
                          key={s.supplier_code}
                          type="button"
                          className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0"
                          onClick={() => pickSupplier(s)}
                          data-testid={`service-po-supplier-suggestion-${s.supplier_code}`}
                        >
                          <span className="font-semibold text-[#101828] font-data">{s.supplier_code}</span>
                          <span className="text-[#667085]"> - {s.name}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  {selectedSupplier && (
                    <div className="pt-1">
                      <Badge className="bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89] rounded-sm font-data text-xs px-3 py-1.5" data-testid="service-po-payment-terms-badge">
                        Payment Terms (from Supplier Master): {selectedSupplier.cash_discount_terms_code
                          ? `${selectedSupplier.cash_discount_terms_code} - ${selectedSupplier.cash_discount_terms_text || "Unmapped code"}`
                          : "Not on file - SAP default will apply"}
                      </Badge>
                    </div>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054] flex items-center gap-1"><Calendar size={12} /> PO Date *</Label>
                  <Input type="date" value={poDate} onChange={(e) => setPoDate(e.target.value)} className={`${inputCls} font-data`} data-testid="service-po-date-input" />
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054]">Currency</Label>
                  <Select value={currency} onValueChange={setCurrency}>
                    <SelectTrigger className={inputCls} data-testid="service-po-currency-select">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="INR">INR</SelectItem>
                      <SelectItem value="USD">USD</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>

            {/* 5. Line items */}
            <div className="bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-hidden" data-testid="service-po-line-items-card">
              <div className="px-4 py-2.5 border-b border-[#D0D5DD] bg-[#F9FAFB] flex items-center justify-between">
                <h2 className="text-[11px] font-bold uppercase tracking-wide text-[#344054] font-heading flex items-center gap-2">
                  <Truck size={14} /> 5. Line Items Engine
                </h2>
              </div>
              <div className="px-4 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB] flex items-center gap-4 flex-wrap" data-testid="service-po-account-assignment-banner">
                <span className="text-[11px] text-[#667085]">
                  <span className="font-semibold text-[#344054]">Product Category:</span> <span className="font-data">{PO_TYPE_OPTIONS.find((o) => o.value === poType)?.productCategory}</span> (fixed for {PO_TYPE_OPTIONS.find((o) => o.value === poType)?.label} lines)
                </span>
                <span className="text-[11px] text-[#667085]">
                  <span className="font-semibold text-[#344054]">Account Assignment:</span> Cost Center <span className="font-data">{billToCompany || "(select Bill-To)"}</span> - same as Bill-To
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs border-collapse min-w-[980px]" data-testid="service-po-line-items-table">
                  <thead>
                    <tr>
                      {[
                        "Description", "HSN/SAC", "GL Account", "Qty", "UoM", "Unit Price", "Delivery Date",
                        ...(poType === "jobwork" ? ["O/P Qty", "O/P Product", "O/P Description"] : []),
                        "Line Total", "",
                      ].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] py-1.5 px-2.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((l, idx) => {
                      return (
                      <tr
                        key={l.key}
                        className={idx % 2 === 1 ? "bg-[#F9FAFB]" : "bg-white"}
                        data-testid={`service-po-line-row-${idx}`}
                      >
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[220px] relative">
                          <Input
                            value={l.description}
                            onChange={(e) => updateLine(l.key, "description", e.target.value)}
                            placeholder="e.g. SECURITY CHARGES"
                            className="h-8 text-xs rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                            data-testid={`service-po-line-description-input-${idx}`}
                          />
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[100px]">
                          <Input
                            value={l.hsn_code}
                            onChange={(e) => updateLine(l.key, "hsn_code", e.target.value)}
                            placeholder="Optional"
                            className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                            data-testid={`service-po-line-hsn-input-${idx}`}
                          />
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[220px] relative" ref={(el) => { glWrapperRefs.current[l.key] = el; }}>
                          <Input
                            value={l.glAccountQuery}
                            onChange={(e) => onGlAccountQueryChange(l.key, e.target.value)}
                            onFocus={() => setLines((prev) => prev.map((x) => (x.key === l.key ? { ...x, showGlSuggestions: true } : x)))}
                            placeholder="Search GL Account..."
                            className="h-8 text-xs rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                            data-testid={`service-po-line-gl-input-${idx}`}
                          />
                          {l.showGlSuggestions && (
                            <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid={`service-po-line-gl-suggestions-${idx}`}>
                              {glAccountMatches(l.glAccountQuery).map((a) => (
                                <button
                                  key={a.code}
                                  type="button"
                                  className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0"
                                  onClick={() => pickGlAccount(l.key, a)}
                                  data-testid={`service-po-line-gl-suggestion-${idx}-${a.code}`}
                                >
                                  <span className="font-semibold text-[#101828] font-data">{a.code}</span>
                                  <span className="text-[#667085]"> - {a.description}</span>
                                </button>
                              ))}
                              {glAccountMatches(l.glAccountQuery).length === 0 && (
                                <div className="px-3 py-2 text-xs text-[#98A2B3]">No matching GL Account</div>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[110px]">
                          <Input type="number" min="0" step="any" value={l.quantity} onChange={(e) => updateLine(l.key, "quantity", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`service-po-line-qty-input-${idx}`} />
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[130px]">
                          <div className="relative flex items-center gap-1">
                            <Input value={l.unit_of_measure} onChange={(e) => updateLine(l.key, "unit_of_measure", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`service-po-line-uom-input-${idx}`} title={l.uomFromPr && !l.uomMappingConfident ? `PR said "${l.uomFromPr}" - please verify the SAP unit code` : undefined} />
                            {l.uomFromPr && !l.uomMappingConfident && (
                              <WarningCircle size={12} className="absolute -top-1.5 -right-1.5 text-[#B54708]" weight="fill" data-testid={`service-po-line-uom-warning-${idx}`} />
                            )}
                          </div>
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[110px]">
                          <Input type="number" min="0" step="any" value={l.unit_price} onChange={(e) => updateLine(l.key, "unit_price", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`service-po-line-price-input-${idx}`} />
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[140px]">
                          <Input type="date" value={l.delivery_date} onChange={(e) => updateLine(l.key, "delivery_date", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`service-po-line-delivery-date-input-${idx}`} />
                        </td>
                        {poType === "jobwork" && (
                          <>
                            <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[100px]">
                              <Input type="number" min="0" step="any" value={l.output_quantity} onChange={(e) => updateLine(l.key, "output_quantity", e.target.value)} placeholder="Optional" className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`service-po-line-output-qty-input-${idx}`} />
                            </td>
                            <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[200px] relative" ref={(el) => { outputWrapperRefs.current[l.key] = el; }}>
                              <Input
                                value={l.outputProductQuery}
                                onChange={(e) => onOutputProductQueryChange(l.key, e.target.value)}
                                onFocus={() => setLines((prev) => prev.map((x) => (x.key === l.key ? { ...x, showOutputSuggestions: true } : x)))}
                                placeholder="Search product (optional)..."
                                className="h-8 text-xs rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                                data-testid={`service-po-line-output-product-input-${idx}`}
                              />
                              {l.showOutputSuggestions && (l.outputProductSuggestions || []).length > 0 && (
                                <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid={`service-po-line-output-suggestions-${idx}`}>
                                  {(l.outputProductSuggestions || []).map((p) => (
                                    <button
                                      key={p.product_id}
                                      type="button"
                                      className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0"
                                      onClick={() => pickOutputProduct(l.key, p)}
                                      data-testid={`service-po-line-output-suggestion-${idx}-${p.product_id}`}
                                    >
                                      <span className="font-semibold text-[#101828] font-data">{p.product_id}</span>
                                      <span className="text-[#667085]"> - {p.description}</span>
                                    </button>
                                  ))}
                                </div>
                              )}
                            </td>
                            <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[180px]">
                              <Input
                                value={l.output_product_description}
                                onChange={(e) => updateLine(l.key, "output_product_description", e.target.value)}
                                placeholder="e.g. Link Front-42"
                                className="h-8 text-xs rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
                                data-testid={`service-po-line-output-description-input-${idx}`}
                              />
                            </td>
                          </>
                        )}
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[110px] font-data text-xs font-semibold text-[#101828] whitespace-nowrap" data-testid={`service-po-line-total-${idx}`}>
                          {fmtMoney(lineTotal(l), currency)}
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 text-center whitespace-nowrap min-w-[90px]">
                          <Button type="button" variant="ghost" size="icon" className="h-7 w-7 rounded-sm hover:bg-[#FEF3F2]" onClick={() => removeLine(l.key)} disabled={lines.length === 1} data-testid={`service-po-line-remove-button-${idx}`} title="Remove row">
                            <Trash size={13} className="text-[#B42318]" />
                          </Button>
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {/* SUMMARY SIDEBAR */}
          <div className="lg:col-span-4 xl:col-span-3 space-y-4">
            <div className="sticky top-4 space-y-4">
              <div className={cardCls} data-testid="service-po-summary-card">
                <h2 className={sectionHeadingCls}>
                  <ListChecks size={14} /> 6. Order Summary
                </h2>
                <div className="space-y-1.5 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="text-[#667085] text-[13px]">Line Items</span>
                    <span className="font-data font-semibold text-[#101828]" data-testid="service-po-summary-line-count">{lines.length}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[#667085] text-[13px]">Total Units</span>
                    <span className="font-data font-semibold text-[#101828]" data-testid="service-po-summary-total-units">{totalUnits}</span>
                  </div>
                  <div className="h-px bg-[#EAECF0] my-1" />
                  <div className="flex items-center justify-between">
                    <span className="text-[#344054] font-semibold text-[13px]">Gross Order Value</span>
                    <span className="font-data font-extrabold text-base text-[#004B87]" data-testid="service-po-summary-total-value">{fmtMoney(grandTotal, currency)}</span>
                  </div>
                </div>

                <div className="h-px bg-[#EAECF0]" />

                <div className="space-y-1.5" data-testid="service-po-summary-checklist">
                  {checklist.map((c, i) => (
                    <div key={i} className="flex items-start gap-2 text-xs" data-testid={`service-po-checklist-item-${i}`}>
                      {c.ok ? <CheckCircle size={14} className="text-[#027A48] mt-0.5 shrink-0" weight="fill" /> : <WarningCircle size={14} className="text-[#B54708] mt-0.5 shrink-0" weight="fill" />}
                      <span className={c.ok ? "text-[#344054]" : "text-[#B54708]"}>{c.label}</span>
                    </div>
                  ))}
                </div>

                <Button
                  type="button"
                  className="w-full h-10 rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D] font-semibold"
                  onClick={openConfirm}
                  disabled={!canSubmit}
                  data-testid="service-po-submit-button"
                >
                  Create Service Purchase Order in SAP <ArrowRight size={15} className="ml-1.5" />
                </Button>
                <p className="text-[11px] text-[#98A2B3] text-center">This creates a real, live SAP document. Review the audit recap carefully.</p>
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* Confirm dialog - audit recap */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-2xl" data-testid="service-po-confirm-dialog">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><WarningCircle size={18} className="text-[#B54708]" /> Audit Recap - Confirm Service Purchase Order</DialogTitle>
            <DialogDescription>This will submit a real Purchase Order to SAP ByDesign - it cannot be undone from this app.</DialogDescription>
          </DialogHeader>
          <div className="text-sm space-y-3 text-[#344054]">
            <div className="grid grid-cols-2 gap-2 bg-[#F9FAFB] rounded-sm p-3 font-data text-xs">
              <div><span className="text-[#667085]">PO Type:</span> <b>{PO_TYPE_OPTIONS.find((o) => o.value === poType)?.label}</b></div>
              <div><span className="text-[#667085]">PR Number:</span> <b>{prFetched?.voc_no}</b></div>
              <div><span className="text-[#667085]">Company:</span> <b>{company}</b></div>
              <div><span className="text-[#667085]">Purchase Unit:</span> <b>{purchaseUnitSite}</b></div>
              <div><span className="text-[#667085]">Bill-To:</span> <b>{billToCompany}</b></div>
              <div><span className="text-[#667085]">PO Date:</span> <b>{poDate}</b></div>
              <div className="col-span-2"><span className="text-[#667085]">Supplier:</span> <b>{selectedSupplier ? `${selectedSupplier.supplier_code} - ${selectedSupplier.name}` : "-"}</b></div>
            </div>
            <div className="border border-[#D0D5DD] rounded-sm overflow-hidden">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-[#EAECF0]">
                    <th className="text-left p-2 font-heading uppercase text-[#344054]">Description</th>
                    <th className="text-left p-2 font-heading uppercase text-[#344054]">GL Account</th>
                    <th className="text-right p-2 font-heading uppercase text-[#344054]">Qty</th>
                    <th className="text-left p-2 font-heading uppercase text-[#344054]">Delivery</th>
                    {poType === "jobwork" && <th className="text-left p-2 font-heading uppercase text-[#344054]">Output Product</th>}
                    <th className="text-right p-2 font-heading uppercase text-[#344054]">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l, i) => (
                    <tr key={l.key} className="border-t border-[#D0D5DD]">
                      <td className="p-2 font-data">{l.description}</td>
                      <td className="p-2 font-data">{l.gl_account_code}</td>
                      <td className="p-2 text-right font-data">{l.quantity} {l.unit_of_measure}</td>
                      <td className="p-2 font-data">{l.delivery_date}</td>
                      {poType === "jobwork" && (
                        <td className="p-2 font-data text-[11px]">
                          {l.output_product_id || l.output_product_description
                            ? `${l.output_product_id || "-"} ${l.output_product_description ? `(${l.output_product_description})` : ""}${l.output_quantity ? ` x${l.output_quantity}` : ""}`
                            : "-"}
                        </td>
                      )}
                      <td className="p-2 text-right font-data font-semibold">{fmtMoney(lineTotal(l), currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex justify-between items-center text-[11px] text-[#667085] font-data">
              <span>Product Category: {PO_TYPE_OPTIONS.find((o) => o.value === poType)?.productCategory} &middot; Account Assignment: Cost Center {billToCompany} (100%)</span>
            </div>
            {poType === "jobwork" && lines.some((l) => l.output_product_id || l.output_product_description) && (
              <p className="text-[11px] text-[#B54708] bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-2" data-testid="service-po-output-product-disclaimer">
                Note: SAP doesn't currently expose a field for Output Product on this integration - these details will be saved in your records here, but not pushed to SAP. Please add them manually on the SAP PO screen for now.
              </p>
            )}
            <div className="flex justify-end font-data text-sm font-bold text-[#004B87]">Grand Total: {fmtMoney(grandTotal, currency)}</div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" className="rounded-sm" onClick={() => setConfirmOpen(false)} disabled={submitting} data-testid="service-po-confirm-cancel-button">Cancel</Button>
            <Button type="button" className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D]" onClick={submit} disabled={submitting} data-testid="service-po-confirm-submit-button">
              {submitting ? <><CircleNotch size={13} className="mr-1.5 animate-spin" /> Submitting to SAP...</> : "Confirm & Submit"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Result dialog */}
      <Dialog open={!!result} onOpenChange={(open) => !open && resetForm()}>
        <DialogContent data-testid="service-po-result-dialog">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {result?.po_number ? <CheckCircle size={18} className="text-[#027A48]" weight="fill" /> : <WarningCircle size={18} className="text-[#B42318]" weight="fill" />}
              {result?.po_number ? "Service Purchase Order Created" : "Service Purchase Order Failed"}
            </DialogTitle>
          </DialogHeader>
          {result?.po_number ? (
            <p className="text-sm text-[#344054]" data-testid="service-po-result-success-message">
              SAP Purchase Order <span className="font-data font-bold text-[#004B87]" data-testid="service-po-result-number">{result.po_number}</span> was created successfully.
              {result.sap_po_number && (
                <span className="block mt-1 text-xs text-[#475467]" data-testid="service-po-result-printed-number">
                  Printed PO #: <span className="font-data font-semibold">{result.sap_po_number}</span>
                </span>
              )}
              {result.outputSavedLocallyOnly && (
                <span className="block mt-2 text-xs text-[#B54708]" data-testid="service-po-result-output-disclaimer">
                  Output Product details were saved to your records only - please add them manually on the SAP PO screen too.
                </span>
              )}
            </p>
          ) : (
            <p className="text-sm text-[#B42318]" data-testid="service-po-result-error-message">{result?.error}</p>
          )}
          <DialogFooter>
            <Button type="button" className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D]" onClick={resetForm} data-testid="service-po-result-close-button">
              {result?.po_number ? "Create Another" : "Close"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
