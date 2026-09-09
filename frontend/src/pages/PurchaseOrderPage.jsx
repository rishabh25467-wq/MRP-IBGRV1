import { useState, useEffect, useRef, Fragment } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";
import {
  Trash, WarningCircle, CheckCircle, CircleNotch, MagnifyingGlass,
  Buildings, CreditCard, Calendar, Truck, ArrowRight, ShieldCheck, ListChecks,
  FileMagnifyingGlass, XCircle, CalendarPlus, Lightbulb, ArrowsLeftRight,
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

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const todayISO = () => new Date().toISOString().slice(0, 10);

// Aug 2026, user's explicit rule: Business Residence (Company) is fixed
// by the chosen Purchase Unit site, never picked independently - mirrors
// the exact SITE_TO_COMPANY mapping already used elsewhere in this app
// (sap_wip_clearing_client.py) - RI sites vs RT sites.
const RI_SITES = new Set(["P1", "P8", "P5", "P1W", "W1"]);
const companyForSite = (site) => (RI_SITES.has((site || "").toUpperCase()) ? "RI" : "RT");

// Sep 4 2026, user's explicit ask: Bill-To is a per-site Finance/Billing
// unit ("{site}-FIN"), scoped to whichever company the Purchase Unit
// site belongs to - mirrors backend's BILL_TO_OPTIONS_BY_COMPANY.
const BILL_TO_OPTIONS_BY_COMPANY = {
  RI: ["P1-FIN", "P8-FIN", "P1W-FIN", "P5-FIN"],
  RT: ["P2-FIN", "P3-FIN", "P2W-FIN", "P7-FIN", "P9-FIN", "P4-FIN"],
};

// Sep 4 2026, user's explicit ask: currency grouping must match the
// currency itself, not always Indian lakh-style grouping (e.g. USD
// amounts must read $761,840.00, not $7,61,840.00).
const fmtMoney = (amount, ccy) =>
  new Intl.NumberFormat(ccy === "USD" ? "en-US" : "en-IN", { style: "currency", currency: ccy === "USD" ? "USD" : "INR", maximumFractionDigits: 2 }).format(amount || 0);

const emptyLine = () => ({
  key: `line-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  product_id: "",
  description: "",
  unit_of_measure: "EA",
  uomFromPr: null,
  uomMappingConfident: true,
  // Sep 9 2026, user's explicit ask: on-demand alternate-UoM lookup
  // (e.g. "1 Packet = 100 EA") - null until the buyer clicks the fetch
  // icon next to the UOM field for this line.
  uomOptions: null,
  uomOptionsLoading: false,
  fromPr: false,
  prLineNo: null,
  prOriginalQty: null,
  quantity: "",
  unit_price: "",
  delivery_date: todayISO(),
  productQuery: "",
  productSuggestions: [],
  showSuggestions: false,
});

export default function PurchaseOrderPage() {
  const navigate = useNavigate();
  const [sites, setSites] = useState([]);
  const [purchaseUnitSite, setPurchaseUnitSite] = useState("");
  const [billToCompany, setBillToCompany] = useState("");
  const [poDate, setPoDate] = useState(todayISO());
  const [currency, setCurrency] = useState("INR");

  // Sep 4 2026, user's explicit ask: PR Number is now the MANDATORY entry
  // point - it looks up an already-approved PR from the external SCM.AI
  // "Smart Approvals" system and autofills Vendor/Purchase Unit/Line Items
  // (Delivery Date is NOT part of a PR, stays user-entered per line).
  const [prVocNo, setPrVocNo] = useState("");
  const [prFetching, setPrFetching] = useState(false);
  const [prFetched, setPrFetched] = useState(null);
  // Sep 4 2026, user's explicit ask: let the buyer FIND a PR by number,
  // vendor name or vendor code instead of typing a voc_no blind.
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
  const productDebounceRefs = useRef({});

  const [lines, setLines] = useState([emptyLine()]);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null); // { po_number } | { error }

  useEffect(() => {
    axios.get(`${API}/purchase-orders/sites`).then((r) => setSites(r.data.sites || [])).catch(() => toast.error("Could not load sites"));
  }, []);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (supplierWrapperRef.current && !supplierWrapperRef.current.contains(e.target)) setShowSupplierSuggestions(false);
      if (prWrapperRef.current && !prWrapperRef.current.contains(e.target)) setShowPrSuggestions(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const onPrQueryChange = (v) => {
    setPrVocNo(v);
    setShowPrSuggestions(true);
    clearTimeout(prDebounceRef.current);
    if (!v.trim()) { setPrSuggestions([]); return; }
    prDebounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/purchase-orders/pr-available`, { params: { search: v, limit: 20 } });
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
      const { data } = await axios.get(`${API}/purchase-orders/pr-lookup/${encodeURIComponent(voc)}`);
      setPrFetched(data);
      setCurrency(data.currency || "INR");
      // Sep 5 2026, user's explicit ask: Purchase Unit auto-fills from the
      // PR's own site, and Bill-To auto-fills to that same site's Finance
      // unit ("{site}-FIN") so the buyer never has to fill either manually.
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
        product_id: it.matched_product_id || "",
        description: it.matched_description || it.iname || "",
        unit_of_measure: it.matched_unit_of_measure || it.sap_unit_of_measure || "EA",
        uomFromPr: it.unit,
        uomMappingConfident: it.matched_unit_of_measure ? true : it.unit_mapping_confident,
        fromPr: true,
        // Sep 4 2026, user's explicit ask: allow splitting one PR line's
        // qty across several PO lines (a delivery schedule) - grouped by
        // prLineNo, capped at prOriginalQty so the split can never exceed
        // what the PR actually approved for that item.
        prLineNo: it.line_no,
        prOriginalQty: it.qty,
        quantity: it.qty,
        unit_price: it.rate,
        delivery_date: todayISO(),
        productQuery: it.matched_product_id ? `${it.matched_product_id} - ${it.matched_description || ""}` : (it.iname || ""),
        productSuggestions: [],
        showSuggestions: false,
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
        const { data } = await axios.get(`${API}/purchase-orders/suppliers/search`, { params: { q: v, limit: 15 } });
        setSupplierSuggestions(data);
      } catch { /* silent - user can keep typing */ }
    }, 300);
  };

  const pickSupplier = (s) => {
    setSelectedSupplier(s);
    setSupplierQuery(`${s.supplier_code} - ${s.name}`);
    setShowSupplierSuggestions(false);
  };

  const onProductQueryChange = (lineKey, v) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, productQuery: v, product_id: "", showSuggestions: true } : l)));
    clearTimeout(productDebounceRefs.current[lineKey]);
    if (!v.trim()) {
      setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, productSuggestions: [] } : l)));
      return;
    }
    productDebounceRefs.current[lineKey] = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/purchase-orders/products/search`, { params: { q: v, limit: 15 } });
        setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, productSuggestions: data } : l)));
      } catch { /* silent */ }
    }, 300);
  };

  const pickProduct = (lineKey, p) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey
      ? { ...l, product_id: p.product_id, description: p.description, unit_of_measure: p.unit_of_measure || "EA", uomMappingConfident: true, uomOptions: null, uomOptionsLoading: false, productQuery: `${p.product_id} - ${p.description || ""}`, showSuggestions: false }
      : l)));
  };

  // Sep 9 2026, user's explicit ask: "fetch secondary unit of the item
  // while creating PO" (e.g. 6550-002047, 1 Packet = 100 EA maintained
  // in SAP's own Material master) - on-demand only, per user's choice,
  // triggered by a small icon next to the UOM field rather than firing
  // automatically for every line item picked.
  const fetchUomOptions = async (lineKey, productId) => {
    if (!productId) return;
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, uomOptionsLoading: true } : l)));
    try {
      const { data } = await axios.get(`${API}/purchase-orders/products/${encodeURIComponent(productId)}/uom-options`);
      setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, uomOptions: data.options, uomOptionsLoading: false } : l)));
      if (data.options.length > 1) {
        toast.success(`${data.options.length} units available for ${productId}`, { description: data.options.map((o) => o.label).join(" · ") });
      } else {
        toast.info(`${productId} has only its base unit (${data.base_unit}) in SAP`, { description: "No alternate units/quantity conversions configured for this material." });
      }
    } catch (err) {
      setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, uomOptionsLoading: false } : l)));
      toast.error("Could not fetch units from SAP", { description: err?.response?.data?.detail || "Please try again" });
    }
  };

  const updateLine = (lineKey, field, value) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, [field]: value } : l)));
  };

  const removeLine = (lineKey) => setLines((prev) => (prev.length > 1 ? prev.filter((l) => l.key !== lineKey) : prev));
  const duplicateLine = (lineKey) => setLines((prev) => {
    const idx = prev.findIndex((l) => l.key === lineKey);
    if (idx < 0) return prev;
    // Sep 4 2026, user's explicit ask: duplicating a PR-sourced line is
    // how a buyer SPLITS that PR item's qty into a delivery schedule -
    // keep it linked to the same PR line (fromPr/prLineNo/prOriginalQty)
    // and same product, but blank the qty so the split amount is explicit.
    const copy = { ...prev[idx], key: `line-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, quantity: "" };
    const next = [...prev];
    next.splice(idx + 1, 0, copy);
    return next;
  });

  const prLineAllocated = (prLineNo) => lines.reduce((s, l) => (l.prLineNo === prLineNo ? s + (Number(l.quantity) || 0) : s), 0);
  const prLineGroupSize = (prLineNo) => lines.filter((l) => l.prLineNo === prLineNo).length;
  const prLineHasBlankRow = (prLineNo) => lines.some((l) => l.prLineNo === prLineNo && (l.quantity === "" || l.quantity == null));
  // Sep 5 2026, user's explicit ask: clearer "under/fully/over allocated"
  // status for a split PR line, replacing the plain "X/Y split" text.
  // testing_agent iteration_145: a freshly-split row starts with a blank
  // qty, which sums to the SAME total as before splitting - showing a
  // misleading Green "fully scheduled" for a row that still needs a
  // value. Force Amber whenever any row in the group is still blank,
  // regardless of what the numeric total happens to add up to.
  const splitAllocationStatus = (prLineNo, prOriginalQty, unit) => {
    const allocated = prLineAllocated(prLineNo);
    const remaining = prOriginalQty - allocated;
    const uom = unit || "EA";
    if (allocated > prOriginalQty + 1e-6) {
      return { tone: "over", label: `${allocated}/${prOriginalQty} ${uom} - exceeds by ${(allocated - prOriginalQty).toFixed(2).replace(/\.00$/, "")}` };
    }
    if (prLineHasBlankRow(prLineNo)) {
      return { tone: "under", label: `${allocated}/${prOriginalQty} ${uom} allocated - enter a quantity for every scheduled delivery` };
    }
    if (allocated < prOriginalQty - 1e-6) {
      return { tone: "under", label: `${allocated}/${prOriginalQty} ${uom} allocated (${remaining.toFixed(2).replace(/\.00$/, "")} remaining)` };
    }
    return { tone: "full", label: `${allocated}/${prOriginalQty} ${uom} fully scheduled` };
  };
  const isSplitGroupStart = (idx) => {
    const l = lines[idx];
    if (!l.fromPr || l.prLineNo == null || prLineGroupSize(l.prLineNo) <= 1) return false;
    return idx === 0 || lines[idx - 1].prLineNo !== l.prLineNo;
  };

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
      if (!l.product_id) errors.push(l.fromPr ? `Line ${idx + 1}: PR item "${l.description || l.iname || ""}" did not match a SAP product - remove this line or fix the catalog and re-fetch` : `Line ${idx + 1}: Product must be selected from the list`);
      if (!l.quantity || Number(l.quantity) <= 0) errors.push(`Line ${idx + 1}: Quantity must be greater than 0`);
      if (l.unit_price === "" || Number(l.unit_price) < 0) errors.push(`Line ${idx + 1}: Unit Price must be 0 or more`);
      if (!l.delivery_date) errors.push(`Line ${idx + 1}: Delivery Date is required`);
      if (l.delivery_date && poDate && l.delivery_date < poDate) errors.push(`Line ${idx + 1}: Delivery Date cannot be before PO Date`);
    });
    const seenPrLines = new Set();
    lines.forEach((l) => {
      if (l.fromPr && l.prLineNo != null && !seenPrLines.has(l.prLineNo)) {
        seenPrLines.add(l.prLineNo);
        const allocated = prLineAllocated(l.prLineNo);
        if (allocated > l.prOriginalQty + 1e-6) {
          errors.push(`PR line ${l.prLineNo}: split total ${allocated} exceeds the PR's ordered qty of ${l.prOriginalQty}`);
        }
      }
    });
    return errors;
  };

  const checklist = [
    { label: "PR fetched & line items autofilled", ok: !!prFetched },
    { label: "Purchase Unit & Bill-To selected", ok: !!purchaseUnitSite && !!billToCompany },
    { label: "Supplier selected from SAP Master", ok: !!selectedSupplier },
    { label: "Every line matched to a real SAP product, with Qty & Price", ok: lines.every((l) => l.product_id && Number(l.quantity) > 0 && l.unit_price !== "") },
    { label: "Delivery dates on/after PO Date", ok: lines.every((l) => !l.delivery_date || !poDate || l.delivery_date >= poDate) },
    { label: "No split line exceeds its PR's ordered quantity", ok: lines.every((l) => !l.fromPr || l.prLineNo == null || prLineAllocated(l.prLineNo) <= l.prOriginalQty + 1e-6) },
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
        supplier_code: selectedSupplier.supplier_code,
        purchase_unit_site: purchaseUnitSite,
        bill_to_company: billToCompany,
        po_date: poDate,
        currency,
        pr_number: prFetched.voc_no,
        items: lines.map((l) => ({
          product_id: l.product_id, description: l.description || null,
          quantity: Number(l.quantity), unit_of_measure: l.unit_of_measure || "EA",
          unit_price: Number(l.unit_price), delivery_date: l.delivery_date,
        })),
      };
      const { data } = await axios.post(`${API}/purchase-orders/create`, payload);
      setResult({ po_number: data.po_number });
      setConfirmOpen(false);
      toast.success(`Purchase Order ${data.po_number} created in SAP`, {
        description: "Click to view its full details",
        action: {
          label: "View PO",
          onClick: () => navigate(`/purchasing-strategy/created-purchase-orders?po=${data.po_number}`),
        },
        duration: 10000,
      });
    } catch (e) {
      const detail = e?.response?.data?.detail;
      setResult({ error: typeof detail === "string" && detail.trim() ? detail : "Failed to create Purchase Order in SAP" });
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
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Purchase Order Creation</span>
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
            <h1 className="font-heading text-xl sm:text-2xl font-bold text-[#101828] tracking-tight flex items-center gap-2" data-testid="po-page-title">
              <ShieldCheck size={20} className="text-[#004B87]" weight="fill" />
              Purchase Order Creation
            </h1>
            <p className="text-sm text-[#475467] mt-0.5">Builds and pushes a live Purchase Order into SAP Business ByDesign - submitted the moment you confirm below.</p>
          </div>
          <Badge className="bg-[#EFF8FF] text-[#175CD3] border border-[#B2DDFF] rounded-sm font-data text-xs px-3 py-1.5" data-testid="po-live-sap-badge">
            <span className="w-1.5 h-1.5 rounded-full bg-[#175CD3] mr-2 inline-block animate-pulse" />
            LIVE SAP OData Write Mode
          </Badge>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
          {/* MAIN FORM */}
          <div className="lg:col-span-8 xl:col-span-9 space-y-4">
            {/* 1. PR Lookup - mandatory entry point */}
            <div className={cardCls} data-testid="po-pr-lookup-card">
              <h2 className={sectionHeadingCls}>
                <FileMagnifyingGlass size={14} /> 1. Purchase Requisition Lookup (Mandatory)
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
                      data-testid="po-pr-number-input"
                    />
                    {showPrSuggestions && prSuggestions.length > 0 && (
                      <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-64 overflow-y-auto" data-testid="po-pr-suggestions">
                        {prSuggestions.map((pr) => (
                          <button
                            key={pr.voc_no}
                            type="button"
                            className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0 flex items-center justify-between gap-2"
                            onClick={() => { setPrVocNo(pr.voc_no); fetchPR(pr.voc_no); }}
                            data-testid={`po-pr-suggestion-${pr.voc_no}`}
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
                  <Button type="button" className="h-9 rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D] shrink-0" onClick={() => fetchPR()} disabled={prFetching} data-testid="po-pr-fetch-button">
                    {prFetching ? <><CircleNotch size={14} className="mr-1.5 animate-spin" /> Fetching...</> : <><FileMagnifyingGlass size={14} className="mr-1.5" /> Fetch PR</>}
                  </Button>
                </div>
              ) : (
                <div className="bg-[#ECFDF3] border border-[#ABEFC6] rounded-sm p-3 flex items-start justify-between gap-4" data-testid="po-pr-summary">
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
                  <Button type="button" variant="ghost" size="sm" className="h-8 text-xs text-[#667085] hover:bg-[#D0D5DD]/40 hover:text-[#344054] rounded-sm" onClick={clearPR} data-testid="po-pr-clear-button">
                    <XCircle size={14} className="mr-1" /> Change PR
                  </Button>
                </div>
              )}
              <p className="text-[11px] text-[#98A2B3]">Vendor, Purchase Unit, Bill-To and Line Items are autofilled from the PR. Delivery Date isn't part of a PR - enter it per line below.</p>
            </div>

            {/* 2. Org context */}
            <div className={cardCls} data-testid="po-org-context-card">
              <h2 className={sectionHeadingCls}>
                <Buildings size={14} /> 2. Organization &amp; Routing Context
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054]">Purchase Unit (Site) *</Label>
                  <div data-testid="po-purchase-unit-select">
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
                  <div data-testid="po-company-display">
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
                  <div data-testid="po-bill-to-select">
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

            {/* 3. Supplier & commercial terms */}
            <div className={cardCls} data-testid="po-supplier-terms-card">
              <h2 className={sectionHeadingCls}>
                <CreditCard size={14} /> 3. Supplier Master &amp; Commercial Terms
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
                      data-testid="po-supplier-search-input"
                    />
                  </div>
                  {showSupplierSuggestions && supplierSuggestions.length > 0 && (
                    <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid="po-supplier-suggestions">
                      {supplierSuggestions.map((s) => (
                        <button
                          key={s.supplier_code}
                          type="button"
                          className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0"
                          onClick={() => pickSupplier(s)}
                          data-testid={`po-supplier-suggestion-${s.supplier_code}`}
                        >
                          <span className="font-semibold text-[#101828] font-data">{s.supplier_code}</span>
                          <span className="text-[#667085]"> - {s.name}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  {selectedSupplier && (
                    <div className="pt-1">
                      <Badge className="bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89] rounded-sm font-data text-xs px-3 py-1.5" data-testid="po-payment-terms-badge">
                        Payment Terms (from Supplier Master): {selectedSupplier.cash_discount_terms_code
                          ? `${selectedSupplier.cash_discount_terms_code} - ${selectedSupplier.cash_discount_terms_text || "Unmapped code"}`
                          : "Not on file - SAP default will apply"}
                      </Badge>
                    </div>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054] flex items-center gap-1"><Calendar size={12} /> PO Date *</Label>
                  <Input type="date" value={poDate} onChange={(e) => setPoDate(e.target.value)} className={`${inputCls} font-data`} data-testid="po-date-input" />
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium text-[#344054]">Currency</Label>
                  <Select value={currency} onValueChange={setCurrency}>
                    <SelectTrigger className={inputCls} data-testid="po-currency-select">
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

            {/* 4. Line items */}
            <div className="bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-hidden" data-testid="po-line-items-card">
              <div className="px-4 py-2.5 border-b border-[#D0D5DD] bg-[#F9FAFB] flex items-center justify-between">
                <h2 className="text-[11px] font-bold uppercase tracking-wide text-[#344054] font-heading flex items-center gap-2">
                  <Truck size={14} /> 4. Line Items Engine
                </h2>
              </div>
              <div className="px-4 py-2 border-b border-[#D0D5DD] bg-[#EFF8FF] flex items-start gap-2" data-testid="po-split-schedule-guidance-banner">
                <Lightbulb size={14} className="text-[#175CD3] mt-0.5 shrink-0" weight="fill" />
                <p className="text-[11px] text-[#175CD3] leading-snug">
                  <span className="font-semibold">Delivery Split Tip:</span> to schedule staggered shipments for one PR line item, click "Split" on that row - adjust the quantity and delivery date on each resulting row until the total matches the PR's approved quantity.
                </p>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs border-collapse min-w-[980px]" data-testid="po-line-items-table">
                  <thead>
                    <tr>
                      {["Product", "Qty", "UoM", "Unit Price", "Delivery Date", "Line Total", ""].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] py-1.5 px-2.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((l, idx) => {
                      const inSplitGroup = l.fromPr && l.prLineNo != null && prLineGroupSize(l.prLineNo) > 1;
                      const groupStart = isSplitGroupStart(idx);
                      const status = inSplitGroup ? splitAllocationStatus(l.prLineNo, l.prOriginalQty, l.unit_of_measure) : null;
                      const deliveryNo = inSplitGroup ? lines.slice(0, idx + 1).filter((x) => x.prLineNo === l.prLineNo).length : null;
                      return (
                      <Fragment key={l.key}>
                      {groupStart && (
                        <tr key={`${l.key}-group-header`} className="bg-[#F0F7FF]" data-testid={`po-split-group-header-${l.prLineNo}`}>
                          <td colSpan={7} className="border border-[#D0D5DD] border-l-[3px] border-l-[#004B87] py-1.5 px-2.5">
                            <div className="flex items-center gap-2 flex-wrap">
                              <CalendarPlus size={13} className="text-[#004B87]" />
                              <span className="text-[11px] font-bold text-[#004B87] font-heading uppercase tracking-wide">
                                PR Line #{l.prLineNo} - {prLineGroupSize(l.prLineNo)} Scheduled Deliveries
                              </span>
                              <span
                                className={`text-[10px] font-data px-1.5 py-0.5 rounded-sm border ${
                                  status.tone === "over" ? "bg-[#FEF3F2] border-[#FECDCA] text-[#B42318]"
                                  : status.tone === "full" ? "bg-[#ECFDF3] border-[#ABEFC6] text-[#027A48]"
                                  : "bg-[#FFFAEB] border-[#FEDF89] text-[#B54708]"
                                }`}
                                data-testid={`po-split-group-status-${l.prLineNo}`}
                              >
                                {status.label}
                              </span>
                            </div>
                          </td>
                        </tr>
                      )}
                      <tr
                        key={l.key}
                        className={`${inSplitGroup ? "bg-[#F9FCFF] border-l-[3px] border-l-[#004B87]" : idx % 2 === 1 ? "bg-[#F9FAFB]" : "bg-white"}`}
                        data-testid={`po-line-row-${idx}`}
                      >
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[280px] relative">
                          {inSplitGroup && (
                            <span className="inline-block mb-1 text-[10px] font-data text-[#004B87]" data-testid={`po-line-delivery-no-${idx}`}>
                              &#x2514;&#x2500; Delivery #{deliveryNo}
                            </span>
                          )}
                          {l.fromPr ? (
                            l.product_id ? (
                              <div className="h-8 flex items-center px-2 text-xs bg-[#F9FAFB] border border-[#D0D5DD] rounded-sm font-data text-[#101828] truncate" data-testid={`po-line-product-locked-${idx}`} title={`${l.product_id} - ${l.description}`}>
                                {l.product_id} - {l.description}
                              </div>
                            ) : (
                              <div className="h-8 flex items-center px-2 text-xs bg-[#FEF3F2] border border-[#FECDCA] rounded-sm text-[#B42318] truncate" data-testid={`po-line-product-unresolved-${idx}`} title={l.description}>
                                <WarningCircle size={12} className="mr-1.5 shrink-0" weight="fill" /> Not matched in SAP ({l.description}) - remove this line to proceed
                              </div>
                            )
                          ) : (
                            <>
                              <Input
                                value={l.productQuery}
                                onChange={(e) => onProductQueryChange(l.key, e.target.value)}
                                onFocus={() => setLines((prev) => prev.map((x) => (x.key === l.key ? { ...x, showSuggestions: true } : x)))}
                                placeholder="Search Product ID or description..."
                                className={`h-8 text-xs rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]`}
                                data-testid={`po-line-product-input-${idx}`}
                              />
                              {l.showSuggestions && l.productSuggestions.length > 0 && (
                                <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid={`po-line-product-suggestions-${idx}`}>
                                  {l.productSuggestions.map((p) => (
                                    <button
                                      key={p.product_id}
                                      type="button"
                                      className="w-full text-left px-3 py-2 text-xs hover:bg-[#F2F4F7] border-b border-[#EAECF0] last:border-0"
                                      onClick={() => pickProduct(l.key, p)}
                                      data-testid={`po-line-product-suggestion-${idx}-${p.product_id}`}
                                    >
                                      <span className="font-semibold text-[#101828] font-data">{p.product_id}</span>
                                      {p.description && <span className="text-[#667085]"> - {p.description}</span>}
                                    </button>
                                  ))}
                                </div>
                              )}
                            </>
                          )}
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[110px]">
                          <Input type="number" min="0" step="any" value={l.quantity} onChange={(e) => updateLine(l.key, "quantity", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`po-line-qty-input-${idx}`} />
                          {inSplitGroup && (
                            <div className="mt-1" data-testid={`po-line-split-allocated-${idx}`}>
                              <div className="h-1 w-full rounded-sm bg-[#EAECF0] overflow-hidden">
                                <div
                                  className={`h-full ${status.tone === "over" ? "bg-[#B42318]" : status.tone === "full" ? "bg-[#027A48]" : "bg-[#B54708]"}`}
                                  style={{ width: `${Math.min(100, (prLineAllocated(l.prLineNo) / (l.prOriginalQty || 1)) * 100)}%` }}
                                />
                              </div>
                              <span className={`text-[9px] font-data leading-tight block mt-0.5 ${status.tone === "over" ? "text-[#B42318]" : status.tone === "full" ? "text-[#027A48]" : "text-[#B54708]"}`}>
                                {status.label}
                              </span>
                            </div>
                          )}
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[130px]">
                          <div className="relative flex items-center gap-1">
                            {l.uomOptions ? (
                              <Select value={l.unit_of_measure} onValueChange={(v) => updateLine(l.key, "unit_of_measure", v)}>
                                <SelectTrigger className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus:ring-1 focus:ring-[#004B87]" data-testid={`po-line-uom-select-${idx}`}>
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  {l.uomOptions.map((o) => (
                                    <SelectItem key={o.unit_code} value={o.unit_code} data-testid={`po-line-uom-option-${idx}-${o.unit_code}`}>
                                      {o.label}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            ) : (
                              <Input value={l.unit_of_measure} onChange={(e) => updateLine(l.key, "unit_of_measure", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`po-line-uom-input-${idx}`} title={l.uomFromPr && !l.uomMappingConfident ? `PR said "${l.uomFromPr}" - please verify the SAP unit code` : undefined} />
                            )}
                            {l.uomFromPr && !l.uomMappingConfident && (
                              <WarningCircle size={12} className="absolute -top-1.5 -right-1.5 text-[#B54708]" weight="fill" data-testid={`po-line-uom-warning-${idx}`} />
                            )}
                            {!l.uomOptions && (
                              <button
                                type="button"
                                onClick={() => fetchUomOptions(l.key, l.product_id)}
                                disabled={!l.product_id || l.uomOptionsLoading}
                                title="Fetch secondary/alternate units from SAP (e.g. Packet/Box conversions)"
                                className="shrink-0 h-8 w-8 flex items-center justify-center rounded-sm border border-[#D0D5DD] text-[#004B87] hover:bg-[#E5F0FA] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                data-testid={`po-line-fetch-uom-button-${idx}`}
                              >
                                {l.uomOptionsLoading ? <CircleNotch size={13} className="animate-spin" /> : <ArrowsLeftRight size={13} />}
                              </button>
                            )}
                          </div>
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[110px]">
                          <Input type="number" min="0" step="any" value={l.unit_price} onChange={(e) => updateLine(l.key, "unit_price", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`po-line-price-input-${idx}`} />
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[140px]">
                          <Input type="date" value={l.delivery_date} onChange={(e) => updateLine(l.key, "delivery_date", e.target.value)} className="h-8 text-xs font-data rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]" data-testid={`po-line-delivery-date-input-${idx}`} />
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 min-w-[110px] font-data text-xs font-semibold text-[#101828] whitespace-nowrap" data-testid={`po-line-total-${idx}`}>
                          {fmtMoney(lineTotal(l), currency)}
                        </td>
                        <td className="border border-[#D0D5DD] py-1.5 px-2.5 text-center whitespace-nowrap min-w-[90px]">
                          {l.fromPr && (
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              className="h-7 px-2 text-[10px] rounded-sm border-[#B2DDFF] bg-[#EFF8FF] text-[#175CD3] hover:bg-[#D1E9FF] mr-1"
                              onClick={() => duplicateLine(l.key)}
                              data-testid={`po-line-split-schedule-button-${idx}`}
                              title="Split this PR item into multiple delivery dates & partial quantities"
                            >
                              <CalendarPlus size={12} className="mr-1" /> Split
                            </Button>
                          )}
                          <Button type="button" variant="ghost" size="icon" className="h-7 w-7 rounded-sm hover:bg-[#FEF3F2]" onClick={() => removeLine(l.key)} disabled={lines.length === 1} data-testid={`po-line-remove-button-${idx}`} title="Remove row">
                            <Trash size={13} className="text-[#B42318]" />
                          </Button>
                        </td>
                      </tr>
                      </Fragment>
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
              <div className={cardCls} data-testid="po-summary-card">
                <h2 className={sectionHeadingCls}>
                  <ListChecks size={14} /> 5. Order Summary
                </h2>
                <div className="space-y-1.5 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="text-[#667085] text-[13px]">Line Items</span>
                    <span className="font-data font-semibold text-[#101828]" data-testid="po-summary-line-count">{lines.length}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[#667085] text-[13px]">Total Units</span>
                    <span className="font-data font-semibold text-[#101828]" data-testid="po-summary-total-units">{totalUnits}</span>
                  </div>
                  <div className="h-px bg-[#EAECF0] my-1" />
                  <div className="flex items-center justify-between">
                    <span className="text-[#344054] font-semibold text-[13px]">Gross Order Value</span>
                    <span className="font-data font-extrabold text-base text-[#004B87]" data-testid="po-summary-total-value">{fmtMoney(grandTotal, currency)}</span>
                  </div>
                </div>

                <div className="h-px bg-[#EAECF0]" />

                <div className="space-y-1.5" data-testid="po-summary-checklist">
                  {checklist.map((c, i) => (
                    <div key={i} className="flex items-start gap-2 text-xs" data-testid={`po-checklist-item-${i}`}>
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
                  data-testid="po-submit-button"
                >
                  Create Purchase Order in SAP <ArrowRight size={15} className="ml-1.5" />
                </Button>
                <p className="text-[11px] text-[#98A2B3] text-center">This creates a real, live SAP document. Review the audit recap carefully.</p>
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* Confirm dialog - audit recap */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-2xl" data-testid="po-confirm-dialog">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><WarningCircle size={18} className="text-[#B54708]" /> Audit Recap - Confirm Purchase Order</DialogTitle>
            <DialogDescription>This will submit a real Purchase Order to SAP ByDesign - it cannot be undone from this app.</DialogDescription>
          </DialogHeader>
          <div className="text-sm space-y-3 text-[#344054]">
            <div className="grid grid-cols-2 gap-2 bg-[#F9FAFB] rounded-sm p-3 font-data text-xs">
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
                    <th className="text-left p-2 font-heading uppercase text-[#344054]">Product</th>
                    <th className="text-right p-2 font-heading uppercase text-[#344054]">Qty</th>
                    <th className="text-left p-2 font-heading uppercase text-[#344054]">Delivery</th>
                    <th className="text-right p-2 font-heading uppercase text-[#344054]">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l, i) => (
                    <tr key={l.key} className="border-t border-[#D0D5DD]">
                      <td className="p-2 font-data">{l.product_id}</td>
                      <td className="p-2 text-right font-data">{l.quantity} {l.unit_of_measure}</td>
                      <td className="p-2 font-data">{l.delivery_date}</td>
                      <td className="p-2 text-right font-data font-semibold">{fmtMoney(lineTotal(l), currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex justify-end font-data text-sm font-bold text-[#004B87]">Grand Total: {fmtMoney(grandTotal, currency)}</div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" className="rounded-sm" onClick={() => setConfirmOpen(false)} disabled={submitting} data-testid="po-confirm-cancel-button">Cancel</Button>
            <Button type="button" className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D]" onClick={submit} disabled={submitting} data-testid="po-confirm-submit-button">
              {submitting ? <><CircleNotch size={13} className="mr-1.5 animate-spin" /> Submitting to SAP...</> : "Confirm & Submit"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Result dialog */}
      <Dialog open={!!result} onOpenChange={(open) => !open && resetForm()}>
        <DialogContent data-testid="po-result-dialog">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {result?.po_number ? <CheckCircle size={18} className="text-[#027A48]" weight="fill" /> : <WarningCircle size={18} className="text-[#B42318]" weight="fill" />}
              {result?.po_number ? "Purchase Order Created" : "Purchase Order Failed"}
            </DialogTitle>
          </DialogHeader>
          {result?.po_number ? (
            <p className="text-sm text-[#344054]" data-testid="po-result-success-message">
              SAP Purchase Order{" "}
              <button
                type="button"
                className="font-data font-bold text-[#004B87] underline hover:text-[#003A6A]"
                data-testid="po-result-number"
                onClick={() => navigate(`/purchasing-strategy/created-purchase-orders?po=${result.po_number}`)}
              >
                {result.po_number}
              </button>{" "}
              was created successfully.
            </p>
          ) : (
            <p className="text-sm text-[#B42318]" data-testid="po-result-error-message">{result?.error}</p>
          )}
          <DialogFooter>
            {result?.po_number && (
              <Button type="button" variant="outline" className="rounded-sm" onClick={() => navigate(`/purchasing-strategy/created-purchase-orders?po=${result.po_number}`)} data-testid="po-result-view-created-button">
                View Created POs
              </Button>
            )}
            <Button type="button" className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] active:bg-[#00294D]" onClick={resetForm} data-testid="po-result-close-button">
              {result?.po_number ? "Create Another" : "Close"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
