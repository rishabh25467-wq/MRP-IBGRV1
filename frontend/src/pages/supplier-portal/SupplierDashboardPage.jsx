import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import {
  Buildings, SignOut, Package, PlugsConnected, Truck, MagnifyingGlass,
  X, Eye, ListChecks, ArrowRight, Flask, CheckCircle,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  return String(detail);
}

function fmtMoney(amount, currency) {
  if (amount === null || amount === undefined) return "-";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: currency || "INR", maximumFractionDigits: 2 }).format(amount);
  } catch {
    return amount.toFixed(2);
  }
}

// User's ask (Sep 2 2026): dates were wrapping onto 2 lines in the PO
// table ("2026-08-\n21") - short DD-MMM-YY format fits on one line.
function fmtDate(dateStr) {
  if (!dateStr) return "-";
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return dateStr;
  const day = String(d.getDate()).padStart(2, "0");
  const month = d.toLocaleString("en-US", { month: "short" });
  const year = String(d.getFullYear()).slice(-2);
  return `${day}-${month}-${year}`;
}

// Sep 2 2026, user's explicit ask - Open Qty is now fetched live from
// SAP (fetch_open_po_quantities, refreshed ~every 20 min in the
// background) instead of only computed from this app's own shipment
// records, so a receipt made outside this app still counts. This
// shows the supplier/staff how fresh that SAP read is.
function timeAgo(isoString) {
  const diffMin = Math.max(0, Math.round((Date.now() - new Date(isoString).getTime()) / 60000));
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  return `${Math.round(diffMin / 60)}h ago`;
}

const cartKey = (po) => `${po.po_number}::${po.item_number}`;

