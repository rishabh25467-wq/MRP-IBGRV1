import { useState, useEffect, useRef } from "react";
import "@/App.css";
import axios from "axios";
import {
  Shield,
  MagnifyingGlass,
  Sparkle,
  Trash,
  ArrowRight,
  WarningCircle,
  CheckCircle,
  CircleNotch,
  Robot,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const formatQty = (v) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 }));
const formatSapId = (id) => (id ? id.replace(/^0+(?=\d)/, "") : id);
const cleanSapMessage = (msg) => (msg ? msg.replace(/\s{2,}/g, " ").trim() : msg);
const parseGiError = (raw) => {
  if (!raw) return null;
  const jsonMatch = raw.match(/\{.*\}/s);
  if (jsonMatch) {
    try {
      const value = JSON.parse(jsonMatch[0])?.error?.message?.value;
      if (value) return value.split("::").filter(Boolean).join(" — ");
    } catch { /* fall through to raw cleanup below */ }
  }
  return cleanSapMessage(raw);
};
const todayISO = () => new Date().toISOString().slice(0, 10);

// Real (not simulated) steps this app's live SAP write actually goes
// through today - drives the progress bar in the confirm dialog. Goods
// Issue / Outbound Delivery are NOT here yet (needs 2 more SAP services
// Basis hasn't exposed - see stock_transfer_service.py) - add steps here
// once that's wired in, same job-polling mechanism will just work.
const STO_STEPS = [
  { key: "validate", label: "Validate order" },
  { key: "check", label: "SAP availability & data check" },
  { key: "create", label: "Create in SAP" },
];

// One selected line item. Every field the SAP ByDesign reference screen
// shows per-line (Source Warehouse, Available Qty, Ship-from Site,
// Availability Status) lives here.
const emptyLine = (product) => ({
  key: `${product.product_id}-${Date.now()}`,
  product_id: product.product_id,
  description: product.description,
  unit_of_measure: product.unit_of_measure,
  locations: product.locations || [],
  source_warehouse_id: "",
  ship_from_site_id: "",
  available_qty: null,
  requested_qty: "",
  suggestion: null,
  error: null,
});

