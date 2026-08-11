import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import {
  Database,
  Shield,
  Truck,
  Plus,
  PencilSimple,
  Trash,
  MagnifyingGlass,
  CloudArrowDown,
  WarningCircle,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const inputCls =
  "h-8 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87] w-full";
const labelCls = "font-heading text-xs font-bold text-[#475467] uppercase tracking-wide";

const emptySupplierForm = { name: "", contact_person: "", email: "", phone: "" };

// Vendor code (SAP internal supplier ID, e.g. "S2560") shown alongside the
// name so buyers can cross-reference against SAP screens directly.
const withVendorCode = (name, code) => {
  if (!name) return code || "—";
  return code ? `${name} (${code})` : name;
};

export default function SupplierMasterPage() {
  const [suppliers, setSuppliers] = useState([]);
  const [suppliersLoading, setSuppliersLoading] = useState(true);
  const [supplierSearch, setSupplierSearch] = useState("");
  const [supplierPage, setSupplierPage] = useState(1);
  const SUPPLIER_PAGE_SIZE = 50;
  const [syncing, setSyncing] = useState(false);
  const [syncBanner, setSyncBanner] = useState(null);

  const [supplierDialogOpen, setSupplierDialogOpen] = useState(false);
  const [editingSupplier, setEditingSupplier] = useState(null);
  const [supplierForm, setSupplierForm] = useState(emptySupplierForm);
  const [savingSupplier, setSavingSupplier] = useState(false);

  const loadSuppliers = async () => {
    setSuppliersLoading(true);
    try {
      const { data } = await axios.get(`${API}/suppliers`);
      setSuppliers(data);
    } catch (err) {
      toast.error("Could not load suppliers", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSuppliersLoading(false);
    }
  };

  useEffect(() => {
    loadSuppliers();
  }, []);

  const syncFromSap = async () => {
    setSyncing(true);
    setSyncBanner(null);
    try {
      const { data } = await axios.post(`${API}/suppliers/sync-from-sap`);
      const jobId = data.job_id;
      const startedAt = Date.now();
      // Pulling ~3000 suppliers from SAP takes ~60-90s - poll instead of
      // holding one HTTP request open past the platform's ingress timeout.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const { data: job } = await axios.get(`${API}/suppliers/sync-from-sap/status/${jobId}`);
        if (job.status === "done") {
          toast.success("Synced from SAP", { description: `${job.result.created} new, ${job.result.updated} updated` });
          loadSuppliers();
          break;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "Sync failed");
        }
        if (Date.now() - startedAt > 5 * 60 * 1000) {
          throw new Error("SAP sync is taking too long. Please try again.");
        }
      }
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Sync failed";
      setSyncBanner(detail);
      toast.error("SAP sync failed", { description: detail });
    } finally {
      setSyncing(false);
    }
  };

  const openAddSupplier = () => {
    setEditingSupplier(null);
    setSupplierForm(emptySupplierForm);
    setSupplierDialogOpen(true);
  };

  const openEditSupplier = (s) => {
    setEditingSupplier(s);
    setSupplierForm({
      name: s.name || "",
      contact_person: s.contact_person || "",
      email: s.email || "",
      phone: s.phone || "",
    });
    setSupplierDialogOpen(true);
  };

  const saveSupplier = async () => {
    if (!supplierForm.name.trim()) {
      toast.error("Supplier name is required");
      return;
    }
    setSavingSupplier(true);
    try {
      if (editingSupplier) {
        await axios.patch(`${API}/suppliers/${editingSupplier.id}`, supplierForm);
        toast.success("Supplier updated");
      } else {
        await axios.post(`${API}/suppliers`, supplierForm);
        toast.success("Supplier added");
      }
      setSupplierDialogOpen(false);
      loadSuppliers();
    } catch (err) {
      toast.error("Failed to save supplier", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSavingSupplier(false);
    }
  };

  const deleteSupplier = async (s) => {
    if (!window.confirm(`Delete supplier "${s.name}"? This also removes all its part assignments.`)) return;
    try {
      await axios.delete(`${API}/suppliers/${s.id}`);
      toast.success("Supplier deleted");
      loadSuppliers();
    } catch (err) {
      toast.error("Failed to delete supplier", { description: err?.response?.data?.detail || err.message });
    }
  };

  const filteredSuppliers = suppliers.filter((s) => {
    const q = supplierSearch.trim().toLowerCase();
    if (!q) return true;
    return (
      s.name.toLowerCase().includes(q) ||
      (s.contact_person || "").toLowerCase().includes(q) ||
      (s.email || "").toLowerCase().includes(q) ||
      (s.sap_internal_id || "").toLowerCase().includes(q)
    );
  });
  const supplierTotalPages = Math.max(1, Math.ceil(filteredSuppliers.length / SUPPLIER_PAGE_SIZE));
  const pagedSuppliers = filteredSuppliers.slice(
    (supplierPage - 1) * SUPPLIER_PAGE_SIZE,
    supplierPage * SUPPLIER_PAGE_SIZE
  );

  useEffect(() => {
    setSupplierPage(1);
  }, [supplierSearch]);

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />

      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-5 shrink-0 z-10 gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Supplier Master</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <div className="shrink-0 w-8" />
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4">
        {syncBanner && (
          <div
            className="bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-3 flex items-start gap-2.5"
            data-testid="supplier-sync-banner"
          >
            <WarningCircle size={16} weight="fill" className="text-[#B54708] mt-0.5 shrink-0" />
            <div className="text-[13px] text-[#7A4504]">
              <span className="font-bold">SAP sync unavailable: </span>
              {syncBanner}
            </div>
          </div>
        )}

        {/* Supplier Master List */}
        <section className="bg-white border border-[#D0D5DD] rounded-sm" data-testid="supplier-master-section">
          <div className="p-2.5 border-b border-[#D0D5DD] flex items-center gap-2 flex-wrap">
            <Truck size={16} weight="bold" className="text-[#004B87]" />
            <h2 className="font-heading text-sm font-bold text-[#1D2939]">Supplier Master List</h2>
            <div className="relative flex-1 min-w-[180px] max-w-xs ml-2">
              <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
              <input
                type="text"
                placeholder="Search suppliers..."
                value={supplierSearch}
                onChange={(e) => setSupplierSearch(e.target.value)}
                className={`${inputCls} pl-7`}
                data-testid="supplier-search-input"
              />
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={syncFromSap}
              disabled={syncing}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054] ml-auto"
              data-testid="sync-suppliers-from-sap-button"
            >
              <CloudArrowDown size={13} className={`mr-1.5 ${syncing ? "animate-pulse" : ""}`} />
              {syncing ? "Syncing..." : "Sync from SAP"}
            </Button>
            <Button
              type="button"
              onClick={openAddSupplier}
              className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm"
              data-testid="add-supplier-button"
            >
              <Plus size={13} className="mr-1.5" />
              Add Supplier
            </Button>
          </div>

          <div className="overflow-x-auto max-h-[calc(100vh-220px)] overflow-y-auto">
            <table className="w-full text-[13px] border-collapse" data-testid="supplier-master-table">
              <thead>
                <tr className="sticky top-0">
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Name</th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Contact Person</th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Email</th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Phone</th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Source</th>
                  <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-center text-xs font-bold text-[#344054] font-heading uppercase w-20">Actions</th>
                </tr>
              </thead>
              <tbody>
                {suppliersLoading ? (
                  <tr>
                    <td colSpan={6} className="border border-[#D0D5DD] text-center py-6 text-[13px] text-[#475467]">Loading...</td>
                  </tr>
                ) : filteredSuppliers.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="border border-[#D0D5DD] text-center py-6 text-[13px] text-[#475467]" data-testid="supplier-master-empty">
                      {suppliers.length === 0 ? "No suppliers yet - add one or sync from SAP" : "No suppliers match your search"}
                    </td>
                  </tr>
                ) : (
                  pagedSuppliers.map((s, i) => (
                    <tr key={s.id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`supplier-row-${i}`}>
                      <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">
                        {withVendorCode(s.name, s.sap_internal_id)}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.contact_person || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.email || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{s.phone || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1">
                        <Badge
                          variant="outline"
                          className={s.source === "sap" ? "bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] text-xs" : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs"}
                        >
                          {s.source === "sap" ? "SAP" : "Local"}
                        </Badge>
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1 text-center">
                        <div className="flex items-center justify-center gap-1">
                          <button
                            type="button"
                            onClick={() => openEditSupplier(s)}
                            className="p-1 text-[#475467] hover:text-[#004B87]"
                            data-testid={`edit-supplier-button-${i}`}
                          >
                            <PencilSimple size={14} />
                          </button>
                          <button
                            type="button"
                            onClick={() => deleteSupplier(s)}
                            className="p-1 text-[#475467] hover:text-[#B42318]"
                            data-testid={`delete-supplier-button-${i}`}
                          >
                            <Trash size={14} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          {filteredSuppliers.length > 0 && (
            <div className="flex items-center justify-between px-2.5 py-1.5 border-t border-[#D0D5DD] text-xs text-[#475467]" data-testid="supplier-pagination-bar">
              <span>
                Showing {(supplierPage - 1) * SUPPLIER_PAGE_SIZE + 1}-
                {Math.min(supplierPage * SUPPLIER_PAGE_SIZE, filteredSuppliers.length)} of {filteredSuppliers.length}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setSupplierPage((p) => Math.max(1, p - 1))}
                  disabled={supplierPage <= 1}
                  className="h-7 text-xs rounded-sm border-[#D0D5DD]"
                  data-testid="supplier-page-prev-button"
                >
                  Previous
                </Button>
                <span data-testid="supplier-page-indicator">Page {supplierPage} of {supplierTotalPages}</span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setSupplierPage((p) => Math.min(supplierTotalPages, p + 1))}
                  disabled={supplierPage >= supplierTotalPages}
                  className="h-7 text-xs rounded-sm border-[#D0D5DD]"
                  data-testid="supplier-page-next-button"
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </section>
      </main>

      {/* Supplier Add/Edit Dialog */}
      <Dialog open={supplierDialogOpen} onOpenChange={setSupplierDialogOpen}>
        <DialogContent className="max-w-md" data-testid="supplier-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">{editingSupplier ? "Edit Supplier" : "Add Supplier"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <label className={labelCls}>Name *</label>
              <input
                type="text"
                value={supplierForm.name}
                onChange={(e) => setSupplierForm((f) => ({ ...f, name: e.target.value }))}
                className={`${inputCls} mt-1`}
                data-testid="supplier-form-name-input"
              />
            </div>
            <div>
              <label className={labelCls}>Contact Person</label>
              <input
                type="text"
                value={supplierForm.contact_person}
                onChange={(e) => setSupplierForm((f) => ({ ...f, contact_person: e.target.value }))}
                className={`${inputCls} mt-1`}
                data-testid="supplier-form-contact-input"
              />
            </div>
            <div>
              <label className={labelCls}>Email</label>
              <input
                type="email"
                value={supplierForm.email}
                onChange={(e) => setSupplierForm((f) => ({ ...f, email: e.target.value }))}
                className={`${inputCls} mt-1`}
                data-testid="supplier-form-email-input"
              />
            </div>
            <div>
              <label className={labelCls}>Phone</label>
              <input
                type="text"
                value={supplierForm.phone}
                onChange={(e) => setSupplierForm((f) => ({ ...f, phone: e.target.value }))}
                className={`${inputCls} mt-1`}
                data-testid="supplier-form-phone-input"
              />
            </div>
            <Button
              type="button"
              onClick={saveSupplier}
              disabled={savingSupplier}
              className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm"
              data-testid="supplier-form-save-button"
            >
              {savingSupplier ? "Saving..." : editingSupplier ? "Save Changes" : "Add Supplier"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
