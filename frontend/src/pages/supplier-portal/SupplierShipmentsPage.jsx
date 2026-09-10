import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { CheckCircle, XCircle, Clock, PencilSimple, Plus, X, PlugsConnected, MagnifyingGlass } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";
import { SupplierPortalLayout } from "./SupplierPortalLayout";

const SHIPMENT_STATUS_BADGE = {
  in_transit: { label: "In Transit", className: "bg-[#EFF6FF] text-[#1E40AF] border-[#BFDBFE]", icon: Clock },
  discrepancy: { label: "Discrepancy", className: "bg-[#FFFBEB] text-[#92400E] border-[#FDE68A]", icon: XCircle },
  approved: { label: "Received", className: "bg-[#ECFDF5] text-[#065F46] border-[#A7F3D0]", icon: CheckCircle },
  rejected: { label: "Rejected", className: "bg-[#FEF2F2] text-[#991B1B] border-[#FECACA]", icon: XCircle },
};

function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  return String(detail);
}

export default function SupplierShipmentsPage() {
  const { account } = useSupplierAuth();
  const { vendorCode } = useParams();
  const isImpersonating = !!(account?.testing_mode && vendorCode && vendorCode !== account?.vendor_code);
  // Sep 9 2026: lets the Dashboard's "In Transit Qty" link jump here
  // pre-filtered to a specific PO (e.g. /shipments/H1330?po=28792).
  const [searchParams] = useSearchParams();

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
  const [search, setSearch] = useState(searchParams.get("po") || "");

  const [detailShipment, setDetailShipment] = useState(null);

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
    // Keep a shipment single-entity (Sep 2026, "mrp vendor side changes.docx" -
    // the GRN Approval screen's Site field is now derived from a shipment's
    // buyer entity) - once it has items, only allow adding more from that
    // SAME entity. Legacy shipments with no buyer_code on their items yet
    // are left unrestricted.
    const shipmentEntity = editing.items.find((it) => it.buyer_code)?.buyer_code;
    return pos.filter((po) => po.remaining_qty > 0
      && !already.has(`${po.po_number}::${po.item_number}`)
      && (!shipmentEntity || (po.buyer_code || "RT") === shipmentEntity)
      && (!q || [po.po_number, po.item_number, po.description].some((v) => (v || "").toString().toLowerCase().includes(q))));
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
    <SupplierPortalLayout
      active="shipments"
      vendorCode={vendorCode}
      isImpersonating={isImpersonating}
      pageTitle="In-Transit Shipments & Dispatch Tracking"
      pageSubtitle="You can edit an In Transit shipment anytime before it's received"
    >
      <div data-testid="supplier-shipments-page">
        <p className="text-sm text-[#475569] mt-1">You can edit an "In Transit" shipment's contents anytime before it's received and posted to SAP - once approved, it's locked.</p>

        <div className="mt-4 bg-white border border-[#E2E8F0] rounded-xl p-2.5 flex items-center gap-3 flex-wrap shadow-[0_1px_2px_0_rgba(16,24,40,0.05)]" data-testid="supplier-shipments-filters">
          <div className="relative">
            <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#475569]" />
            <Input
              placeholder="Search doc code or PO number..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8 h-8 w-56 rounded-sm border-[#E2E8F0] text-[13px]"
              data-testid="supplier-shipments-search-input"
            />
          </div>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-40 h-8 rounded-sm border-[#E2E8F0] bg-white text-[13px]" data-testid="supplier-shipments-status-filter">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" data-testid="supplier-shipments-status-filter-all">All Statuses</SelectItem>
              <SelectItem value="in_transit" data-testid="supplier-shipments-status-filter-in_transit">In Transit</SelectItem>
              <SelectItem value="discrepancy" data-testid="supplier-shipments-status-filter-discrepancy">Discrepancy</SelectItem>
              <SelectItem value="approved" data-testid="supplier-shipments-status-filter-approved">Received</SelectItem>
              <SelectItem value="rejected" data-testid="supplier-shipments-status-filter-rejected">Rejected</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-[#475569]">From</span>
            <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="h-8 w-36 rounded-sm border-[#E2E8F0] text-[13px]" data-testid="supplier-shipments-date-from" />
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-[#475569]">To</span>
            <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="h-8 w-36 rounded-sm border-[#E2E8F0] text-[13px]" data-testid="supplier-shipments-date-to" />
          </div>
          {filtersActive && (
            <Button variant="outline" size="sm" onClick={clearFilters} className="rounded-sm" data-testid="supplier-shipments-clear-filters">
              <X size={12} className="mr-1" /> Clear
            </Button>
          )}
          <span className="text-xs text-[#475569] ml-auto">{filteredShipments.length} of {shipments.length} shipment{shipments.length !== 1 ? "s" : ""}</span>
        </div>

        {loading && <div className="mt-8 text-sm text-[#475569]">Loading your shipments...</div>}

        {!loading && (
          <div className="mt-4 bg-white border border-[#E2E8F0] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto" data-testid="supplier-shipments-table">
            <table className="w-full text-[13px] border-collapse">
              <thead className="bg-[#F1F5F9] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                <tr>
                  <th className="border border-[#E2E8F0] p-1.5 text-left">Doc Code</th>
                  <th className="border border-[#E2E8F0] p-1.5 text-left">Created</th>
                  <th className="border border-[#E2E8F0] p-1.5 text-left">PO Numbers</th>
                  <th className="border border-[#E2E8F0] p-1.5 text-left">Printed PO #</th>
                  <th className="border border-[#E2E8F0] p-1.5 text-left">Items</th>
                  <th className="border border-[#E2E8F0] p-1.5 text-left">Status</th>
                  <th className="border border-[#E2E8F0] p-1.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {filteredShipments.length === 0 && (
                  <tr><td colSpan={7} className="border border-[#E2E8F0] px-3 py-6 text-center text-[#475569]" data-testid="supplier-shipments-empty">{shipments.length === 0 ? "No shipments yet." : "No shipments match these filters."}</td></tr>
                )}
                {filteredShipments.map((s) => {
                  const badge = SHIPMENT_STATUS_BADGE[s.status] || SHIPMENT_STATUS_BADGE.in_transit;
                  const Icon = badge.icon;
                  return (
                    <tr key={s._id} onClick={() => setDetailShipment(s)} className="cursor-pointer bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" data-testid={`supplier-shipment-row-${s._id}`}>
                      <td className="border border-[#E2E8F0] px-2 py-1 font-data font-bold text-[#1E40AF]">{s._id}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1 font-data text-xs text-[#475569]">{new Date(s.created_at).toLocaleDateString()}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1 font-data text-xs">{[...new Set(s.items.map((it) => it.po_number))].join(", ")}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1 font-data text-xs" data-testid={`supplier-shipment-printed-po-${s._id}`}>{[...new Set(s.items.map((it) => it.sap_po_number).filter(Boolean))].join(", ") || "\u2014"}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1 text-xs text-[#475569] max-w-xs">
                        {s.items.map((it) => `PO ${it.po_number} · ${it.description || it.item_number} × ${it.ship_qty}${it.unit_of_measure ? ` ${it.unit_of_measure}` : ""}`).join("; ")}
                      </td>
                      <td className="border border-[#E2E8F0] px-2 py-1">
                        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-sm text-xs font-semibold border ${badge.className}`}>
                          <Icon size={12} /> {badge.label}
                        </span>
                        {s.status === "rejected" && s.rejection_reason && (
                          <div className="text-xs text-[#991B1B] mt-1">{s.rejection_reason}</div>
                        )}
                        {s.status === "discrepancy" && s.discrepancy_reason && (
                          <div className="text-xs text-[#991B1B] mt-1">{s.discrepancy_reason}</div>
                        )}
                        {s.status === "approved" && (
                          <div className="text-xs mt-1 flex items-center gap-1" style={{ color: s.sap_sync_status === "posted" ? "#065F46" : "#1E40AF" }}>
                            <PlugsConnected size={11} /> {s.sap_sync_status === "posted" ? "Posted to SAP" : "SAP posting pending"}
                          </div>
                        )}
                      </td>
                      <td className="border border-[#E2E8F0] px-2 py-1 text-right">
                        {(s.status === "in_transit" || s.status === "discrepancy") && (
                          <Button size="sm" variant="outline" onClick={(e) => { e.stopPropagation(); openEdit(s); }} className="rounded-sm" data-testid={`supplier-shipment-edit-button-${s._id}`}>
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

      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="rounded-sm max-w-lg" data-testid="supplier-shipment-edit-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading font-data">Edit Shipment {editing?.code}</DialogTitle>
            <DialogDescription>Add or remove items, or change quantities. Changes only take effect once you save.</DialogDescription>
          </DialogHeader>
          <div className="max-h-56 overflow-y-auto divide-y divide-[#E2E8F0]">
            {editing?.items.map((it, idx) => (
              <div key={`${it.po_number}-${it.item_number}`} className="flex items-center gap-2 py-2" data-testid={`supplier-edit-row-${it.po_number}-${it.item_number}`}>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium truncate">{it.description || it.item_number}</div>
                  <div className="text-xs text-[#475569] font-data">PO {it.po_number} · Item {it.item_number}</div>
                </div>
                <Input
                  type="number"
                  value={it.ship_qty}
                  onChange={(e) => updateEditQty(idx, e.target.value)}
                  className="h-8 w-24 text-right rounded-sm border-[#E2E8F0] text-xs"
                  data-testid={`supplier-edit-qty-input-${it.po_number}-${it.item_number}`}
                />
                <span className="text-xs text-[#475569] w-10">{it.unit_of_measure}</span>
                <button onClick={() => removeItemFromEdit(idx)} className="text-[#991B1B] hover:bg-[#991B1B]/10 rounded-sm p-1" data-testid={`supplier-edit-remove-${it.po_number}-${it.item_number}`}>
                  <X size={14} />
                </button>
              </div>
            ))}
            {editing?.items.length === 0 && <div className="text-sm text-[#475569] py-4 text-center">No items - add at least one below.</div>}
          </div>

          <div className="mt-2 border-t border-[#E2E8F0] pt-2">
            <Input
              placeholder="Search your open POs to add an item..."
              value={addSearch}
              onChange={(e) => setAddSearch(e.target.value)}
              className="rounded-sm border-[#E2E8F0] text-xs"
              data-testid="supplier-edit-add-search-input"
            />
            {addSearch && (
              <div className="max-h-32 overflow-y-auto mt-1 border border-[#E2E8F0] rounded-sm">
                {addableItems.length === 0 && <div className="text-xs text-[#475569] p-2">No matching open items.</div>}
                {addableItems.map((po) => (
                  <button
                    key={`${po.po_number}-${po.item_number}`}
                    onClick={() => { addItemToEdit(po); setAddSearch(""); }}
                    className="w-full text-left px-2 py-1.5 text-xs hover:bg-[#F0F4F8] flex items-center justify-between gap-2"
                    data-testid={`supplier-edit-addable-${po.po_number}-${po.item_number}`}
                  >
                    <span className="truncate">PO {po.po_number} · {po.description}</span>
                    <Plus size={12} className="text-[#1E40AF] shrink-0" />
                  </button>
                ))}
              </div>
            )}
          </div>

          {saveError && <div className="text-sm text-[#991B1B]" data-testid="supplier-edit-save-error">{saveError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setEditing(null)}>Cancel</Button>
            <Button
              onClick={() => setConfirmSaveOpen(true)}
              disabled={!editing?.items.length || editing.items.some((it) => !(parseFloat(it.ship_qty) > 0))}
              className="rounded-sm bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150"
              data-testid="supplier-edit-save-button"
            >
              Save Changes
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmSaveOpen} onOpenChange={setConfirmSaveOpen}>
        <DialogContent className="rounded-sm" data-testid="supplier-edit-confirm-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Save these changes?</DialogTitle>
            <DialogDescription>This updates shipment {editing?.code}'s contents. Are you sure?</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setConfirmSaveOpen(false)}>Cancel</Button>
            <Button onClick={saveEdit} disabled={saving} className="rounded-sm bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150" data-testid="supplier-edit-confirm-save-button">
              {saving ? "Saving..." : "Yes, Save Changes"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!detailShipment} onOpenChange={(o) => !o && setDetailShipment(null)}>
        <DialogContent className="rounded-sm max-w-2xl" data-testid="supplier-shipment-detail-modal">
          <DialogHeader>
            <DialogTitle className="font-heading font-data flex items-center gap-2">
              {detailShipment?._id}
              {detailShipment && (() => {
                const badge = SHIPMENT_STATUS_BADGE[detailShipment.status] || SHIPMENT_STATUS_BADGE.in_transit;
                const Icon = badge.icon;
                return (
                  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-sm text-xs font-semibold ${badge.className}`}>
                    <Icon size={12} /> {badge.label}
                  </span>
                );
              })()}
            </DialogTitle>
            <DialogDescription>
              Created {detailShipment && new Date(detailShipment.created_at).toLocaleString()}
              {detailShipment?.status === "approved" && detailShipment?.approved_at && ` · Received ${new Date(detailShipment.approved_at).toLocaleString()}${detailShipment.approved_by ? ` by ${detailShipment.approved_by}` : ""}`}
              {detailShipment?.status === "rejected" && detailShipment?.rejected_at && ` · Rejected ${new Date(detailShipment.rejected_at).toLocaleString()}${detailShipment.rejected_by ? ` by ${detailShipment.rejected_by}` : ""}`}
            </DialogDescription>
          </DialogHeader>

          {detailShipment?.status === "rejected" && detailShipment?.rejection_reason && (
            <div className="text-sm text-[#991B1B] bg-[#991B1B]/10 border border-[#991B1B]/30 rounded-sm px-3 py-2" data-testid="supplier-shipment-detail-rejection-reason">
              Reason: {detailShipment.rejection_reason}
            </div>
          )}

          {detailShipment?.status === "discrepancy" && detailShipment?.discrepancy_reason && (
            <div className="text-sm text-[#991B1B] bg-[#991B1B]/10 border border-[#991B1B]/30 rounded-sm px-3 py-2" data-testid="supplier-shipment-detail-discrepancy-reason">
              Discrepancy: {detailShipment.discrepancy_reason}
              <div className="text-xs mt-1">
                Flagged items: {(detailShipment.discrepancy_items || []).map((it) => `${it.po_number}/${it.item_number}`).join(", ")}
              </div>
              <div className="text-xs mt-1 text-[#475569]">Please edit this shipment to fix the mismatch - it will automatically go back to "In Transit" for re-review.</div>
            </div>
          )}

          <table className="w-full text-sm border-collapse mt-2">
            <thead className="text-[#475569] text-xs uppercase">
              <tr>
                <th className="text-left py-1 font-semibold">PO Number</th>
                <th className="text-left py-1 font-semibold">Item</th>
                <th className="text-left py-1 font-semibold">Description</th>
                <th className="text-right py-1 font-semibold">Ship Qty</th>
                <th className="text-right py-1 font-semibold">PO Qty</th>
              </tr>
            </thead>
            <tbody>
              {detailShipment?.items.map((it, i) => (
                <tr key={i} className="border-t border-[#E2E8F0]" data-testid={`supplier-shipment-detail-item-${it.po_number}-${it.item_number}`}>
                  <td className="py-1.5 font-data">{it.po_number}</td>
                  <td className="py-1.5 font-data">{it.item_number}</td>
                  <td className="py-1.5">{it.description}</td>
                  <td className="py-1.5 text-right font-data font-semibold">{it.ship_qty} {it.unit_of_measure}</td>
                  <td className="py-1.5 text-right font-data text-[#475569]">{it.po_qty}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {detailShipment?.status === "approved" && (
            <div className="border-t border-[#E2E8F0] pt-3 mt-1" data-testid="supplier-shipment-detail-sap-status">
              <div className="text-xs font-semibold text-[#475569] uppercase mb-1.5">SAP Posting Status</div>
              <div className="flex items-center gap-1.5 text-sm" style={{ color: detailShipment.sap_sync_status === "posted" ? "#065F46" : "#1E40AF" }}>
                <PlugsConnected size={14} />
                {detailShipment.sap_sync_status === "posted" ? "Posted to SAP" : "SAP posting pending"}
              </div>
              {detailShipment.sap_gr_result?.per_po && (
                <div className="mt-2 space-y-1">
                  {detailShipment.sap_gr_result.per_po.map((r, i) => (
                    <div key={i} className="text-xs flex items-center gap-2" data-testid={`supplier-shipment-detail-sap-po-result-${r.po_number}`}>
                      <span className="font-data font-semibold">PO {r.po_number}</span>
                      <span className={r.ok !== false ? "text-[#065F46]" : "text-[#991B1B]"}>{r.ok !== false ? "Received by SAP" : (r.reason || "SAP rejected this posting")}</span>
                    </div>
                  ))}
                </div>
              )}
              {detailShipment.sap_gr_result?.reason && !detailShipment.sap_gr_result?.per_po && (
                <div className="text-xs text-[#991B1B] mt-1">{detailShipment.sap_gr_result.reason}</div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
      </div>
    </SupplierPortalLayout>
  );
}
