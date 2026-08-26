import { useEffect, useState } from "react";
import { Buildings, SignOut, Package, PlugsConnected, Truck, CheckCircle, XCircle, Clock } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

const SHIPMENT_STATUS_BADGE = {
  in_transit: { label: "In Transit", className: "bg-[#E3A008]/15 text-[#8A6116]", icon: Clock },
  approved: { label: "Received", className: "bg-[#10B981]/15 text-[#0B7A56]", icon: CheckCircle },
  rejected: { label: "Rejected", className: "bg-[#E02424]/10 text-[#B91C1C]", icon: XCircle },
};

function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  return String(detail);
}

export default function SupplierDashboardPage() {
  const { account, logout } = useSupplierAuth();
  const [pos, setPos] = useState([]);
  const [shipments, setShipments] = useState([]);
  const [liveSync, setLiveSync] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const [shipTarget, setShipTarget] = useState(null);
  const [shipQty, setShipQty] = useState("");
  const [shipSubmitting, setShipSubmitting] = useState(false);
  const [shipError, setShipError] = useState("");
  const [successCode, setSuccessCode] = useState(null);

  const loadAll = async () => {
    try {
      const [poRes, shipRes] = await Promise.all([supplierApi.get("/purchase-orders"), supplierApi.get("/shipments")]);
      setPos(poRes.data.purchase_orders || []);
      setLiveSync(!!poRes.data.live_sync);
      setShipments(shipRes.data.shipments || []);
    } catch (e) {
      setError(e.response?.data?.detail || "Could not load your Purchase Orders.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openShipDialog = (item) => {
    setShipTarget(item);
    setShipQty("");
    setShipError("");
  };

  const submitShipment = async () => {
    setShipError("");
    const qty = parseFloat(shipQty);
    if (!qty || qty <= 0) {
      setShipError("Enter a quantity greater than 0");
      return;
    }
    if (qty > shipTarget.remaining_qty + 1e-6) {
      setShipError(`Only ${shipTarget.remaining_qty} still open on this item`);
      return;
    }
    setShipSubmitting(true);
    try {
      const { data } = await supplierApi.post("/shipments", {
        po_number: shipTarget.po_number,
        items: [{ item_number: shipTarget.item_number, ship_qty: qty }],
      });
      setSuccessCode(data._id);
      setShipTarget(null);
      loadAll();
    } catch (e) {
      setShipError(formatApiErrorDetail(e.response?.data?.detail));
    } finally {
      setShipSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="supplier-dashboard-page">
      <div className="bg-[#0076CC] text-white px-6 py-3 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-2">
          <Buildings size={20} weight="fill" className="text-white" />
          <span className="font-sans font-bold tracking-tight">Supplier Portal</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right hidden sm:block">
            <div className="text-sm font-semibold" data-testid="supplier-dashboard-company-name">{account?.company_name}</div>
            <div className="text-xs text-white/75 font-data">Vendor Code: {account?.vendor_code}</div>
          </div>
          <Button onClick={logout} variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150" data-testid="supplier-dashboard-logout-button">
            <SignOut size={14} className="mr-1" /> Sign Out
          </Button>
        </div>
      </div>

      <div className="max-w-5xl mx-auto p-4 md:p-6">
        <h1 className="font-sans text-lg font-bold text-[#111827]">Your Open Purchase Orders</h1>
        <p className="text-sm text-[#5B738B] mt-1 font-data">Vendor Code {account?.vendor_code} · {account?.email}</p>

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

        {!loading && pos.length > 0 && (
          <div className="mt-4 bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-hidden" data-testid="supplier-dashboard-po-table">
            <table className="w-full text-sm border-collapse">
              <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
                <tr>
                  <th className="text-left px-3 py-2 font-semibold">PO Number</th>
                  <th className="text-left px-3 py-2 font-semibold">Item</th>
                  <th className="text-right px-3 py-2 font-semibold">Open Qty</th>
                  <th className="text-left px-3 py-2 font-semibold">Due Date</th>
                  <th className="text-right px-3 py-2 font-semibold">Ship</th>
                </tr>
              </thead>
              <tbody>
                {pos.map((po, i) => (
                  <tr key={i} className="border-b border-[#CBD3DB]" data-testid={`supplier-po-row-${po.po_number}-${po.item_number}`}>
                    <td className="px-3 py-2 font-data">{po.po_number}</td>
                    <td className="px-3 py-2">{po.description || po.product_id}</td>
                    <td className="px-3 py-2 text-right font-data">{po.remaining_qty} {po.unit_of_measure}</td>
                    <td className="px-3 py-2 font-data">{po.due_date || "-"}</td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        size="sm"
                        disabled={po.remaining_qty <= 0}
                        onClick={() => openShipDialog(po)}
                        className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150"
                        data-testid={`supplier-dashboard-ship-button-${po.po_number}-${po.item_number}`}
                      >
                        <Truck size={14} className="mr-1" /> Ship
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <h2 className="font-sans text-base font-bold text-[#111827] mt-8">Your Shipments</h2>
        <div className="mt-3 bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-hidden" data-testid="supplier-shipments-table">
          <table className="w-full text-sm border-collapse">
            <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
              <tr>
                <th className="text-left px-3 py-2 font-semibold">Doc Code</th>
                <th className="text-left px-3 py-2 font-semibold">PO Number</th>
                <th className="text-left px-3 py-2 font-semibold">Items</th>
                <th className="text-left px-3 py-2 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {shipments.length === 0 && (
                <tr><td colSpan={4} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-shipments-empty">No shipments yet.</td></tr>
              )}
              {shipments.map((s) => {
                const badge = SHIPMENT_STATUS_BADGE[s.status] || SHIPMENT_STATUS_BADGE.in_transit;
                const Icon = badge.icon;
                return (
                  <tr key={s._id} className="border-b border-[#CBD3DB]" data-testid={`supplier-shipment-row-${s._id}`}>
                    <td className="px-3 py-2 font-data font-bold text-[#111827]">{s._id}</td>
                    <td className="px-3 py-2 font-data">{s.po_number}</td>
                    <td className="px-3 py-2 text-xs text-[#5B738B]">
                      {s.items.map((it) => `${it.item_number} × ${it.ship_qty}`).join(", ")}
                    </td>
                    <td className="px-3 py-2">
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-sm text-xs font-semibold ${badge.className}`}>
                        <Icon size={12} /> {badge.label}
                      </span>
                      {s.status === "rejected" && s.rejection_reason && (
                        <div className="text-xs text-[#B91C1C] mt-1">{s.rejection_reason}</div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <Dialog open={!!shipTarget} onOpenChange={(o) => !o && setShipTarget(null)}>
        <DialogContent className="rounded-sm" data-testid="supplier-ship-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Create Shipment</DialogTitle>
            <DialogDescription className="font-data">
              PO {shipTarget?.po_number} · Item {shipTarget?.item_number} · {shipTarget?.description}
            </DialogDescription>
          </DialogHeader>
          <p className="text-xs text-[#5B738B]">Open quantity remaining: <span className="font-data">{shipTarget?.remaining_qty} {shipTarget?.unit_of_measure}</span></p>
          <Input
            type="number"
            placeholder="Quantity to ship"
            value={shipQty}
            onChange={(e) => setShipQty(e.target.value)}
            className="rounded-sm border-[#CBD3DB] focus:ring-2 focus:ring-[#4DA3E0] focus:outline-none"
            data-testid="supplier-ship-qty-input"
          />
          {shipError && <div className="text-sm text-[#B91C1C]" data-testid="supplier-ship-error">{shipError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setShipTarget(null)}>Cancel</Button>
            <Button onClick={submitShipment} disabled={shipSubmitting} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-ship-submit-button">
              {shipSubmitting ? "Submitting..." : "Confirm Shipment"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!successCode} onOpenChange={(o) => !o && setSuccessCode(null)}>
        <DialogContent className="rounded-sm" data-testid="supplier-ship-success-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Shipment Created</DialogTitle>
            <DialogDescription>Write this code on your shipment paperwork - it's how the warehouse matches it on arrival.</DialogDescription>
          </DialogHeader>
          <div className="text-center py-4 bg-[#F5F6F7] border border-[#CBD3DB] rounded-sm">
            <div className="text-3xl font-data font-bold tracking-widest text-[#0076CC]" data-testid="supplier-ship-success-code">{successCode}</div>
          </div>
          <Button onClick={() => setSuccessCode(null)} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-ship-success-close-button">Done</Button>
        </DialogContent>
      </Dialog>
    </div>
  );
}
