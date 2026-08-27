import { useState, useEffect, useRef } from "react";
import axios from "axios";
import { Plus, Trash, WarningCircle, CheckCircle, CircleNotch, MagnifyingGlass } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const todayISO = () => new Date().toISOString().slice(0, 10);

// Aug 2026, user's explicit rule: Business Residence (Company) is fixed
// by the chosen Purchase Unit site, never picked independently -
// mirrors the exact SITE_TO_COMPANY mapping already used elsewhere in
// this app (sap_wip_clearing_client.py) - RI sites vs RT sites.
const RI_SITES = new Set(["P1", "P8", "P5", "P1W", "W1"]);
const companyForSite = (site) => (RI_SITES.has((site || "").toUpperCase()) ? "RI" : "RT");

const emptyLine = () => ({
  key: `line-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  product_id: "",
  description: "",
  unit_of_measure: "EA",
  quantity: "",
  unit_price: "",
  delivery_date: todayISO(),
  productQuery: "",
  productSuggestions: [],
  showSuggestions: false,
});

export default function PurchaseOrderPage() {
  const [sites, setSites] = useState([]);
  const [purchaseUnitSite, setPurchaseUnitSite] = useState("");
  const [billToCompany, setBillToCompany] = useState("");
  const [poDate, setPoDate] = useState(todayISO());
  const [prNumber, setPrNumber] = useState("");
  const [currency, setCurrency] = useState("INR");

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
    if (purchaseUnitSite) setBillToCompany(companyForSite(purchaseUnitSite));
  }, [purchaseUnitSite]);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (supplierWrapperRef.current && !supplierWrapperRef.current.contains(e.target)) setShowSupplierSuggestions(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

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
      ? { ...l, product_id: p.product_id, description: p.description, unit_of_measure: p.unit_of_measure || "EA", productQuery: `${p.product_id} - ${p.description || ""}`, showSuggestions: false }
      : l)));
  };

  const updateLine = (lineKey, field, value) => {
    setLines((prev) => prev.map((l) => (l.key === lineKey ? { ...l, [field]: value } : l)));
  };

  const addLine = () => setLines((prev) => [...prev, emptyLine()]);
  const removeLine = (lineKey) => setLines((prev) => (prev.length > 1 ? prev.filter((l) => l.key !== lineKey) : prev));

  const validationErrors = () => {
    const errors = [];
    if (!purchaseUnitSite) errors.push("Purchase Unit (Site) is required");
    if (!billToCompany) errors.push("Bill-To Company is required");
    if (!selectedSupplier) errors.push("Supplier must be selected from the list");
    if (!poDate) errors.push("PO Date is required");
    if (lines.length === 0) errors.push("At least one line item is required");
    lines.forEach((l, idx) => {
      if (!l.product_id) errors.push(`Line ${idx + 1}: Product must be selected from the list`);
      if (!l.quantity || Number(l.quantity) <= 0) errors.push(`Line ${idx + 1}: Quantity must be greater than 0`);
      if (l.unit_price === "" || Number(l.unit_price) < 0) errors.push(`Line ${idx + 1}: Unit Price must be 0 or more`);
      if (!l.delivery_date) errors.push(`Line ${idx + 1}: Delivery Date is required`);
    });
    return errors;
  };

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
        pr_number: prNumber || null,
        items: lines.map((l) => ({
          product_id: l.product_id, description: l.description || null,
          quantity: Number(l.quantity), unit_of_measure: l.unit_of_measure || "EA",
          unit_price: Number(l.unit_price), delivery_date: l.delivery_date,
        })),
      };
      const { data } = await axios.post(`${API}/purchase-orders/create`, payload);
      setResult({ po_number: data.po_number });
      setConfirmOpen(false);
    } catch (e) {
      const detail = e?.response?.data?.detail;
      setResult({ error: typeof detail === "string" && detail.trim() ? detail : "Failed to create Purchase Order in SAP" });
      setConfirmOpen(false);
    } finally {
      setSubmitting(false);
    }
  };

  const resetForm = () => {
    setPurchaseUnitSite(""); setBillToCompany(""); setPoDate(todayISO()); setPrNumber("");
    setSupplierQuery(""); setSelectedSupplier(null); setLines([emptyLine()]); setResult(null);
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] flex flex-col font-sans">
      <Toaster position="top-right" />
      <header className="bg-white border-b border-[#D0D5DD] px-6 py-3 flex items-center justify-between gap-4 flex-wrap">
        <NavTabs />
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto max-w-[1200px] w-full mx-auto px-6 py-6 space-y-4">
        <div>
          <h1 className="font-heading text-xl font-bold text-[#1D2939]" data-testid="po-page-title">Create Purchase Order</h1>
          <p className="text-sm text-[#667085] mt-0.5">Builds and pushes a real Purchase Order into SAP ByDesign - submitted live the moment you confirm below.</p>
        </div>

        {/* Header fields */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-4" data-testid="po-header-card">
          <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">1. Order Details</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="space-y-1.5">
              <Label className="text-xs text-[#344054]">Purchase Unit (Site) *</Label>
              <Select value={purchaseUnitSite} onValueChange={setPurchaseUnitSite}>
                <SelectTrigger className="h-9 text-sm" data-testid="po-purchase-unit-select">
                  <SelectValue placeholder="Choose site" />
                </SelectTrigger>
                <SelectContent>
                  {sites.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs text-[#344054]">Company (Business Residence)</Label>
              <div
                className="h-9 flex items-center px-3 rounded-sm border border-[#D0D5DD] bg-[#F9FAFB] text-sm font-semibold text-[#1D2939]"
                data-testid="po-company-display"
              >
                {purchaseUnitSite ? companyForSite(purchaseUnitSite) : <span className="text-[#98A2B3] font-normal">Auto-set from Purchase Unit</span>}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs text-[#344054]">Bill-To Company *</Label>
              <Select value={billToCompany} onValueChange={setBillToCompany}>
                <SelectTrigger className="h-9 text-sm" data-testid="po-bill-to-select">
                  <SelectValue placeholder="Choose company" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="RI">RAY INTERNATIONAL (RI)</SelectItem>
                  <SelectItem value="RT">RADISH TECHNOLOGIES (RT)</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5 relative" ref={supplierWrapperRef}>
              <Label className="text-xs text-[#344054]">Supplier *</Label>
              <div className="relative">
                <MagnifyingGlass size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                <Input
                  value={supplierQuery}
                  onChange={(e) => onSupplierQueryChange(e.target.value)}
                  onFocus={() => setShowSupplierSuggestions(true)}
                  placeholder="Search supplier name or code..."
                  className="h-9 text-sm pl-8"
                  data-testid="po-supplier-search-input"
                />
              </div>
              {showSupplierSuggestions && supplierSuggestions.length > 0 && (
                <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid="po-supplier-suggestions">
                  {supplierSuggestions.map((s) => (
                    <button
                      key={s.supplier_code}
                      type="button"
                      className="w-full text-left px-3 py-2 text-xs hover:bg-[#F9FAFB] border-b border-[#EAECF0] last:border-0"
                      onClick={() => pickSupplier(s)}
                      data-testid={`po-supplier-suggestion-${s.supplier_code}`}
                    >
                      <span className="font-medium text-[#344054]">{s.supplier_code}</span>
                      <span className="text-[#667085]"> - {s.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs text-[#344054]">PO Date *</Label>
              <Input type="date" value={poDate} onChange={(e) => setPoDate(e.target.value)} className="h-9 text-sm" data-testid="po-date-input" />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs text-[#344054]">PR Number (reference only, optional)</Label>
              <Input
                value={prNumber} onChange={(e) => setPrNumber(e.target.value)}
                placeholder="e.g. PR-2026-0001" className="h-9 text-sm" data-testid="po-pr-number-input"
              />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs text-[#344054]">Currency</Label>
              <Select value={currency} onValueChange={setCurrency}>
                <SelectTrigger className="h-9 text-sm" data-testid="po-currency-select">
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

        {/* Line items */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="po-line-items-card">
          <div className="px-3 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB] flex items-center justify-between">
            <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">2. Line Items</h3>
            <Button type="button" size="sm" variant="outline" className="h-7 text-xs" onClick={addLine} data-testid="po-add-line-button">
              <Plus size={13} className="mr-1" /> Add Item
            </Button>
          </div>
          <table className="w-full text-xs border-collapse min-w-[900px]" data-testid="po-line-items-table">
            <thead>
              <tr>
                {["Product", "Qty", "UoM", "Unit Price", "Delivery Date", ""].map((h) => (
                  <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {lines.map((l, idx) => (
                <tr key={l.key} data-testid={`po-line-row-${idx}`}>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[280px] relative">
                    <Input
                      value={l.productQuery}
                      onChange={(e) => onProductQueryChange(l.key, e.target.value)}
                      onFocus={() => setLines((prev) => prev.map((x) => (x.key === l.key ? { ...x, showSuggestions: true } : x)))}
                      placeholder="Search Product ID or description..."
                      className="h-8 text-xs"
                      data-testid={`po-line-product-input-${idx}`}
                    />
                    {l.showSuggestions && l.productSuggestions.length > 0 && (
                      <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-56 overflow-y-auto" data-testid={`po-line-product-suggestions-${idx}`}>
                        {l.productSuggestions.map((p) => (
                          <button
                            key={p.product_id}
                            type="button"
                            className="w-full text-left px-3 py-2 text-xs hover:bg-[#F9FAFB] border-b border-[#EAECF0] last:border-0"
                            onClick={() => pickProduct(l.key, p)}
                            data-testid={`po-line-product-suggestion-${idx}-${p.product_id}`}
                          >
                            <span className="font-medium text-[#344054]">{p.product_id}</span>
                            {p.description && <span className="text-[#667085]"> - {p.description}</span>}
                          </button>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[90px]">
                    <Input type="number" min="0" step="any" value={l.quantity} onChange={(e) => updateLine(l.key, "quantity", e.target.value)} className="h-8 text-xs" data-testid={`po-line-qty-input-${idx}`} />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[70px]">
                    <Input value={l.unit_of_measure} onChange={(e) => updateLine(l.key, "unit_of_measure", e.target.value)} className="h-8 text-xs" data-testid={`po-line-uom-input-${idx}`} />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[110px]">
                    <Input type="number" min="0" step="any" value={l.unit_price} onChange={(e) => updateLine(l.key, "unit_price", e.target.value)} className="h-8 text-xs" data-testid={`po-line-price-input-${idx}`} />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[140px]">
                    <Input type="date" value={l.delivery_date} onChange={(e) => updateLine(l.key, "delivery_date", e.target.value)} className="h-8 text-xs" data-testid={`po-line-delivery-date-input-${idx}`} />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1.5 text-center">
                    <Button type="button" variant="ghost" size="icon" className="h-7 w-7" onClick={() => removeLine(l.key)} disabled={lines.length === 1} data-testid={`po-line-remove-button-${idx}`}>
                      <Trash size={13} className="text-[#B42318]" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex justify-end">
          <Button type="button" onClick={openConfirm} data-testid="po-submit-button">Create Purchase Order</Button>
        </div>
      </main>

      {/* Confirm dialog */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent data-testid="po-confirm-dialog">
          <DialogHeader>
            <DialogTitle>Confirm Purchase Order</DialogTitle>
            <DialogDescription>This will submit a real Purchase Order to SAP ByDesign - it cannot be undone from this app.</DialogDescription>
          </DialogHeader>
          <div className="text-sm space-y-1 text-[#344054]">
            <p><b>Company:</b> {companyForSite(purchaseUnitSite)} · <b>Purchase Unit:</b> {purchaseUnitSite} · <b>Bill-To:</b> {billToCompany}</p>
            <p><b>Supplier:</b> {selectedSupplier ? `${selectedSupplier.supplier_code} - ${selectedSupplier.name}` : "—"}</p>
            <p><b>PO Date:</b> {poDate} {prNumber ? <>· <b>PR Number:</b> {prNumber}</> : null}</p>
            <p><b>Line Items:</b> {lines.length}</p>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setConfirmOpen(false)} disabled={submitting} data-testid="po-confirm-cancel-button">Cancel</Button>
            <Button type="button" onClick={submit} disabled={submitting} data-testid="po-confirm-submit-button">
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
              {result?.po_number ? <CheckCircle size={18} className="text-[#0E7C86]" /> : <WarningCircle size={18} className="text-[#B42318]" />}
              {result?.po_number ? "Purchase Order Created" : "Purchase Order Failed"}
            </DialogTitle>
          </DialogHeader>
          {result?.po_number ? (
            <p className="text-sm text-[#344054]" data-testid="po-result-success-message">
              SAP Purchase Order <b data-testid="po-result-number">{result.po_number}</b> was created successfully.
            </p>
          ) : (
            <p className="text-sm text-[#B42318]" data-testid="po-result-error-message">{result?.error}</p>
          )}
          <DialogFooter>
            <Button type="button" onClick={resetForm} data-testid="po-result-close-button">
              {result?.po_number ? "Create Another" : "Close"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
