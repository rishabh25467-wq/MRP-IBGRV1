import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import {
  Buildings, SignOut, Package, PlugsConnected, Truck, MagnifyingGlass,
  X, Eye, ListChecks, ArrowRight, Flask,
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorCode]);

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
    if (!q) return pos;
    return pos.filter((po) => [po.po_number, po.item_number, po.description, po.product_id].some((v) => (v || "").toString().toLowerCase().includes(q)));
  }, [pos, search]);

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
      setSuccessCode(data._id);
      setCart({});
      setConfirmOpen(false);
      setReviewOpen(false);
      load();
    } catch (e) {
      setSubmitError(formatApiErrorDetail(e.response?.data?.detail));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="supplier-dashboard-page">
      <div className="bg-[#0076CC] text-white px-6 py-3 flex items-center justify-between shadow-sm gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <Buildings size={20} weight="fill" className="text-white" />
          <span className="font-sans font-bold tracking-tight">Supplier Portal</span>
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
              <div className="absolute top-full left-0 mt-1 w-full bg-white text-[#111827] rounded-sm shadow-lg border border-[#CBD3DB] max-h-56 overflow-y-auto z-20" data-testid="supplier-vendor-search-results">
                {vendorResults.map((v) => (
                  <button
                    key={v.vendor_code}
                    onClick={() => { setVendorDropdownOpen(false); setVendorSearch(""); navigate(`/supplier-portal/dashboard/${v.vendor_code}`); }}
                    className="w-full text-left px-3 py-2 text-xs hover:bg-[#F5F6F7] flex justify-between gap-2"
                    data-testid={`supplier-vendor-search-result-${v.vendor_code}`}
                  >
                    <span className="truncate">{v.vendor_name || v.vendor_code}</span>
                    <span className="font-data text-[#5B738B] shrink-0">{v.vendor_code}</span>
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
            <h1 className="font-sans text-lg font-bold text-[#111827]">Your Open Purchase Orders</h1>
            <p className="text-sm text-[#5B738B] mt-1 font-data">Vendor Code {vendorCode} · {account?.email}</p>
          </div>
          <div className="relative">
            <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#5B738B]" />
            <Input
              placeholder="Search PO #, Item #, or description..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8 rounded-sm border-[#CBD3DB] focus:ring-2 focus:ring-[#4DA3E0] focus:outline-none w-64"
              data-testid="supplier-po-search-input"
            />
          </div>
        </div>

        {loading && <div className="mt-8 text-sm text-[#5B738B]" data-testid="supplier-dashboard-loading">Loading your Purchase Orders...</div>}

        {!loading && !liveSync && pos.length > 0 && (
          <div className="mt-4 text-xs text-[#8A6116] bg-[#E3A008]/15 border border-[#E3A008]/30 rounded-sm px-3 py-2" data-testid="supplier-dashboard-stale-banner">
            Showing the last synced data - live SAP connection is still being set up.
          </div>
        )}

        {!loading && !liveSync && pos.length === 0 && (
          <div className="mt-6 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-8 text-center" data-testid="supplier-dashboard-not-configured">
            <PlugsConnected size={32} weight="fill" className="text-[#E3A008] mx-auto mb-3" />
            <h2 className="font-sans font-bold text-[#111827]">SAP Connection Coming Soon</h2>
            <p className="text-sm text-[#5B738B] mt-1 max-w-md mx-auto">
              Your account is approved. We're still connecting this portal to SAP - your Purchase Orders will appear here automatically once that's live.
            </p>
          </div>
        )}

        {!loading && error && (
          <div className="mt-6 text-sm text-[#B91C1C] bg-[#E02424]/10 border border-[#E02424]/30 rounded-sm px-4 py-3" data-testid="supplier-dashboard-error">{error}</div>
        )}

        {!loading && liveSync && !error && pos.length === 0 && (
          <div className="mt-6 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-8 text-center" data-testid="supplier-dashboard-empty">
            <Package size={32} weight="fill" className="text-[#5B738B] mx-auto mb-3" />
            <p className="text-sm text-[#5B738B]">No open Purchase Orders right now.</p>
          </div>
        )}

        {!loading && filteredPos.length > 0 && (
          <div className="mt-4 bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-x-auto" data-testid="supplier-dashboard-po-table">
            <table className="w-full text-sm border-collapse">
              <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
                <tr>
                  <th className="text-left px-3 py-2 font-semibold w-8"></th>
                  <th className="text-left px-3 py-2 font-semibold">PO Number</th>
                  <th className="text-left px-3 py-2 font-semibold">Item</th>
                  <th className="text-left px-3 py-2 font-semibold">PO From</th>
                  <th className="text-left px-3 py-2 font-semibold">PO Date</th>
                  <th className="text-right px-3 py-2 font-semibold">Unit Price</th>
                  <th className="text-right px-3 py-2 font-semibold">Subtotal</th>
                  <th className="text-right px-3 py-2 font-semibold">Open Qty</th>
                  <th className="text-left px-3 py-2 font-semibold">Due Date</th>
                  <th className="text-right px-3 py-2 font-semibold w-28">Ship Qty</th>
                </tr>
              </thead>
              <tbody>
                {filteredPos.map((po, i) => {
                  const key = cartKey(po);
                  const inCart = !!cart[key];
                  return (
                    <tr key={i} className="border-b border-[#CBD3DB] hover:bg-[#F5F6F7]/60" data-testid={`supplier-po-row-${po.po_number}-${po.item_number}`}>
                      <td className="px-3 py-2">
                        <Checkbox
                          checked={inCart}
                          disabled={po.remaining_qty <= 0}
                          onCheckedChange={(c) => toggleCartItem(po, !!c)}
                          data-testid={`supplier-po-checkbox-${po.po_number}-${po.item_number}`}
                        />
                      </td>
                      <td className="px-3 py-2 font-data">
                        <button
                          onClick={() => setDetailPoNumber(po.po_number)}
                          className="text-[#0076CC] hover:underline underline-offset-2 flex items-center gap-1"
                          data-testid={`supplier-po-detail-link-${po.po_number}`}
                        >
                          <Eye size={12} /> {po.po_number}
                        </button>
                      </td>
                      <td className="px-3 py-2">{po.description || po.product_id}</td>
                      <td className="px-3 py-2 text-xs">{po.buyer_entity_name}</td>
                      <td className="px-3 py-2 font-data text-xs">{po.po_date || "-"}</td>
                      <td className="px-3 py-2 text-right font-data text-xs">{fmtMoney(po.unit_price, po.currency)}</td>
                      <td className="px-3 py-2 text-right font-data text-xs">{fmtMoney(po.subtotal, po.currency)}</td>
                      <td className="px-3 py-2 text-right font-data">{po.remaining_qty} {po.unit_of_measure}</td>
                      <td className="px-3 py-2 font-data text-xs">{po.due_date || "-"}</td>
                      <td className="px-3 py-2 text-right">
                        <Input
                          type="number"
                          disabled={!inCart}
                          value={inCart ? cart[key].ship_qty : ""}
                          onChange={(e) => updateCartQty(key, e.target.value)}
                          className="h-7 w-24 text-right rounded-sm border-[#CBD3DB] text-xs"
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
        {!loading && pos.length > 0 && filteredPos.length === 0 && (
          <div className="mt-6 text-sm text-[#5B738B] text-center" data-testid="supplier-po-search-empty">No POs match "{search}".</div>
        )}
      </div>

      {cartItems.length > 0 && (
        <div className="fixed bottom-0 left-0 right-0 bg-white border-t border-[#CBD3DB] shadow-[0_-2px_8px_0_rgba(16,24,40,0.08)] px-4 py-3 flex items-center justify-between z-20" data-testid="supplier-cart-bar">
          <span className="text-sm text-[#111827] font-semibold">{cartItems.length} item{cartItems.length > 1 ? "s" : ""} selected</span>
          <Button onClick={() => setReviewOpen(true)} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-review-shipment-button">
            <Truck size={14} className="mr-1" /> Review Shipment
          </Button>
        </div>
      )}

      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent className="rounded-sm max-w-lg" data-testid="supplier-review-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Review Shipment</DialogTitle>
            <DialogDescription>Confirm the items and quantities before generating your shipment code.</DialogDescription>
          </DialogHeader>
          <div className="max-h-72 overflow-y-auto divide-y divide-[#CBD3DB]">
            {cartItems.map((c) => (
              <div key={c.key} className="flex items-center gap-2 py-2" data-testid={`supplier-review-row-${c.key}`}>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium truncate">{c.description || c.item_number}</div>
                  <div className="text-xs text-[#5B738B] font-data">PO {c.po_number} · Item {c.item_number}</div>
                </div>
                <Input
                  type="number"
                  value={c.ship_qty}
                  onChange={(e) => updateCartQty(c.key, e.target.value)}
                  className="h-8 w-24 text-right rounded-sm border-[#CBD3DB] text-xs"
                  data-testid={`supplier-review-qty-input-${c.key}`}
                />
                <span className="text-xs text-[#5B738B] w-10">{c.unit_of_measure}</span>
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
              className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150"
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
            <DialogTitle className="font-sans">Generate Shipment Code?</DialogTitle>
            <DialogDescription>
              This will create one shipment covering {cartItems.length} item{cartItems.length > 1 ? "s" : ""} across {new Set(cartItems.map((c) => c.po_number)).size} PO(s). You can still edit its contents until it's received - are you sure?
            </DialogDescription>
          </DialogHeader>
          {submitError && <div className="text-sm text-[#B91C1C]" data-testid="supplier-submit-error">{submitError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button onClick={submitShipment} disabled={submitting} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-confirm-generate-button">
              {submitting ? "Generating..." : "Yes, Generate Code"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!successCode} onOpenChange={(o) => !o && setSuccessCode(null)}>
        <DialogContent className="rounded-sm" data-testid="supplier-ship-success-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Shipment Created</DialogTitle>
            <DialogDescription>Write this code on your shipment paperwork - it's how the warehouse matches it on arrival. You can still edit its contents from the Shipments page until it's received.</DialogDescription>
          </DialogHeader>
          <div className="text-center py-4 bg-[#F5F6F7] border border-[#CBD3DB] rounded-sm">
            <div className="text-3xl font-data font-bold tracking-widest text-[#0076CC]" data-testid="supplier-ship-success-code">{successCode}</div>
          </div>
          <Button onClick={() => setSuccessCode(null)} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-ship-success-close-button">Done</Button>
        </DialogContent>
      </Dialog>

      <Dialog open={!!detailPoNumber} onOpenChange={(o) => !o && setDetailPoNumber(null)}>
        <DialogContent className="rounded-sm max-w-2xl" data-testid="supplier-po-detail-modal">
          <DialogHeader>
            <DialogTitle className="font-sans font-data">PO {detailPoNumber}</DialogTitle>
            <DialogDescription>
              {detailItems[0]?.buyer_entity_name} · Ordered {detailItems[0]?.po_date || "-"}
            </DialogDescription>
          </DialogHeader>
          <table className="w-full text-sm border-collapse">
            <thead className="text-[#5B738B] text-xs uppercase">
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
                <tr key={i} className="border-t border-[#CBD3DB]">
                  <td className="py-1.5 font-data">{it.item_number}</td>
                  <td className="py-1.5">{it.description}</td>
                  <td className="py-1.5 text-right font-data">{it.po_qty} {it.unit_of_measure}</td>
                  <td className="py-1.5 text-right font-data">{fmtMoney(it.unit_price, it.currency)}</td>
                  <td className="py-1.5 text-right font-data">{fmtMoney(it.subtotal, it.currency)}</td>
                  <td className="py-1.5 font-data">{it.due_date || "-"}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t-2 border-[#CBD3DB]">
                <td colSpan={4} className="py-2 text-right font-semibold text-xs text-[#5B738B]">PO Total</td>
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
