import { useEffect, useState } from "react";
import { Buildings, SignOut, Package, PlugsConnected } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

export default function SupplierDashboardPage() {
  const { account, logout } = useSupplierAuth();
  const [pos, setPos] = useState([]);
  const [notConfigured, setNotConfigured] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await supplierApi.get("/purchase-orders");
        setPos(data.purchase_orders || []);
      } catch (e) {
        if (e.response?.status === 503) setNotConfigured(true);
        else setError(e.response?.data?.detail || "Could not load your Purchase Orders.");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return (
    <div className="min-h-screen bg-[#F8FAFC]" data-testid="supplier-dashboard-page">
      <div className="bg-[#0F172A] text-white px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Buildings size={22} weight="fill" className="text-[#F59E0B]" />
          <span className="font-heading font-bold tracking-tight">Supplier Portal</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right hidden sm:block">
            <div className="text-sm font-semibold" data-testid="supplier-dashboard-company-name">{account?.company_name}</div>
            <div className="text-xs text-white/60">Vendor Code: {account?.vendor_code}</div>
          </div>
          <Button onClick={logout} variant="outline" size="sm" className="border-white/30 text-white hover:bg-white/10 hover:text-white" data-testid="supplier-dashboard-logout-button">
            <SignOut size={14} className="mr-1" /> Sign Out
          </Button>
        </div>
      </div>

      <div className="max-w-5xl mx-auto p-6">
        <h1 className="font-heading text-xl font-bold text-[#0F172A]">Your Open Purchase Orders</h1>
        <p className="text-sm text-[#667085] mt-1">Vendor Code {account?.vendor_code} · {account?.email}</p>

        {loading && <div className="mt-8 text-sm text-[#667085]" data-testid="supplier-dashboard-loading">Loading your Purchase Orders...</div>}

        {!loading && notConfigured && (
          <div className="mt-8 bg-white border border-[#EAECF0] rounded-xl p-8 text-center" data-testid="supplier-dashboard-not-configured">
            <PlugsConnected size={36} weight="fill" className="text-[#F59E0B] mx-auto mb-3" />
            <h2 className="font-heading font-bold text-[#0F172A]">SAP Connection Coming Soon</h2>
            <p className="text-sm text-[#667085] mt-1 max-w-md mx-auto">
              Your account is approved. We're still connecting this portal to SAP - your Purchase Orders will appear here automatically once that's live.
            </p>
          </div>
        )}

        {!loading && error && (
          <div className="mt-8 text-sm text-[#B42318] bg-[#FEF3F2] border border-[#FDA29B] rounded-lg px-4 py-3" data-testid="supplier-dashboard-error">{error}</div>
        )}

        {!loading && !notConfigured && !error && pos.length === 0 && (
          <div className="mt-8 bg-white border border-[#EAECF0] rounded-xl p-8 text-center" data-testid="supplier-dashboard-empty">
            <Package size={36} weight="fill" className="text-[#98A2B3] mx-auto mb-3" />
            <p className="text-sm text-[#667085]">No open Purchase Orders right now.</p>
          </div>
        )}

        {!loading && pos.length > 0 && (
          <div className="mt-6 bg-white border border-[#EAECF0] rounded-xl overflow-hidden" data-testid="supplier-dashboard-po-table">
            <table className="w-full text-sm">
              <thead className="bg-[#F9FAFB] text-[#667085] text-xs uppercase">
                <tr>
                  <th className="text-left px-4 py-2">PO Number</th>
                  <th className="text-left px-4 py-2">Item</th>
                  <th className="text-right px-4 py-2">Open Qty</th>
                  <th className="text-left px-4 py-2">Due Date</th>
                </tr>
              </thead>
              <tbody>
                {pos.map((po, i) => (
                  <tr key={i} className="border-t border-[#EAECF0]">
                    <td className="px-4 py-2">{po.po_number}</td>
                    <td className="px-4 py-2">{po.item_description || po.product_id}</td>
                    <td className="px-4 py-2 text-right">{po.open_qty}</td>
                    <td className="px-4 py-2">{po.due_date}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
