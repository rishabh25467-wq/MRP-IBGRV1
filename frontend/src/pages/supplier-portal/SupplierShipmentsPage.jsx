import { useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Buildings, SignOut, CheckCircle, XCircle, Clock, ArrowLeft, PencilSimple, Plus, X, PlugsConnected, MagnifyingGlass } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
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

export default function SupplierShipmentsPage() {
  const { account, logout } = useSupplierAuth();
  const { vendorCode } = useParams();
  const isImpersonating = !!(account?.testing_mode && vendorCode && vendorCode !== account?.vendor_code);

  const [shipments, setShipments] = useState([]);
  const [pos, setPos] = useState([]);
  const [loading, setLoading] = useState(true);

  const [editing, setEditing] = useState(null); // shipment being edited (draft copy)
  const [addSearch, setAddSearch] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmSaveOpen, setConfirmSaveOpen] = useState(false);

  const [statusFilter, setStatusFilter] = useState("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [search, setSearch] = useState("");

  const load = async () => {
    try {
      const params = isImpersonating ? { as_vendor: vendorCode } : {};
      const [shipRes, poRes] = await Promise.all([
        supplierApi.get("/shipments", { params }),
        supplierApi.get("/purchase-orders", { params }),
      ]);
      setShipments(shipRes.data.shipments || []);
      setPos(poRes.data.purchase_orders || []);
    } catch {
      // shipments list failing is non-fatal here - table just shows empty
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorCode]);

  const openEdit = (s) => {
    setEditing({ code: s._id, items: s.items.map((it) => ({ ...it })) });
    setAddSearch("");
    setSaveError("");
  };

  const filteredShipments = useMemo(() => {
    const from = dateFrom ? new Date(`${dateFrom}T00:00:00`) : null;
    const to = dateTo ? new Date(`${dateTo}T23:59:59`) : null;
    const q = search.trim().toLowerCase();
    return shipments.filter((s) => {
      if (statusFilter !== "all" && s.status !== statusFilter) return false;
      const created = new Date(s.created_at);
      if (from && created < from) return false;
      if (to && created > to) return false;
      if (q && !s._id.toLowerCase().includes(q) && !s.items.some((it) => (it.po_number || "").toLowerCase().includes(q))) return false;
      return true;
    });
  }, [shipments, statusFilter, dateFrom, dateTo, search]);

  const clearFilters = () => { setStatusFilter("all"); setDateFrom(""); setDateTo(""); setSearch(""); };
  const filtersActive = statusFilter !== "all" || !!dateFrom || !!dateTo || !!search;

  const addableItems = useMemo(() => {
    if (!editing) return [];
    const already = new Set(editing.items.map((it) => `${it.po_number}::${it.item_number}`));
    const q = addSearch.trim().toLowerCase();
    return pos.filter((po) => po.remaining_qty > 0 && !already.has(`${po.po_number}::${po.item_number}`) && (!q || [po.po_number, po.item_number, po.description].some((v) => (v || "").toString().toLowerCase().includes(q))));
  }, [editing, addSearch, pos]);

  const addItemToEdit = (po) => {
    setEditing((prev) => ({ ...prev, items: [...prev.items, { po_number: po.po_number, item_number: po.item_number, description: po.description, unit_of_measure: po.unit_of_measure, ship_qty: "" }] }));
  };

  const removeItemFromEdit = (idx) => {
    setEditing((prev) => ({ ...prev, items: prev.items.filter((_, i) => i !== idx) }));
  };

  const updateEditQty = (idx, value) => {
    setEditing((prev) => ({ ...prev, items: prev.items.map((it, i) => (i === idx ? { ...it, ship_qty: value } : it)) }));
  };

  const saveEdit = async () => {
    setSaving(true);
    setSaveError("");
    try {
      const items = editing.items.map((it) => ({ po_number: it.po_number, item_number: it.item_number, ship_qty: parseFloat(it.ship_qty) }));
      await supplierApi.put(`/shipments/${editing.code}`, { items });
      setConfirmSaveOpen(false);
      setEditing(null);
      load();
    } catch (e) {
      setSaveError(formatApiErrorDetail(e.response?.data?.detail));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="supplier-shipments-page">
      <div className="bg-[#0076CC] text-white px-6 py-3 flex items-center justify-between shadow-sm gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <Buildings size={20} weight="fill" className="text-white" />
          <span className="font-sans font-bold tracking-tight">Supplier Portal</span>
        </div>
        <div className="flex items-center gap-4">
          <Link to={`/supplier-portal/dashboard/${vendorCode}`} data-testid="supplier-nav-dashboard-link">
            <Button variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150">
              <ArrowLeft size={14} className="mr-1" /> Purchase Orders
            </Button>
          </Link>
          <div className="text-right hidden sm:block">
            <div className="text-sm font-semibold">{isImpersonating ? `Viewing: ${vendorCode}` : account?.company_name}</div>
            <div className="text-xs text-white/75 font-data">Vendor Code: {vendorCode}</div>
          </div>
          <Button onClick={logout} variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150" data-testid="supplier-dashboard-logout-button">
            <SignOut size={14} className="mr-1" /> Sign Out
          </Button>
        </div>
      </div>

      <div className="max-w-5xl mx-auto p-4 md:p-6">
        <h1 className="font-sans text-lg font-bold text-[#111827]">Your Shipments</h1>
        <p className="text-sm text-[#5B738B] mt-1">You can edit an "In Transit" shipment's contents anytime before it's received and posted to SAP - once approved, it's locked.</p>

        <div className="mt-4 flex items-center gap-2 flex-wrap" data-testid="supplier-shipments-filters">
          <div className="relative">
            <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#5B738B]" />
            <Input
              placeholder="Search doc code or PO number..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8 h-9 w-56 rounded-sm border-[#CBD3DB] text-sm"
              data-testid="supplier-shipments-search-input"
            />
          </div>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-40 h-9 rounded-sm border-[#CBD3DB] bg-white text-sm" data-testid="supplier-shipments-status-filter">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" data-testid="supplier-shipments-status-filter-all">All Statuses</SelectItem>
              <SelectItem value="in_transit" data-testid="supplier-shipments-status-filter-in_transit">In Transit</SelectItem>
              <SelectItem value="approved" data-testid="supplier-shipments-status-filter-approved">Received</SelectItem>
              <SelectItem value="rejected" data-testid="supplier-shipments-status-filter-rejected">Rejected</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-[#5B738B]">From</span>
            <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="h-9 w-36 rounded-sm border-[#CBD3DB] text-sm" data-testid="supplier-shipments-date-from" />
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-[#5B738B]">To</span>
            <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="h-9 w-36 rounded-sm border-[#CBD3DB] text-sm" data-testid="supplier-shipments-date-to" />
          </div>
          {filtersActive && (
            <Button variant="outline" size="sm" onClick={clearFilters} className="rounded-sm" data-testid="supplier-shipments-clear-filters">
              <X size={12} className="mr-1" /> Clear
            </Button>
          )}
          <span className="text-xs text-[#5B738B] ml-auto">{filteredShipments.length} of {shipments.length} shipment{shipments.length !== 1 ? "s" : ""}</span>
        </div>

        {loading && <div className="mt-8 text-sm text-[#5B738B]">Loading your shipments...</div>}

        {!loading && (
          <div className="mt-4 bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-hidden" data-testid="supplier-shipments-table">
            <table className="w-full text-sm border-collapse">
              <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
                <tr>
                  <th className="text-left px-3 py-2 font-semibold">Doc Code</th>
                  <th className="text-left px-3 py-2 font-semibold">Created</th>
                  <th className="text-left px-3 py-2 font-semibold">PO Numbers</th>
                  <th className="text-left px-3 py-2 font-semibold">Items</th>
                  <th className="text-left px-3 py-2 font-semibold">Status</th>
                  <th className="text-right px-3 py-2 font-semibold">Action</th>
                </tr>
              </thead>
              <tbody>
                {filteredShipments.length === 0 && (
                  <tr><td colSpan={6} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-shipments-empty">{shipments.length === 0 ? "No shipments yet." : "No shipments match these filters."}</td></tr>
                )}
                {filteredShipments.map((s) => {
                  const badge = SHIPMENT_STATUS_BADGE[s.status] || SHIPMENT_STATUS_BADGE.in_transit;
                  const Icon = badge.icon;
                  return (
                    <tr key={s._id} className="border-b border-[#CBD3DB]" data-testid={`supplier-shipment-row-${s._id}`}>
                      <td className="px-3 py-2 font-data font-bold text-[#111827]">{s._id}</td>
                      <td className="px-3 py-2 font-data text-xs text-[#5B738B]">{new Date(s.created_at).toLocaleDateString()}</td>
                      <td className="px-3 py-2 font-data text-xs">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                      <td className="px-3 py-2 text-xs text-[#5B738B] max-w-xs">
                        {s.items.map((it) => `PO ${it.po_number} · ${it.description || it.item_number} × ${it.ship_qty}${it.unit_of_measure ? ` ${it.unit_of_measure}` : ""}`).join("; ")}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-sm text-xs font-semibold ${badge.className}`}>
                          <Icon size={12} /> {badge.label}
                        </span>
                        {s.status === "rejected" && s.rejection_reason && (
                          <div className="text-xs text-[#B91C1C] mt-1">{s.rejection_reason}</div>
                        )}
                        {s.status === "approved" && (
                          <div className="text-xs mt-1 flex items-center gap-1" style={{ color: s.sap_sync_status === "posted" ? "#0B7A56" : "#1D4ED8" }}>
                            <PlugsConnected size={11} /> {s.sap_sync_status === "posted" ? "Posted to SAP" : "SAP posting pending"}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {s.status === "in_transit" && (
                          <Button size="sm" variant="outline" onClick={() => openEdit(s)} className="rounded-sm" data-testid={`supplier-shipment-edit-button-${s._id}`}>
                            <PencilSimple size={12} className="mr-1" /> Edit
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="rounded-sm max-w-lg" data-testid="supplier-shipment-edit-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans font-data">Edit Shipment {editing?.code}</DialogTitle>
            <DialogDescription>Add or remove items, or change quantities. Changes only take effect once you save.</DialogDescription>
          </DialogHeader>
          <div className="max-h-56 overflow-y-auto divide-y divide-[#CBD3DB]">
            {editing?.items.map((it, idx) => (
              <div key={`${it.po_number}-${it.item_number}`} className="flex items-center gap-2 py-2" data-testid={`supplier-edit-row-${it.po_number}-${it.item_number}`}>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium truncate">{it.description || it.item_number}</div>
                  <div className="text-xs text-[#5B738B] font-data">PO {it.po_number} · Item {it.item_number}</div>
                </div>
                <Input
                  type="number"
                  value={it.ship_qty}
                  onChange={(e) => updateEditQty(idx, e.target.value)}
                  className="h-8 w-24 text-right rounded-sm border-[#CBD3DB] text-xs"
                  data-testid={`supplier-edit-qty-input-${it.po_number}-${it.item_number}`}
                />
                <span className="text-xs text-[#5B738B] w-10">{it.unit_of_measure}</span>
                <button onClick={() => removeItemFromEdit(idx)} className="text-[#B91C1C] hover:bg-[#E02424]/10 rounded-sm p-1" data-testid={`supplier-edit-remove-${it.po_number}-${it.item_number}`}>
                  <X size={14} />
                </button>
              </div>
            ))}
            {editing?.items.length === 0 && <div className="text-sm text-[#5B738B] py-4 text-center">No items - add at least one below.</div>}
          </div>

          <div className="mt-2 border-t border-[#CBD3DB] pt-2">
            <Input
              placeholder="Search your open POs to add an item..."
              value={addSearch}
              onChange={(e) => setAddSearch(e.target.value)}
              className="rounded-sm border-[#CBD3DB] text-xs"
              data-testid="supplier-edit-add-search-input"
            />
            {addSearch && (
              <div className="max-h-32 overflow-y-auto mt-1 border border-[#CBD3DB] rounded-sm">
                {addableItems.length === 0 && <div className="text-xs text-[#5B738B] p-2">No matching open items.</div>}
                {addableItems.map((po) => (
                  <button
                    key={`${po.po_number}-${po.item_number}`}
                    onClick={() => { addItemToEdit(po); setAddSearch(""); }}
                    className="w-full text-left px-2 py-1.5 text-xs hover:bg-[#F5F6F7] flex items-center justify-between gap-2"
                    data-testid={`supplier-edit-addable-${po.po_number}-${po.item_number}`}
                  >
                    <span className="truncate">PO {po.po_number} · {po.description}</span>
                    <Plus size={12} className="text-[#0076CC] shrink-0" />
                  </button>
                ))}
              </div>
            )}
          </div>

          {saveError && <div className="text-sm text-[#B91C1C]" data-testid="supplier-edit-save-error">{saveError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setEditing(null)}>Cancel</Button>
            <Button onClick={() => setConfirmSaveOpen(true)} disabled={!editing?.items.length} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-edit-save-button">
              Save Changes
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmSaveOpen} onOpenChange={setConfirmSaveOpen}>
        <DialogContent className="rounded-sm" data-testid="supplier-edit-confirm-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Save these changes?</DialogTitle>
            <DialogDescription>This updates shipment {editing?.code}'s contents. Are you sure?</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setConfirmSaveOpen(false)}>Cancel</Button>
            <Button onClick={saveEdit} disabled={saving} className="rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-edit-confirm-save-button">
              {saving ? "Saving..." : "Yes, Save Changes"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