export default function SupplierDashboardPage() {
  const { account, logout } = useSupplierAuth();
  const navigate = useNavigate();
  const { vendorCode } = useParams();
  const isImpersonating = !!(account?.testing_mode && vendorCode && vendorCode !== account?.vendor_code);

  const [pos, setPos] = useState([]);
  const [liveSync, setLiveSync] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  const [cart, setCart] = useState({});
  const [reviewOpen, setReviewOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [successCode, setSuccessCode] = useState(null);

  // Entity restriction ("mrp vendor side changes.docx", Sep 2026): a
  // vendor with POs from more than one buyer entity (RI/RT) must pick ONE
  // to view/act on at a time - never a mixed table, never a mixed cart.
  const [selectedEntity, setSelectedEntity] = useState(null);

  const [detailPoNumber, setDetailPoNumber] = useState(null);

  // TESTING ONLY (user's explicit ask, Aug 28 2026) - impersonate any
  // vendor's data via a search box. Gated by account.testing_mode
  // (mirrors backend SUPPLIER_PORTAL_TESTING_MODE) - REMOVE BEFORE LAUNCH.
  const [vendorSearch, setVendorSearch] = useState("");
  const [vendorResults, setVendorResults] = useState([]);
  const [vendorDropdownOpen, setVendorDropdownOpen] = useState(false);

  const load = async () => {
    try {
      const params = isImpersonating ? { as_vendor: vendorCode } : {};
      const { data } = await supplierApi.get("/purchase-orders", { params });
      setPos(data.purchase_orders || []);
      setLiveSync(!!data.live_sync);
      setError("");
    } catch (e) {
      setError(e.response?.data?.detail || "Could not load your Purchase Orders.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    load();
    setCart({});
    setSelectedEntity(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorCode]);

  // Every distinct buying entity (RI/RT) present across this vendor's own
  // POs - built from the same `buyer_code`/`buyer_entity_name` fields
  // already used for the "PO From" column.
  const entities = useMemo(() => {
    const map = new Map();
    pos.forEach((po) => {
      const code = po.buyer_code || "RT";
      if (!map.has(code)) map.set(code, po.buyer_entity_name || code);
    });
    return Array.from(map.entries()).map(([code, name]) => ({ code, name }));
  }, [pos]);

  // Auto-pick when there's only one entity to choose from (nothing to
  // force) - otherwise leave it unselected until the vendor picks one, and
  // reset if the previously-selected entity's POs disappeared (e.g. all
  // finished, or an impersonation switch).
  useEffect(() => {
    if (entities.length === 1) {
      setSelectedEntity(entities[0].code);
    } else if (selectedEntity && !entities.some((e) => e.code === selectedEntity)) {
      setSelectedEntity(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities]);

  const chooseEntity = (code) => {
    if (code === selectedEntity) return;
    setSelectedEntity(code);
    setCart({});
  };

  const entityPos = useMemo(
    () => (selectedEntity ? pos.filter((po) => (po.buyer_code || "RT") === selectedEntity) : []),
    [pos, selectedEntity]
  );

  useEffect(() => {
    if (!account?.testing_mode || !vendorSearch.trim()) {
      setVendorResults([]);
      return;
    }
    const t = setTimeout(async () => {
      try {
        const { data } = await supplierApi.get("/testing/vendor-directory", { params: { q: vendorSearch } });
        setVendorResults(data.vendors || []);
      } catch {
        setVendorResults([]);
      }
    }, 250);
    return () => clearTimeout(t);
  }, [vendorSearch, account?.testing_mode]);

  const filteredPos = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return entityPos;
    return entityPos.filter((po) => [po.po_number, po.item_number, po.description, po.product_id].some((v) => (v || "").toString().toLowerCase().includes(q)));
  }, [entityPos, search]);

  // Bulk Cart Add - group by PO Number (open items only) so a single
  // "select all" toggle can act on the whole group; anchored to the
  // first OPEN row of each group (not just the first row overall) and
  // pre-computed once here instead of re-filtering on every click/render
  // (code review feedback, iteration_124).
  const rowsWithPoGroup = useMemo(() => {
    const groups = {};
    filteredPos.forEach((po) => {
      if (po.remaining_qty > 0) {
        groups[po.po_number] = groups[po.po_number] || [];
        groups[po.po_number].push(po);
      }
    });
    const seenOpenPo = new Set();
    return filteredPos.map((po) => {
      const isOpen = po.remaining_qty > 0;
      const isFirstOpenOfPo = isOpen && !seenOpenPo.has(po.po_number);
      if (isOpen) seenOpenPo.add(po.po_number);
      const openRows = groups[po.po_number] || [];
      return { ...po, _isFirstOpenOfPo: isFirstOpenOfPo, _poOpenRows: openRows };
    });
  }, [filteredPos]);

  const detailItems = useMemo(() => pos.filter((po) => po.po_number === detailPoNumber), [pos, detailPoNumber]);

  const toggleCartItem = (po, checked) => {
    setCart((prev) => {
      const next = { ...prev };
      const key = cartKey(po);
      if (checked) {
        next[key] = { po_number: po.po_number, item_number: po.item_number, description: po.description, unit_of_measure: po.unit_of_measure, remaining_qty: po.remaining_qty, ship_qty: "" };
      } else {
        delete next[key];
      }
      return next;
    });
  };

  const isPoFullySelected = (rows) => rows.length > 0 && rows.every((p) => !!cart[cartKey(p)]);

  const toggleSelectAllForPo = (rows) => {
    const allSelected = isPoFullySelected(rows);
    setCart((prev) => {
      const next = { ...prev };
      rows.forEach((p) => {
        const key = cartKey(p);
        if (allSelected) {
          delete next[key];
        } else {
          // Left blank, same as an individual checkbox - a full-remaining-qty
          // prefill across every row in one click was flagged as risky
          // (code review feedback, iteration_124).
          next[key] = { po_number: p.po_number, item_number: p.item_number, description: p.description, unit_of_measure: p.unit_of_measure, remaining_qty: p.remaining_qty, ship_qty: "" };
        }
      });
      return next;
    });
  };

  const updateCartQty = (key, value) => {
    setCart((prev) => ({ ...prev, [key]: { ...prev[key], ship_qty: value } }));
  };

  const removeFromCart = (key) => {
    setCart((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const cartItems = Object.entries(cart).map(([key, v]) => ({ key, ...v }));

  const submitShipment = async () => {
    setSubmitting(true);
    setSubmitError("");
    try {
      const items = cartItems.map((c) => ({ po_number: c.po_number, item_number: c.item_number, ship_qty: parseFloat(c.ship_qty) }));
      const params = isImpersonating ? { as_vendor: vendorCode } : {};
      const { data } = await supplierApi.post("/shipments", { items }, { params });
      setCart({});
      setConfirmOpen(false);
      setReviewOpen(false);
      setTimeout(() => setSuccessCode(data._id), 200);
      load();
    } catch (e) {
      setSubmitError(formatApiErrorDetail(e.response?.data?.detail));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F2F4F7] font-sans" data-testid="supplier-dashboard-page">
      <div className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] text-white px-6 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <Buildings size={20} weight="fill" className="text-white" />
          <span className="font-heading font-bold tracking-tight">Supplier Portal</span>
        </div>
        {account?.testing_mode && (
          <div className="relative flex-1 max-w-xs" data-testid="supplier-vendor-impersonation-search">
            <div className="flex items-center gap-1 bg-white/15 rounded-sm px-2 py-1">
              <Flask size={13} className="text-white/70 shrink-0" />
              <Input
                placeholder="Testing: view as vendor..."
                value={vendorSearch}
                onChange={(e) => { setVendorSearch(e.target.value); setVendorDropdownOpen(true); }}
                onFocus={() => setVendorDropdownOpen(true)}
                className="h-6 border-none bg-transparent text-white placeholder:text-white/60 focus-visible:ring-0 px-1 text-xs"
                data-testid="supplier-vendor-search-input"
              />
            </div>
            {vendorDropdownOpen && vendorResults.length > 0 && (
              <div className="absolute top-full left-0 mt-1 w-full bg-white text-[#1D2939] rounded-sm shadow-lg border border-[#D0D5DD] max-h-56 overflow-y-auto z-20" data-testid="supplier-vendor-search-results">
                {vendorResults.map((v) => (
                  <button
                    key={v.vendor_code}
                    onClick={() => { setVendorDropdownOpen(false); setVendorSearch(""); navigate(`/supplier-portal/dashboard/${v.vendor_code}`); }}
                    className="w-full text-left px-3 py-2 text-xs hover:bg-[#F0F4F8] flex justify-between gap-2"
                    data-testid={`supplier-vendor-search-result-${v.vendor_code}`}
                  >
                    <span className="truncate">{v.vendor_name || v.vendor_code}</span>
                    <span className="font-data text-[#475467] shrink-0">{v.vendor_code}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="flex items-center gap-4">
          <Link to={`/supplier-portal/shipments/${vendorCode}`} data-testid="supplier-nav-shipments-link">
            <Button variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150">
              <ListChecks size={14} className="mr-1" /> Shipments <ArrowRight size={12} className="ml-1" />
            </Button>
          </Link>
          <div className="text-right" data-testid="supplier-dashboard-vendor-info">
            <div className="text-sm font-semibold" data-testid="supplier-dashboard-company-name">{isImpersonating ? `Viewing: ${vendorCode}` : account?.company_name}</div>
            <div className="text-xs text-white/75 font-data">Vendor Code: {vendorCode}</div>
          </div>
          <Button onClick={logout} variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150" data-testid="supplier-dashboard-logout-button">
            <SignOut size={14} className="mr-1" /> Sign Out
          </Button>
        </div>
      </div>

      <div className="max-w-6xl mx-auto p-4 md:p-6 pb-24">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h1 className="font-heading text-xl font-bold text-[#1D2939]">Your Open Purchase Orders</h1>
            <p className="text-sm text-[#475467] mt-1 font-data">Vendor Code {vendorCode} · {account?.email}</p>
          </div>
          <div className="relative">
            <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#475467]" />
            <Input
              placeholder="Search PO #, Item #, or description..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 pr-2 w-72 text-[13px] rounded-sm border-[#D0D5DD] focus-visible:border-[#004B87] focus-visible:ring-1 focus-visible:ring-[#004B87]"
              data-testid="supplier-po-search-input"
            />
          </div>
        </div>

        {loading && <div className="mt-8 text-sm text-[#475467]" data-testid="supplier-dashboard-loading">Loading your Purchase Orders...</div>}

        {!loading && entities.length > 1 && (
          <div className="mt-4 flex items-center gap-2 flex-wrap" data-testid="supplier-entity-switcher">
            <span className="text-xs font-semibold text-[#475467] uppercase">Entity</span>
            {entities.map((e) => (
              <button
                key={e.code}
                onClick={() => chooseEntity(e.code)}
                className={`text-xs font-semibold px-3 py-1.5 rounded-sm border transition-colors duration-150 ${
                  selectedEntity === e.code
                    ? "bg-[#004B87] border-[#004B87] text-white"
                    : "bg-white border-[#D0D5DD] text-[#1D2939] hover:border-[#004B87]"
                }`}
                data-testid={`supplier-entity-option-${e.code}`}
              >
                {e.name} ({e.code})
              </button>
            ))}
          </div>
        )}

        {!loading && entities.length > 1 && !selectedEntity && (
          <div className="mt-6 bg-white border border-[#D0D5DD] rounded-sm shadow-sm p-8 text-center" data-testid="supplier-entity-pick-required">
            <Buildings size={32} weight="fill" className="text-[#004B87] mx-auto mb-3" />
            <h2 className="font-heading font-bold text-[#1D2939]">Choose an entity to continue</h2>
            <p className="text-sm text-[#475467] mt-1 max-w-md mx-auto">
              You have open Purchase Orders from more than one entity - pick one above to view and act on its POs.
            </p>
          </div>
        )}

        {!loading && !liveSync && pos.length > 0 && (
          <div className="mt-4 text-xs text-[#8A6116] bg-[#E3A008]/15 border border-[#E3A008]/30 rounded-sm px-3 py-2" data-testid="supplier-dashboard-stale-banner">
            Showing the last synced data - live SAP connection is still being set up.
          </div>
        )}

        {!loading && !liveSync && pos.length === 0 && (
          <div className="mt-6 bg-white border border-[#D0D5DD] rounded-sm shadow-sm p-8 text-center" data-testid="supplier-dashboard-not-configured">
            <PlugsConnected size={32} weight="fill" className="text-[#E3A008] mx-auto mb-3" />
            <h2 className="font-heading font-bold text-[#1D2939]">SAP Connection Coming Soon</h2>
            <p className="text-sm text-[#475467] mt-1 max-w-md mx-auto">
              Your account is approved. We're still connecting this portal to SAP - your Purchase Orders will appear here automatically once that's live.
            </p>
          </div>
        )}

        {!loading && error && (
          <div className="mt-6 text-sm text-[#B91C1C] bg-[#E02424]/10 border border-[#E02424]/30 rounded-sm px-4 py-3" data-testid="supplier-dashboard-error">{error}</div>
        )}

        {!loading && liveSync && !error && pos.length === 0 && (
          <div className="mt-6 bg-white border border-[#D0D5DD] rounded-sm shadow-sm p-8 text-center" data-testid="supplier-dashboard-empty">
            <Package size={32} weight="fill" className="text-[#475467] mx-auto mb-3" />
            <p className="text-sm text-[#475467]">No open Purchase Orders right now.</p>
          </div>
        )}

        {!loading && filteredPos.length > 0 && (
          <div className="mt-4 bg-white border border-[#D0D5DD] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto" data-testid="supplier-dashboard-po-table">
            <table className="w-full text-[13px] border-collapse">
              <thead className="bg-[#EAECF0] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                <tr>
                  <th className="border border-[#D0D5DD] p-1.5 text-left w-8"></th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">PO Number</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">Item</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">PO From</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">PO Date</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Unit Price</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Subtotal</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right">Open Qty</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-left">Due Date</th>
                  <th className="border border-[#D0D5DD] p-1.5 text-right w-28">Ship Qty</th>
                </tr>
              </thead>
              <tbody>
                {rowsWithPoGroup.map((po, i) => {
                  const key = cartKey(po);
                  const inCart = !!cart[key];
                  return (
                    <tr key={i} className="bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" data-testid={`supplier-po-row-${po.po_number}-${po.item_number}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1">
                        <Checkbox
                          checked={inCart}
                          disabled={po.remaining_qty <= 0}
                          onCheckedChange={(c) => toggleCartItem(po, !!c)}
                          data-testid={`supplier-po-checkbox-${po.po_number}-${po.item_number}`}
                        />
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data">
                        <button
                          onClick={() => setDetailPoNumber(po.po_number)}
                          className="text-[#004B87] hover:underline underline-offset-2 flex items-center gap-1"
                          data-testid={`supplier-po-detail-link-${po.po_number}`}
                        >
                          <Eye size={12} /> {po.po_number}
                        </button>
                        {po._isFirstOpenOfPo && po._poOpenRows.length > 1 && (
                          <button
                            onClick={() => toggleSelectAllForPo(po._poOpenRows)}
                            className="text-xs text-[#004B87] hover:underline underline-offset-2 mt-0.5 block whitespace-nowrap"
                            data-testid={`supplier-po-select-all-${po.po_number}`}
                          >
                            {isPoFullySelected(po._poOpenRows) ? "Deselect all" : `Select all ${po._poOpenRows.length} items`}
                          </button>
                        )}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1">{po.description || po.product_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-xs">{po.buyer_entity_name}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data text-xs whitespace-nowrap">{fmtDate(po.po_date)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data text-xs">{fmtMoney(po.unit_price, po.currency)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data text-xs">{fmtMoney(po.subtotal, po.currency)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right font-data">
                        {po.remaining_qty} {po.unit_of_measure}
                        <div className="text-[10px] text-[#98A2B3] font-sans flex items-center justify-end gap-1" data-testid={`supplier-po-sap-verified-${po.po_number}-${po.item_number}`}>
                          {po.sap_verified_at ? (
                            <>
                              SAP-verified {timeAgo(po.sap_verified_at)}
                              <CheckCircle size={12} weight="fill" className="text-[#12B76A]" data-testid={`supplier-po-sap-verified-tick-${po.po_number}-${po.item_number}`} />
                            </>
                          ) : (
                            "not yet verified in SAP"
                          )}
                        </div>
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-data text-xs whitespace-nowrap">{fmtDate(po.due_date)}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-right">
                        <Input
                          type="number"
                          disabled={!inCart}
                          value={inCart ? cart[key].ship_qty : ""}
                          onChange={(e) => updateCartQty(key, e.target.value)}
                          className="h-7 w-24 text-right rounded-sm border-[#D0D5DD] text-xs"
                          data-testid={`supplier-po-qty-input-${po.po_number}-${po.item_number}`}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {!loading && (entities.length <= 1 || selectedEntity) && pos.length > 0 && filteredPos.length === 0 && (
          <div className="mt-6 text-sm text-[#475467] text-center" data-testid="supplier-po-search-empty">No POs match "{search}".</div>
        )}
      </div>

      {cartItems.length > 0 && (
        <div className="fixed bottom-0 left-0 right-0 bg-white border-t border-[#D0D5DD] shadow-[0_-2px_8px_0_rgba(16,24,40,0.08)] px-4 py-3 flex items-center justify-between z-20" data-testid="supplier-cart-bar">
          <span className="text-sm text-[#1D2939] font-semibold" data-testid="supplier-cart-count">{cartItems.length} item{cartItems.length > 1 ? "s" : ""} selected</span>
          <Button onClick={() => setReviewOpen(true)} className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] transition-colors duration-150" data-testid="supplier-review-shipment-button">
            <Truck size={14} className="mr-1" /> Review Shipment
          </Button>
        </div>
      )}

      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent className="rounded-sm max-w-lg" data-testid="supplier-review-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Review Shipment</DialogTitle>
            <DialogDescription>Confirm the items and quantities before generating your shipment code.</DialogDescription>
          </DialogHeader>
          <div className="max-h-72 overflow-y-auto divide-y divide-[#D0D5DD]">
            {cartItems.map((c) => (
              <div key={c.key} className="flex items-center gap-2 py-2" data-testid={`supplier-review-row-${c.key}`}>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium truncate">{c.description || c.item_number}</div>
                  <div className="text-xs text-[#475467] font-data">PO {c.po_number} · Item {c.item_number}</div>
                </div>
                <Input
                  type="number"
                  value={c.ship_qty}
                  onChange={(e) => updateCartQty(c.key, e.target.value)}
                  className="h-8 w-24 text-right rounded-sm border-[#D0D5DD] text-xs"
                  data-testid={`supplier-review-qty-input-${c.key}`}
                />
                <span className="text-xs text-[#475467] w-10">{c.unit_of_measure}</span>
                <button onClick={() => removeFromCart(c.key)} className="text-[#B91C1C] hover:bg-[#E02424]/10 rounded-sm p-1" data-testid={`supplier-review-remove-${c.key}`}>
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setReviewOpen(false)}>Cancel</Button>
            <Button
              onClick={() => setConfirmOpen(true)}
              disabled={cartItems.length === 0}
              className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] transition-colors duration-150"
              data-testid="supplier-review-confirm-button"
            >
              Confirm Shipment
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="rounded-sm" data-testid="supplier-confirm-generate-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Generate Shipment Code?</DialogTitle>
            <DialogDescription>
              This will create one shipment covering {cartItems.length} item{cartItems.length > 1 ? "s" : ""} across {new Set(cartItems.map((c) => c.po_number)).size} PO(s). You can still edit its contents until it's received - are you sure?
            </DialogDescription>
          </DialogHeader>
          {submitError && <div className="text-sm text-[#B91C1C]" data-testid="supplier-submit-error">{submitError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button onClick={submitShipment} disabled={submitting} className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] transition-colors duration-150" data-testid="supplier-confirm-generate-button">
              {submitting ? "Generating..." : "Yes, Generate Code"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!successCode} onOpenChange={(o) => !o && setSuccessCode(null)}>
        <DialogContent className="rounded-sm" data-testid="supplier-ship-success-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Shipment Created</DialogTitle>
            <DialogDescription>Write this code on your shipment paperwork - it's how the warehouse matches it on arrival. You can still edit its contents from the Shipments page until it's received.</DialogDescription>
          </DialogHeader>
          <div className="text-center py-4 bg-[#F2F4F7] border border-[#D0D5DD] rounded-sm">
            <div className="text-3xl font-data font-bold tracking-widest text-[#004B87]" data-testid="supplier-ship-success-code">{successCode}</div>
          </div>
          <Button onClick={() => setSuccessCode(null)} className="rounded-sm bg-[#004B87] hover:bg-[#003A6A] transition-colors duration-150" data-testid="supplier-ship-success-close-button">Done</Button>
        </DialogContent>
      </Dialog>

      <Dialog open={!!detailPoNumber} onOpenChange={(o) => !o && setDetailPoNumber(null)}>
        <DialogContent className="rounded-sm max-w-2xl" data-testid="supplier-po-detail-modal">
          <DialogHeader>
            <DialogTitle className="font-heading font-data">PO {detailPoNumber}</DialogTitle>
            <DialogDescription>
              {detailItems[0]?.buyer_entity_name} · Ordered {fmtDate(detailItems[0]?.po_date)}
            </DialogDescription>
          </DialogHeader>
          <table className="w-full text-sm border-collapse">
            <thead className="text-[#475467] text-xs uppercase">
              <tr>
                <th className="text-left py-1 font-semibold">Item</th>
                <th className="text-left py-1 font-semibold">Description</th>
                <th className="text-right py-1 font-semibold">Qty</th>
                <th className="text-right py-1 font-semibold">Unit Price</th>
                <th className="text-right py-1 font-semibold">Subtotal</th>
                <th className="text-left py-1 font-semibold">Due Date</th>
              </tr>
            </thead>
            <tbody>
              {detailItems.map((it, i) => (
                <tr key={i} className="border-t border-[#D0D5DD]">
                  <td className="py-1.5 font-data">{it.item_number}</td>
                  <td className="py-1.5">{it.description}</td>
                  <td className="py-1.5 text-right font-data">{it.po_qty} {it.unit_of_measure}</td>
                  <td className="py-1.5 text-right font-data">{fmtMoney(it.unit_price, it.currency)}</td>
                  <td className="py-1.5 text-right font-data">{fmtMoney(it.subtotal, it.currency)}</td>
                  <td className="py-1.5 font-data whitespace-nowrap">{fmtDate(it.due_date)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t-2 border-[#D0D5DD]">
                <td colSpan={4} className="py-2 text-right font-semibold text-xs text-[#475467]">PO Total</td>
                <td className="py-2 text-right font-data font-bold">{fmtMoney(detailItems.reduce((s, it) => s + (it.subtotal || 0), 0), detailItems[0]?.currency)}</td>
                <td></td>
              </tr>
            </tfoot>
          </table>
        </DialogContent>
      </Dialog>
    </div>
  );
}