export default function StockTransferPage() {
  const [items, setItems] = useState([]);
  const [productQuery, setProductQuery] = useState("");
  const [productSuggestions, setProductSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const searchWrapperRef = useRef(null);
  const suggestionDebounceRef = useRef(null);

  const [shipToSiteId, setShipToSiteId] = useState("");
  const [shipToSiteOptions, setShipToSiteOptions] = useState([]);
  const [shipToLocationId, setShipToLocationId] = useState("");
  const [shipToLocationOptions, setShipToLocationOptions] = useState([]);
  const [requestedDeliveryDate, setRequestedDeliveryDate] = useState("");
  // GST / E-way bill compliance fields (Aug 2026, user's explicit ask) -
  // mandatory before an order can be submitted. NOT yet pushed to SAP -
  // Basis needs to expose a write path for these custom fields on the
  // Stock Transfer Delivery document first (see stock_transfer_service.py
  // module docstring) - captured + enforced here regardless, ready to
  // push the moment that's available.
  const [transportationMode, setTransportationMode] = useState("By Road");
  const [vehicleNo, setVehicleNo] = useState("");
  const [placeOfSupply, setPlaceOfSupply] = useState("");
  const [grNo, setGrNo] = useState("");
  const [dateOfSupply, setDateOfSupply] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState(null);

  const [nlText, setNlText] = useState("");
  const [nlParsing, setNlParsing] = useState(false);
  const [nlPreview, setNlPreview] = useState(null);

  const [recentOrders, setRecentOrders] = useState([]);
  const [loadingRecent, setLoadingRecent] = useState(true);
  const [selectedOrder, setSelectedOrder] = useState(null);

  // 2x-safety confirmation before the real, irreversible live SAP write
  // (user's explicit ask, Aug 2026): clicking "Create" first opens a
  // summary dialog - nothing is submitted until the user explicitly
  // confirms a SECOND time inside that dialog. The dialog then shows a
  // real (not simulated) step-by-step progress bar driven by the
  // background job's own `step` field.
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [sapSubmitPhase, setSapSubmitPhase] = useState(null); // null | "submitting" | "done" | "failed"
  const [sapSubmitMessage, setSapSubmitMessage] = useState(null);
  const [stepStatuses, setStepStatuses] = useState({}); // { [stepKey]: "pending" | "active" | "done" | "failed" }

  const shipFromSiteId = items.find((i) => i.ship_from_site_id)?.ship_from_site_id || "";

  const loadRecentOrders = async () => {
    setLoadingRecent(true);
    try {
      const { data } = await axios.get(`${API}/stock-transfer/orders`);
      setRecentOrders(data);
    } catch {
      setRecentOrders([]);
    } finally {
      setLoadingRecent(false);
    }
  };

  useEffect(() => { loadRecentOrders(); }, []);

  // Ship-to Site options depend on the (derived) Ship-from Site - refetch
  // whenever the first line item's warehouse pick resolves/changes it.
  // User's explicit ask: once Ship-to Site/Location are picked they should
  // stay valid for every item added afterwards, not need re-picking - so
  // only clear them if they've actually become INVALID for the (possibly
  // changed) Ship-from Site, never just because something else changed.
  useEffect(() => {
    if (!shipFromSiteId) {
      setShipToSiteOptions([]);
      setShipToSiteId("");
      return;
    }
    axios.get(`${API}/stock-transfer/ship-to-sites`, { params: { ship_from_site_id: shipFromSiteId } })
      .then(({ data }) => {
        const sites = data.sites || [];
        setShipToSiteOptions(sites);
        setShipToSiteId((prev) => (prev && !sites.includes(prev) ? "" : prev));
      })
      .catch(() => setShipToSiteOptions([]));
  }, [shipFromSiteId]);

  // Ship-to Location options depend on the chosen Ship-to Site.
  useEffect(() => {
    if (!shipToSiteId) { setShipToLocationOptions([]); setShipToLocationId(""); return; }
    axios.get(`${API}/stock-transfer/locations`, { params: { site_id: shipToSiteId } })
      .then(({ data }) => setShipToLocationOptions(data.warehouses || []))
      .catch(() => setShipToLocationOptions([]));
  }, [shipToSiteId]);

  useEffect(() => {
    if (suggestionDebounceRef.current) clearTimeout(suggestionDebounceRef.current);
    const q = productQuery.trim();
    if (q.length < 2) { setProductSuggestions([]); return; }
    suggestionDebounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/products/search`, { params: { q, limit: 10 } });
        setProductSuggestions(data);
      } catch {
        setProductSuggestions([]);
      }
    }, 250);
    return () => clearTimeout(suggestionDebounceRef.current);
  }, [productQuery]);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (searchWrapperRef.current && !searchWrapperRef.current.contains(e.target)) setShowSuggestions(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const addItem = async (product_id, description, initialQty) => {
    setProductQuery("");
    setProductSuggestions([]);
    setShowSuggestions(false);
    if (items.some((i) => i.product_id === product_id)) {
      toast.error(`${product_id} is already in the line item list.`);
      return;
    }
    try {
      const { data } = await axios.get(`${API}/stock-transfer/inventory`, { params: { product_id } });
      const line = emptyLine({ ...data, description: data.description || description });
      if (initialQty != null) line.requested_qty = String(initialQty);
      setItems((prev) => [...prev, line]);
      fetchSuggestion(line.key, product_id);
    } catch {
      toast.error(`Could not check inventory for ${product_id}.`);
    }
  };

  const fetchSuggestion = async (key, product_id, shipTo = shipToSiteId) => {
    try {
      const { data } = await axios.get(`${API}/stock-transfer/suggest-source`, {
        params: { product_id, ship_to_site_id: shipTo || undefined },
      });
      setItems((prev) => prev.map((i) => (i.key === key ? { ...i, suggestion: data } : i)));
    } catch {
      // purely advisory - silently skip on failure
    }
  };

  // Ship-to Site changing can invalidate/refresh every line's suggestion
  // (a warehouse at the new Ship-to Site should never be suggested).
  useEffect(() => {
    items.forEach((i) => fetchSuggestion(i.key, i.product_id, shipToSiteId));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shipToSiteId]);

  const removeItem = (key) => setItems((prev) => prev.filter((i) => i.key !== key));

  const chooseWarehouse = (key, warehouseId) => {
    setItems((prev) => prev.map((i) => {
      if (i.key !== key) return i;
      const loc = i.locations.find((l) => l.warehouse_id === warehouseId);
      return {
        ...i,
        source_warehouse_id: warehouseId,
        ship_from_site_id: loc ? loc.site_id : "",
        available_qty: loc ? loc.qty : null,
        error: null,
      };
    }));
  };

  const applySuggestion = (key) => {
    setItems((prev) => prev.map((i) => {
      if (i.key !== key || !i.suggestion?.suggested) return i;
      const s = i.suggestion.suggested;
      return { ...i, source_warehouse_id: s.warehouse_id, ship_from_site_id: s.site_id, available_qty: s.qty, error: null };
    }));
  };

  const setRequestedQty = (key, value) => {
    setItems((prev) => prev.map((i) => {
      if (i.key !== key) return i;
      const qty = value === "" ? "" : Number(value);
      let error = null;
      if (value !== "" && (isNaN(qty) || qty <= 0)) error = "Requested Quantity must be greater than zero.";
      else if (i.available_qty != null && qty > i.available_qty) {
        error = "Insufficient Stock. Please enter a quantity equal to or less than the available inventory.";
      }
      return { ...i, requested_qty: value, error };
    }));
  };

  const runValidation = () => {
    if (items.length === 0) return "Select at least one item.";
    for (const i of items) {
      if (!i.source_warehouse_id) return `${i.product_id}: Source Warehouse is required.`;
      if (!i.requested_qty || Number(i.requested_qty) <= 0) return `${i.product_id}: Requested Quantity must be greater than zero.`;
      if (i.available_qty != null && Number(i.requested_qty) > i.available_qty) return `${i.product_id}: Insufficient Stock. Please enter a quantity equal to or less than the available inventory.`;
    }
    const sites = new Set(items.map((i) => i.ship_from_site_id));
    if (sites.size > 1) return "All line items must ship from the same Ship-from Site - remove the mismatched item(s) or create a separate order.";
    if (!shipToSiteId) return "Ship-to Site is required.";
    if (shipToSiteId === shipFromSiteId) return "Ship-to Site cannot be the same as Ship-from Site.";
    if (!shipToLocationId) return "Ship-to Location is required.";
    if (!requestedDeliveryDate) return "Requested Delivery Date is required.";
    if (requestedDeliveryDate < todayISO()) return "Requested Delivery Date cannot be earlier than today.";
    if (!transportationMode) return "Transportation Mode is required.";
    if (!vehicleNo.trim()) return "Vehicle No. is required.";
    if (!placeOfSupply.trim()) return "Place Of Supply is required.";
    if (!grNo.trim()) return "G.R No. is required.";
    if (!dateOfSupply) return "Date Of Supply is required.";
    return null;
  };

  const openConfirmDialog = () => {
    const validationError = runValidation();
    setFormError(validationError);
    if (validationError) { toast.error(validationError); return; }
    setShowConfirmDialog(true);
  };

  const applyJobStepStatuses = (job) => {
    setStepStatuses((prev) => {
      if (job.status === "done") return { ...prev, check: "done", create: "done" };
      if (job.status === "failed") {
        if (job.step === "creating") return { ...prev, check: "done", create: "failed" };
        return { ...prev, check: "failed", create: "pending" };
      }
      if (job.step === "creating") return { ...prev, check: "done", create: "active" };
      return { ...prev, check: "active", create: "pending" };
    });
  };

  const pollSapJob = (jobId) => {
    const start = Date.now();
    const tick = async () => {
      try {
        const { data } = await axios.get(`${API}/stock-transfer/orders/sap-status/${jobId}`);
        applyJobStepStatuses(data);
        if (data.status === "done") {
          setSapSubmitPhase("done");
          setSapSubmitMessage(`Created in SAP as ${formatSapId(data.result?.sap_order_id) || "—"}.`);
          toast.success(`Stock Transfer Order created in SAP (${formatSapId(data.result?.sap_order_id) || "—"}).`);
          setItems([]);
          setShipToSiteId(""); setShipToLocationId(""); setRequestedDeliveryDate(""); setFormError(null);
          setVehicleNo(""); setPlaceOfSupply(""); setGrNo(""); setDateOfSupply(""); setTransportationMode("By Road");
          loadRecentOrders();
          return;
        }
        if (data.status === "failed") {
          setSapSubmitPhase("failed");
          setSapSubmitMessage(cleanSapMessage(data.error) || "SAP rejected this order.");
          toast.error(cleanSapMessage(data.error) || "SAP rejected this Stock Transfer Order.");
          loadRecentOrders();
          return;
        }
        if (Date.now() - start > 3 * 60 * 1000) {
          setSapSubmitPhase("failed");
          setSapSubmitMessage("Still waiting on SAP after 3 minutes - check the order's row/detail view shortly.");
          loadRecentOrders();
          return;
        }
        setTimeout(tick, 3000);
      } catch {
        setTimeout(tick, 3000);
      }
    };
    tick();
  };

  const confirmAndSubmit = async () => {
    setSubmitting(true);
    setSapSubmitPhase("submitting");
    setSapSubmitMessage(null);
    setStepStatuses({ validate: "active", check: "pending", create: "pending" });
    try {
      const payload = {
        ship_to_site_id: shipToSiteId,
        ship_to_location_id: shipToLocationId,
        requested_delivery_date: requestedDeliveryDate,
        transportation_mode: transportationMode,
        vehicle_no: vehicleNo.trim(),
        place_of_supply: placeOfSupply.trim(),
        gr_no: grNo.trim(),
        date_of_supply: dateOfSupply,
        items: items.map((i) => ({
          product_id: i.product_id,
          source_warehouse_id: i.source_warehouse_id,
          requested_qty: Number(i.requested_qty),
        })),
      };
      const { data } = await axios.post(`${API}/stock-transfer/orders`, payload);
      setStepStatuses({ validate: "done", check: "active", create: "pending" });
      toast.message(`Stock Transfer Order ${data.sto_id} saved - submitting live to SAP...`);
      loadRecentOrders();
      if (data.sap_job_id) pollSapJob(data.sap_job_id);
    } catch (e) {
      const msg = e?.response?.data?.detail || "Could not create the Stock Transfer Order.";
      setFormError(msg);
      toast.error(msg);
      setSapSubmitPhase(null);
      setStepStatuses({ validate: "failed", check: "pending", create: "pending" });
    } finally {
      setSubmitting(false);
    }
  };

  const parseNaturalLanguage = async () => {
    if (!nlText.trim()) return;
    setNlParsing(true);
    setNlPreview(null);
    try {
      const { data } = await axios.post(`${API}/stock-transfer/parse-nl`, { text: nlText.trim() });
      setNlPreview(data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not parse that request.");
    } finally {
      setNlParsing(false);
    }
  };

  const applyNlPreview = async () => {
    if (!nlPreview) return;
    if (nlPreview.product_id) {
      await addItem(nlPreview.product_id, null, nlPreview.quantity);
    }
    if (nlPreview.ship_to_site_id) setShipToSiteId(nlPreview.ship_to_site_id);
    if (nlPreview.requested_delivery_date) setRequestedDeliveryDate(nlPreview.requested_delivery_date);
    setNlPreview(null);
    setNlText("");
    toast.message("Applied to the form below - review before creating.");
  };

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]" data-testid="stock-transfer-page">
      <Toaster position="top-right" richColors />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Inter-Plant Stock Transfer</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0"><NavTabs /></div>
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto max-w-[1400px] w-full mx-auto px-6 py-6 space-y-4">
        <div>
          <h1 className="font-heading text-xl font-bold text-[#1D2939]">Inter-Plant Stock Transfer</h1>
          <p className="text-sm text-[#667085] mt-0.5">Create a Stock Transfer Order to move inventory between sites - submitted live to SAP immediately after you confirm.</p>
        </div>

        {/* AI natural-language entry (test feature) */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-2" data-testid="stock-transfer-nl-card">
          <div className="flex items-center gap-1.5">
            <Robot size={15} className="text-[#0E7C86]" />
            <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">AI Quick Entry (test)</h3>
          </div>
          <div className="flex gap-2">
            <Input
              value={nlText}
              onChange={(e) => setNlText(e.target.value)}
              placeholder='e.g. "transfer 500 of ITEM-001 to P2 by tomorrow"'
              className="flex-1"
              data-testid="stock-transfer-nl-input"
              onKeyDown={(e) => e.key === "Enter" && parseNaturalLanguage()}
            />
            <Button type="button" variant="outline" disabled={nlParsing || !nlText.trim()} onClick={parseNaturalLanguage} data-testid="stock-transfer-nl-parse-button">
              <Sparkle size={13} className="mr-1.5" /> {nlParsing ? "Parsing..." : "Parse"}
            </Button>
          </div>
          {nlPreview && (
            <div className="bg-[#F9FAFB] border border-[#EAECF0] rounded-sm p-3 text-xs space-y-1" data-testid="stock-transfer-nl-preview">
              <p className="text-[#344054]">Product: <b>{nlPreview.product_id || "—"}</b> · Qty: <b>{nlPreview.quantity ?? "—"}</b> · Ship-to: <b>{nlPreview.ship_to_site_id || "—"}</b> · Delivery: <b>{nlPreview.requested_delivery_date || "—"}</b></p>
              <p className="text-[#98A2B3]">Model: {nlPreview.model_used} - review carefully, nothing is created yet.</p>
              <Button type="button" size="sm" className="mt-1" onClick={applyNlPreview} data-testid="stock-transfer-nl-apply-button">Apply to Form</Button>
            </div>
          )}
        </div>

        {/* Item search */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3">
          <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">1. Select Item(s)</h3>
          <div className="relative max-w-md" ref={searchWrapperRef}>
            <MagnifyingGlass size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input
              value={productQuery}
              onChange={(e) => { setProductQuery(e.target.value); setShowSuggestions(true); }}
              onFocus={() => setShowSuggestions(true)}
              placeholder="Search Product ID or description..."
              className="pl-8"
              data-testid="stock-transfer-product-search-input"
            />
            {showSuggestions && productSuggestions.length > 0 && (
              <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-64 overflow-y-auto" data-testid="stock-transfer-product-suggestions">
                {productSuggestions.map((s) => (
                  <button
                    key={s.product_id}
                    type="button"
                    className="w-full text-left px-3 py-2 text-xs hover:bg-[#F9FAFB] border-b border-[#EAECF0] last:border-0"
                    onClick={() => addItem(s.product_id, s.description)}
                    data-testid={`stock-transfer-product-suggestion-${s.product_id}`}
                  >
                    <span className="font-medium text-[#344054]">{s.product_id}</span>
                    {s.description && <span className="text-[#667085]"> - {s.description}</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Line item table */}
        {items.length > 0 && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="stock-transfer-line-items-card">
            <div className="px-3 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB]">
              <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">2-4. Inventory Check / Source Warehouse / Ship-from Site</h3>
            </div>
            <table className="w-full text-xs border-collapse min-w-[900px]" data-testid="stock-transfer-line-items-table">
              <thead>
                <tr>
                  {["Product", "Source Warehouse", "Ship-from Site", "Available Qty", "Requested Qty", "Status", ""].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.key} data-testid={`stock-transfer-line-${i.product_id}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <div className="font-medium text-[#344054]">{i.product_id}</div>
                      <div className="text-[#667085]">{i.description || "—"}</div>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[220px]">
                      <Select value={i.source_warehouse_id} onValueChange={(v) => chooseWarehouse(i.key, v)}>
                        <SelectTrigger className="h-8 text-xs" data-testid={`stock-transfer-warehouse-select-${i.product_id}`}>
                          <SelectValue placeholder={
                            i.locations.length === 0
                              ? "No usable stock found"
                              : (shipFromSiteId && !i.locations.some((l) => l.site_id === shipFromSiteId) ? `No stock at ${shipFromSiteId}` : "Choose warehouse")
                          } />
                        </SelectTrigger>
                        <SelectContent>
                          {i.locations
                            .filter((l) => !shipFromSiteId || l.site_id === shipFromSiteId || l.warehouse_id === i.source_warehouse_id)
                            .map((l) => (
                              <SelectItem key={l.warehouse_id} value={l.warehouse_id}>
                                {l.site_id} - {l.warehouse_name || l.warehouse_id} ({formatQty(l.qty)})
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                      {i.suggestion?.suggested && i.suggestion.suggested.warehouse_id !== i.source_warehouse_id
                        && (!shipFromSiteId || i.suggestion.suggested.site_id === shipFromSiteId) && (
                        <button
                          type="button"
                          onClick={() => applySuggestion(i.key)}
                          className="mt-1 flex items-center gap-1 text-[11px] text-[#0E7C86] hover:underline"
                          data-testid={`stock-transfer-suggestion-${i.product_id}`}
                        >
                          <Sparkle size={11} /> Suggested: {i.suggestion.suggested.site_id} / {i.suggestion.suggested.warehouse_name || i.suggestion.suggested.warehouse_id}
                        </button>
                      )}
                      {i.suggestion?.suggested && shipFromSiteId && i.suggestion.suggested.site_id !== shipFromSiteId && (
                        <p className="mt-1 text-[11px] text-[#98A2B3]">No stock at {shipFromSiteId} for this item - it would need a separate order.</p>
                      )}
                      {i.suggestion && !i.suggestion.suggested && (
                        <p className="mt-1 text-[11px] text-[#98A2B3]">{i.suggestion.reason}</p>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#667085]">{i.ship_from_site_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(i.available_qty)} {i.unit_of_measure}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Input
                        type="number"
                        value={i.requested_qty}
                        onChange={(e) => setRequestedQty(i.key, e.target.value)}
                        className="h-8 text-xs w-28"
                        data-testid={`stock-transfer-qty-input-${i.product_id}`}
                      />
                      {i.error && <p className="text-[11px] text-[#B42318] font-bold mt-1" data-testid={`stock-transfer-qty-error-${i.product_id}`}>{i.error}</p>}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      {i.locations.length === 0 ? (
                        <span className="inline-flex items-center gap-1 text-[#B42318] font-bold"><WarningCircle size={12} /> No Stock</span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[#027A48] font-bold"><CheckCircle size={12} /> Available</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <button type="button" onClick={() => removeItem(i.key)} className="text-[#B42318] hover:text-[#912018]" data-testid={`stock-transfer-remove-${i.product_id}`}>
                        <Trash size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Header fields: Ship-to Site / Location / Priority / Date */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3">
          <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">5-9. Ship-to Site, Location, Delivery Priority &amp; Date</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div>
              <Label className="text-xs font-bold text-[#344054]">Ship-from Site</Label>
              <Input value={shipFromSiteId || ""} readOnly disabled placeholder="Pick a source warehouse above" data-testid="stock-transfer-ship-from-site" />
              <p className="text-[11px] text-[#98A2B3] mt-0.5">Auto-determined from Source Warehouse.</p>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Ship-to Site*</Label>
              <Select value={shipToSiteId} onValueChange={setShipToSiteId} disabled={!shipFromSiteId}>
                <SelectTrigger data-testid="stock-transfer-ship-to-site-select"><SelectValue placeholder="Choose Ship-to Site" /></SelectTrigger>
                <SelectContent>
                  {shipToSiteOptions.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Ship-to Location*</Label>
              <Select value={shipToLocationId} onValueChange={setShipToLocationId} disabled={!shipToSiteId}>
                <SelectTrigger data-testid="stock-transfer-ship-to-location-select"><SelectValue placeholder="Choose Location" /></SelectTrigger>
                <SelectContent>
                  {shipToLocationOptions.map((w) => <SelectItem key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_name || w.warehouse_id}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Delivery Priority</Label>
              <Input value="Immediate" readOnly disabled data-testid="stock-transfer-delivery-priority" />
            </div>
          </div>
          <div className="max-w-xs">
            <Label className="text-xs font-bold text-[#344054]">Requested Delivery Date*</Label>
            <Input type="date" min={todayISO()} value={requestedDeliveryDate} onChange={(e) => setRequestedDeliveryDate(e.target.value)} data-testid="stock-transfer-delivery-date-input" />
          </div>
        </div>

        {/* GST / E-way bill compliance fields (Aug 2026, user's explicit
            ask) - mandatory before the order can move forward, even
            though pushing these into SAP itself is pending Basis. */}
        <div className="grid grid-cols-1 sm:grid-cols-3 md:grid-cols-5 gap-3">
          <div>
            <Label className="text-xs font-bold text-[#344054]">Transportation Mode*</Label>
            <Select value={transportationMode} onValueChange={setTransportationMode}>
              <SelectTrigger data-testid="stock-transfer-transportation-mode-select"><SelectValue /></SelectTrigger>
              <SelectContent>
                {["By Road", "By Rail", "By Air", "By Self"].map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Vehicle No.*</Label>
            <Input value={vehicleNo} onChange={(e) => setVehicleNo(e.target.value.toUpperCase())} placeholder="e.g. UP85ET2398" data-testid="stock-transfer-vehicle-no-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Place Of Supply*</Label>
            <Input value={placeOfSupply} onChange={(e) => setPlaceOfSupply(e.target.value)} placeholder="e.g. Uttar Pradesh" data-testid="stock-transfer-place-of-supply-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">G.R No.*</Label>
            <Input value={grNo} onChange={(e) => setGrNo(e.target.value)} placeholder="e.g. 6839" data-testid="stock-transfer-gr-no-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Date Of Supply*</Label>
            <Input type="date" value={dateOfSupply} onChange={(e) => setDateOfSupply(e.target.value)} data-testid="stock-transfer-date-of-supply-input" />
          </div>
        </div>

        {formError && (
          <div className="bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-3 text-sm text-[#912018] flex items-center gap-2" data-testid="stock-transfer-form-error">
            <WarningCircle size={16} /> {formError}
          </div>
        )}

        <Button onClick={openConfirmDialog} disabled={submitting} className="w-full sm:w-auto" data-testid="stock-transfer-create-button">
          Review &amp; Create Stock Transfer Order <ArrowRight size={14} className="ml-1.5" />
        </Button>

        {/* Recent orders */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="stock-transfer-recent-card">
          <div className="px-3 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB]">
            <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">Recent Stock Transfer Orders ({recentOrders.length})</h3>
          </div>
          {loadingRecent ? (
            <p className="p-4 text-xs text-[#667085]">Loading...</p>
          ) : recentOrders.length === 0 ? (
            <p className="p-4 text-xs text-[#98A2B3]" data-testid="stock-transfer-recent-empty">No Stock Transfer Orders yet.</p>
          ) : (
            <table className="w-full text-[12px] border-collapse min-w-[900px]" data-testid="stock-transfer-recent-table">
              <thead>
                <tr>
                  {["STO ID", "Created", "By", "Ship-from", "Ship-to", "Location", "Items", "Delivery Date", "SAP Order ID", "Status", "Goods Issue", "GST Push"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-[11px] font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {recentOrders.map((o) => {
                  const badge = o.error_message
                    ? { label: "Issue Found", className: "bg-[#FEF3F2] text-[#B42318]" }
                    : o.status === "created_in_sap"
                    ? { label: "Created in SAP", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : { label: "Pending SAP", className: "bg-[#FEF0C7] text-[#93370D]" };
                  const giBadge = o.gi_status === "posted"
                    ? { label: "GI Posted", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : o.gi_status === "failed"
                    ? { label: "GI Failed", className: "bg-[#FEF3F2] text-[#B42318]" }
                    : o.gi_status === "not_found_timeout"
                    ? { label: "GI Pending (20min+)", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : o.gi_status === "awaiting_delivery"
                    ? { label: "Awaiting Delivery", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : null;
                  const gstBadge = o.gst_note_pushed
                    ? { label: "GST Recorded", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : null;
                  return (
                    <tr
                      key={o.sto_id}
                      onClick={() => setSelectedOrder(o)}
                      className="cursor-pointer hover:bg-[#F9FAFB]"
                      data-testid={`stock-transfer-recent-row-${o.sto_id}`}
                    >
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium text-[#0E7C86] underline">{o.sto_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{new Date(o.created_at).toLocaleString("en-IN")}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.created_by}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.ship_from_site_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.ship_to_site_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.ship_to_location_name || o.ship_to_location_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.items?.length || 0}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.requested_delivery_date}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono" data-testid={`stock-transfer-recent-sap-id-${o.sto_id}`}>{formatSapId(o.sap_order_id) || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">
                        <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${badge.className}`}>{badge.label}</span>
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">
                        {giBadge ? <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${giBadge.className}`}>{giBadge.label}</span> : <span className="text-[#98A2B3]">—</span>}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">
                        {gstBadge ? <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${gstBadge.className}`}>{gstBadge.label}</span> : <span className="text-[#98A2B3]">—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </main>

      {/* 2x-safety confirmation before the real, irreversible live SAP
          write (user's explicit ask, Aug 2026) - clicking "Review &
          Create" above only opens this summary; nothing is submitted to
          SAP until "Confirm & Submit to SAP" is explicitly clicked here. */}
      <Dialog open={showConfirmDialog} onOpenChange={(open) => { if (!submitting) { setShowConfirmDialog(open); if (!open) { setSapSubmitPhase(null); setSapSubmitMessage(null); setStepStatuses({}); } } }}>
        <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto" data-testid="stock-transfer-confirm-dialog">
          <DialogHeader>
            <DialogTitle>Confirm Stock Transfer Order</DialogTitle>
            <DialogDescription>
              This will be written LIVE to SAP immediately - it cannot be undone from this app. Please review carefully.
            </DialogDescription>
          </DialogHeader>

          {sapSubmitPhase ? (
            <div className="space-y-3" data-testid="stock-transfer-sap-submit-status">
              <ol className="space-y-2.5">
                {STO_STEPS.map((step) => {
                  const state = step.key === "validate" ? (stepStatuses.validate || "pending") : (stepStatuses[step.key] || "pending");
                  return (
                    <li key={step.key} className="flex items-center gap-2.5 text-sm" data-testid={`stock-transfer-step-${step.key}`} data-step-state={state}>
                      {state === "done" && <CheckCircle size={18} weight="fill" className="text-[#12B76A] shrink-0" />}
                      {state === "active" && <CircleNotch size={18} className="text-[#7A5AF8] shrink-0 animate-spin" />}
                      {state === "failed" && <WarningCircle size={18} weight="fill" className="text-[#F04438] shrink-0" />}
                      {state === "pending" && <span className="w-[18px] h-[18px] rounded-full border-2 border-[#D0D5DD] shrink-0" />}
                      <span className={state === "pending" ? "text-[#98A2B3]" : state === "failed" ? "text-[#912018] font-medium" : "text-[#344054] font-medium"}>{step.label}</span>
                    </li>
                  );
                })}
              </ol>
              {sapSubmitPhase !== "submitting" && (
                <div
                  className={`rounded-sm p-3 text-sm flex items-start gap-2 ${
                    sapSubmitPhase === "done" ? "bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
                    : "bg-[#FEF3F2] border border-[#FDA29B] text-[#912018]"
                  }`}
                  data-testid="stock-transfer-sap-submit-result"
                >
                  {sapSubmitPhase === "done" ? <CheckCircle size={16} className="mt-0.5 shrink-0" /> : <WarningCircle size={16} className="mt-0.5 shrink-0" />}
                  <p>{sapSubmitMessage}</p>
                </div>
              )}
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
                <div><Label className="text-xs font-bold text-[#344054]">Ship-from Site</Label><p data-testid="stock-transfer-confirm-ship-from">{shipFromSiteId || "—"}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Ship-to Site</Label><p data-testid="stock-transfer-confirm-ship-to">{shipToSiteId}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Ship-to Location</Label><p>{shipToLocationOptions.find((w) => w.warehouse_id === shipToLocationId)?.warehouse_name || shipToLocationId}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Delivery Priority</Label><p>Immediate</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Requested Delivery Date</Label><p>{requestedDeliveryDate}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Transportation Mode</Label><p>{transportationMode}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Vehicle No.</Label><p>{vehicleNo}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Place Of Supply</Label><p>{placeOfSupply}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">G.R No.</Label><p>{grNo}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Date Of Supply</Label><p>{dateOfSupply}</p></div>
              </div>
              <div className="border border-[#EAECF0] rounded-sm overflow-auto">
                <table className="w-full text-xs border-collapse" data-testid="stock-transfer-confirm-items-table">
                  <thead>
                    <tr>
                      {["Product", "Source Warehouse", "Requested Qty"].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-[11px] font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((i) => (
                      <tr key={i.key}>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{i.product_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5">{i.source_warehouse_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(Number(i.requested_qty))} {i.unit_of_measure}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex justify-end gap-2 pt-1">
                <Button type="button" variant="outline" onClick={() => setShowConfirmDialog(false)} data-testid="stock-transfer-confirm-cancel-button">Cancel, go back</Button>
                <Button type="button" onClick={confirmAndSubmit} disabled={submitting} data-testid="stock-transfer-confirm-submit-button">
                  {submitting ? "Submitting..." : "Confirm & Submit to SAP"}
                </Button>
              </div>
            </>
          )}

          {sapSubmitPhase && sapSubmitPhase !== "submitting" && (
            <div className="flex justify-end pt-1">
              <Button type="button" onClick={() => { setShowConfirmDialog(false); setSapSubmitPhase(null); setSapSubmitMessage(null); setStepStatuses({}); }} data-testid="stock-transfer-confirm-close-button">Close</Button>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Detail modal (Aug 2026, user's explicit ask) - clicking any row
          above opens the full breakdown; any SAP-write failure's
          error_message is surfaced here verbatim, human-readable, never a
          raw stack trace. */}
      <Dialog open={!!selectedOrder} onOpenChange={(open) => !open && setSelectedOrder(null)}>
        <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto" data-testid="stock-transfer-detail-modal">
          {selectedOrder && (
            <>
              <DialogHeader>
                <DialogTitle>{selectedOrder.sto_id}</DialogTitle>
                <DialogDescription>
                  Created {new Date(selectedOrder.created_at).toLocaleString("en-IN")} by {selectedOrder.created_by}
                </DialogDescription>
              </DialogHeader>

              {selectedOrder.error_message ? (
                <div className="bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-3 text-sm text-[#912018] flex items-start gap-2" data-testid="stock-transfer-detail-error">
                  <WarningCircle size={16} className="mt-0.5 shrink-0" />
                  <div>
                    <p className="font-bold">SAP rejected this order:</p>
                    <p className="mt-0.5">{cleanSapMessage(selectedOrder.error_message)}</p>
                  </div>
                </div>
              ) : selectedOrder.status === "created_in_sap" ? (
                <div className="bg-[#ECFDF3] border border-[#ABEFC6] rounded-sm p-3 text-sm text-[#027A48] flex items-start gap-2" data-testid="stock-transfer-detail-success">
                  <CheckCircle size={16} className="mt-0.5 shrink-0" />
                  <div>
                    <p className="font-bold">Created in SAP.</p>
                    <p className="mt-0.5">SAP Order ID: {formatSapId(selectedOrder.sap_order_id) || "—"}{selectedOrder.sap_order_uuid ? ` (UUID: ${selectedOrder.sap_order_uuid})` : ""}</p>
                  </div>
                </div>
              ) : (
                <div className="bg-[#FEF0C7] border border-[#FEDF89] rounded-sm p-3 text-sm text-[#93370D]" data-testid="stock-transfer-detail-no-error">
                  Submitting to SAP now - refresh in a few seconds if this doesn't update.
                </div>
              )}

              {selectedOrder.status === "created_in_sap" && (
                <div
                  className={`rounded-sm p-3 text-sm flex items-start gap-2 ${
                    selectedOrder.gi_status === "posted" ? "bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
                    : selectedOrder.gi_status === "failed" ? "bg-[#FEF3F2] border border-[#FDA29B] text-[#912018]"
                    : "bg-[#FEF0C7] border border-[#FEDF89] text-[#93370D]"
                  }`}
                  data-testid="stock-transfer-detail-gi-status"
                >
                  {selectedOrder.gi_status === "posted" ? <CheckCircle size={16} className="mt-0.5 shrink-0" /> : selectedOrder.gi_status === "failed" ? <WarningCircle size={16} className="mt-0.5 shrink-0" /> : null}
                  <div>
                    <p className="font-bold">
                      {selectedOrder.gi_status === "posted" ? "Goods Issue posted - delivery released."
                        : selectedOrder.gi_status === "failed" ? "Goods Issue failed:"
                        : selectedOrder.gi_status === "not_found_timeout" ? "Goods Issue still pending after 20 min - the order itself is unaffected."
                        : "Goods Issue: waiting for SAP to schedule the delivery..."}
                    </p>
                    {selectedOrder.gi_status === "failed" && <p className="mt-0.5">{parseGiError(selectedOrder.gi_error) || "See logs."}</p>}
                    {selectedOrder.outbound_delivery_object_id && (selectedOrder.gi_status === "posted" || selectedOrder.gi_status === "failed") && (
                      <p className="mt-0.5 text-xs opacity-80">Outbound Delivery Request: {selectedOrder.outbound_delivery_object_id}</p>
                    )}
                  </div>
                </div>
              )}

              {selectedOrder.gst_note_pushed && (
                <div
                  className="rounded-sm p-3 text-sm flex items-start gap-2 bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
                  data-testid="stock-transfer-detail-gst-status"
                >
                  <CheckCircle size={16} className="mt-0.5 shrink-0" />
                  <p className="font-bold">GST / Transport details recorded on the SAP Customer Requirement note.</p>
                </div>
              )}

              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
                <div><Label className="text-xs font-bold text-[#344054]">Ship-from Site</Label><p data-testid="stock-transfer-detail-ship-from">{selectedOrder.ship_from_site_id}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Ship-to Site</Label><p data-testid="stock-transfer-detail-ship-to">{selectedOrder.ship_to_site_id}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Ship-to Location</Label><p data-testid="stock-transfer-detail-ship-to-location">{selectedOrder.ship_to_location_name || selectedOrder.ship_to_location_id}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Delivery Priority</Label><p>{selectedOrder.delivery_priority}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Requested Delivery Date</Label><p>{selectedOrder.requested_delivery_date}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Transportation Mode</Label><p>{selectedOrder.transportation_mode || "—"}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Vehicle No.</Label><p>{selectedOrder.vehicle_no || "—"}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Place Of Supply</Label><p>{selectedOrder.place_of_supply || "—"}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">G.R No.</Label><p>{selectedOrder.gr_no || "—"}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Date Of Supply</Label><p>{selectedOrder.date_of_supply || "—"}</p></div>
              </div>

              <div className="border border-[#EAECF0] rounded-sm overflow-auto">
                <table className="w-full text-xs border-collapse" data-testid="stock-transfer-detail-items-table">
                  <thead>
                    <tr>
                      {["Line", "Product", "Description", "Source Warehouse", "Available Qty", "Requested Qty", "Stock Status"].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-[11px] font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(selectedOrder.items || []).map((it) => (
                      <tr key={it.line_no} data-testid={`stock-transfer-detail-item-${it.product_id}`}>
                        <td className="border border-[#D0D5DD] px-2 py-1.5">{it.line_no}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{it.product_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "—"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5">{it.source_warehouse_name || it.source_warehouse_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.available_qty)} {it.unit_of_measure}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.requested_qty)} {it.unit_of_measure}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5">{it.availability_status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
