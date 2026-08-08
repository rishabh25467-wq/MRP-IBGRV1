import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import {
  Database,
  Truck,
  Plus,
  PencilSimple,
  Trash,
  MagnifyingGlass,
  CloudArrowDown,
  WarningCircle,
  Star,
  ShieldCheck,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const inputCls =
  "h-8 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87] w-full";
const labelCls = "font-heading text-xs font-bold text-[#475467] uppercase tracking-wide";

const emptySupplierForm = { name: "", contact_person: "", email: "", phone: "" };
const emptyAssignmentForm = {
  supplier_id: "",
  quota_percent: "",
  lead_time_days: "",
  unit_price: "",
  currency: "INR",
  preference: "Preferred",
  notes: "",
};

export default function SuppliersPage() {
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

  const [productIdInput, setProductIdInput] = useState("");
  const [activeProductId, setActiveProductId] = useState(null);
  const [assignments, setAssignments] = useState([]);
  const [assignmentsLoading, setAssignmentsLoading] = useState(false);
  const [sapPriceSpecs, setSapPriceSpecs] = useState([]);
  const [sapPriceSpecsLoading, setSapPriceSpecsLoading] = useState(false);
  const [sapPriceSpecsError, setSapPriceSpecsError] = useState(null);

  const [assignmentDialogOpen, setAssignmentDialogOpen] = useState(false);
  const [editingAssignment, setEditingAssignment] = useState(null);
  const [assignmentForm, setAssignmentForm] = useState(emptyAssignmentForm);
  const [savingAssignment, setSavingAssignment] = useState(false);

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
      if (activeProductId) loadAssignments(activeProductId);
    } catch (err) {
      toast.error("Failed to delete supplier", { description: err?.response?.data?.detail || err.message });
    }
  };

  const loadAssignments = async (productId) => {
    setAssignmentsLoading(true);
    try {
      const { data } = await axios.get(`${API}/part-suppliers`, { params: { product_id: productId } });
      setAssignments(data);
    } catch (err) {
      toast.error("Could not load supplier assignments", { description: err?.response?.data?.detail || err.message });
    } finally {
      setAssignmentsLoading(false);
    }
  };

  const loadSapPriceSpecs = async (productId) => {
    setSapPriceSpecsLoading(true);
    setSapPriceSpecsError(null);
    try {
      const { data } = await axios.get(`${API}/suppliers/sap-price-specs/${encodeURIComponent(productId)}`);
      setSapPriceSpecs(data);
    } catch (err) {
      setSapPriceSpecs([]);
      setSapPriceSpecsError(err?.response?.data?.detail || err.message || "Could not read SAP purchasing prices");
    } finally {
      setSapPriceSpecsLoading(false);
    }
  };

  const searchProduct = () => {
    const pid = productIdInput.trim();
    if (!pid) return;
    setActiveProductId(pid);
    loadAssignments(pid);
    loadSapPriceSpecs(pid);
  };

  const openAddAssignment = () => {
    if (suppliers.length === 0) {
      toast.error("Add a supplier first", { description: "You need at least one supplier before assigning it to a part." });
      return;
    }
    setEditingAssignment(null);
    setAssignmentForm({ ...emptyAssignmentForm, supplier_id: suppliers[0].id });
    setAssignmentDialogOpen(true);
  };

  const openEditAssignment = (a) => {
    setEditingAssignment(a);
    setAssignmentForm({
      supplier_id: a.supplier_id,
      quota_percent: a.quota_percent ?? "",
      lead_time_days: a.lead_time_days ?? "",
      unit_price: a.unit_price ?? "",
      currency: a.currency || "INR",
      preference: a.preference || "Preferred",
      notes: a.notes || "",
    });
    setAssignmentDialogOpen(true);
  };

  const saveAssignment = async () => {
    if (!assignmentForm.supplier_id) {
      toast.error("Pick a supplier");
      return;
    }
    setSavingAssignment(true);
    const payload = {
      quota_percent: assignmentForm.quota_percent === "" ? null : Number(assignmentForm.quota_percent),
      lead_time_days: assignmentForm.lead_time_days === "" ? null : Number(assignmentForm.lead_time_days),
      unit_price: assignmentForm.unit_price === "" ? null : Number(assignmentForm.unit_price),
      currency: assignmentForm.currency || null,
      preference: assignmentForm.preference,
      notes: assignmentForm.notes || null,
    };
    try {
      if (editingAssignment) {
        await axios.patch(`${API}/part-suppliers/${editingAssignment.id}`, payload);
        toast.success("Assignment updated");
      } else {
        await axios.post(`${API}/part-suppliers`, {
          product_id: activeProductId,
          supplier_id: assignmentForm.supplier_id,
          ...payload,
        });
        toast.success("Supplier assigned to part");
      }
      setAssignmentDialogOpen(false);
      loadAssignments(activeProductId);
    } catch (err) {
      toast.error("Failed to save assignment", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSavingAssignment(false);
    }
  };

  const deleteAssignment = async (a) => {
    if (!window.confirm(`Remove ${a.supplier_name || a.supplier_id} from this part?`)) return;
    try {
      await axios.delete(`${API}/part-suppliers/${a.id}`);
      toast.success("Assignment removed");
      loadAssignments(activeProductId);
    } catch (err) {
      toast.error("Failed to remove assignment", { description: err?.response?.data?.detail || err.message });
    }
  };

  const filteredSuppliers = suppliers.filter((s) => {
    const q = supplierSearch.trim().toLowerCase();
    if (!q) return true;
    return (
      s.name.toLowerCase().includes(q) ||
      (s.contact_person || "").toLowerCase().includes(q) ||
      (s.email || "").toLowerCase().includes(q)
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

      <header className="h-12 bg-[#004B87] shadow-[0_1px_3px_0_rgba(16,24,40,0.1)] flex items-center justify-between px-4 shrink-0 z-10">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2.5" data-testid="app-title">
            <Database size={18} weight="bold" className="text-white" />
            <span className="font-heading text-sm font-bold text-white tracking-tight">SAP BOM Explorer</span>
            <span className="font-sans text-xs text-white/60 hidden sm:inline">| Suppliers</span>
          </div>
          <NavTabs />
        </div>
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

          <div className="overflow-x-auto max-h-72 overflow-y-auto">
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
                      <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{s.name}</td>
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

        {/* Part <-> Supplier Assignments */}
        <section className="bg-white border border-[#D0D5DD] rounded-sm" data-testid="part-supplier-section">
          <div className="p-2.5 border-b border-[#D0D5DD] flex items-center gap-2 flex-wrap">
            <ShieldCheck size={16} weight="bold" className="text-[#004B87]" />
            <h2 className="font-heading text-sm font-bold text-[#1D2939]">Part ↔ Supplier Assignments</h2>
            <div className="flex items-center gap-1.5 ml-2">
              <label htmlFor="part-supplier-product-id-input" className={labelCls}>Product ID</label>
              <input
                id="part-supplier-product-id-input"
                type="text"
                placeholder="e.g. SPC5WM"
                value={productIdInput}
                onChange={(e) => setProductIdInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && searchProduct()}
                className={`${inputCls} w-40`}
                data-testid="part-supplier-product-id-input"
              />
              <Button
                type="button"
                variant="outline"
                onClick={searchProduct}
                disabled={!productIdInput.trim()}
                className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
                data-testid="part-supplier-search-button"
              >
                <MagnifyingGlass size={13} className="mr-1.5" />
                Load
              </Button>
            </div>
            {activeProductId && (
              <Button
                type="button"
                onClick={openAddAssignment}
                className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm ml-auto"
                data-testid="add-part-supplier-button"
              >
                <Plus size={13} className="mr-1.5" />
                Assign Supplier
              </Button>
            )}
          </div>

          {!activeProductId ? (
            <div className="py-10 text-center text-[13px] text-[#98A2B3]" data-testid="part-supplier-empty-state">
              Enter a Product ID above and click "Load" to view/manage its supplier assignments
            </div>
          ) : (
            <div>
              {/* Real SAP purchasing data - read-only */}
              <div className="p-2.5 bg-[#F9FAFB] border-b border-[#D0D5DD]" data-testid="sap-price-specs-panel">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <Badge variant="outline" className="bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] text-xs">SAP</Badge>
                  <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                    Existing Purchasing Prices in SAP (read-only)
                  </span>
                </div>
                {sapPriceSpecsLoading ? (
                  <div className="text-[13px] text-[#475467] py-2">Reading from SAP...</div>
                ) : sapPriceSpecsError ? (
                  <div className="text-[13px] text-[#B54708] py-1" data-testid="sap-price-specs-error">{sapPriceSpecsError}</div>
                ) : sapPriceSpecs.length === 0 ? (
                  <div className="text-[13px] text-[#98A2B3] py-1" data-testid="sap-price-specs-empty">
                    No purchasing price records found in SAP for "{activeProductId}"
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-[13px] border-collapse" data-testid="sap-price-specs-table">
                      <thead>
                        <tr>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-right text-xs font-bold text-[#344054] font-heading uppercase">Price</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Valid From</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Valid To</th>
                          <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1 text-left text-xs font-bold text-[#344054] font-heading uppercase">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sapPriceSpecs.map((p, i) => (
                          <tr key={p.sap_id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`sap-price-spec-row-${i}`}>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#101828]">
                              {p.supplier_name || `Unknown SAP Supplier (${(p.supplier_uuid || "").slice(0, 8)}...)`}
                            </td>
                            <td className={`border border-[#D0D5DD] px-1.5 py-1 text-right tabular-nums ${p.price ? "text-[#101828]" : "text-[#98A2B3]"}`}>
                              {p.price != null ? `${p.currency || ""} ${p.price}` : "—"}
                            </td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{p.start_date || "—"}</td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1 text-[#475467]">{p.end_date === "9999-12-31" ? "Open" : p.end_date || "—"}</td>
                            <td className="border border-[#D0D5DD] px-1.5 py-1">
                              <Badge
                                variant="outline"
                                className={
                                  p.release_status_code === "3"
                                    ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] text-xs"
                                    : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs"
                                }
                              >
                                {p.release_status_code === "3" ? "Released" : `Code ${p.release_status_code || "—"}`}
                              </Badge>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="px-2.5 pt-2.5 pb-1 flex items-center gap-1.5">
                <Badge variant="outline" className="bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs">Local</Badge>
                <span className="font-heading text-xs font-bold text-[#344054] uppercase tracking-wide">
                  App-Managed Assignments (editable - for splitting purchase requisitions)
                </span>
              </div>
              <div className="overflow-x-auto">
              <table className="w-full text-[13px] border-collapse" data-testid="part-supplier-table">
                <thead>
                  <tr>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Supplier</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">Quota %</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">Lead Time (D)</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-right text-xs font-bold text-[#344054] font-heading uppercase">Unit Price</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Preference</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase">Notes</th>
                    <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-center text-xs font-bold text-[#344054] font-heading uppercase w-20">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {assignmentsLoading ? (
                    <tr>
                      <td colSpan={7} className="border border-[#D0D5DD] text-center py-6 text-[13px] text-[#475467]">Loading...</td>
                    </tr>
                  ) : assignments.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="border border-[#D0D5DD] text-center py-6 text-[13px] text-[#475467]" data-testid="part-supplier-no-assignments">
                        No suppliers assigned to "{activeProductId}" yet
                      </td>
                    </tr>
                  ) : (
                    assignments.map((a, i) => (
                      <tr key={a.id} className={i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} data-testid={`part-supplier-row-${i}`}>
                        <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{a.supplier_name || a.supplier_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]">{a.quota_percent ?? "—"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]">{a.lead_time_days ?? "—"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-right tabular-nums text-[#101828]">
                          {a.unit_price != null ? `${a.currency || ""} ${a.unit_price}` : "—"}
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1">
                          <Badge
                            variant="outline"
                            className={
                              a.preference === "Preferred"
                                ? "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] text-xs inline-flex items-center gap-1"
                                : "bg-[#F2F4F7] text-[#475467] border-[#D0D5DD] text-xs inline-flex items-center gap-1"
                            }
                          >
                            {a.preference === "Preferred" && <Star size={10} weight="fill" />}
                            {a.preference}
                          </Badge>
                        </td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-[#475467]">{a.notes || "—"}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1 text-center">
                          <div className="flex items-center justify-center gap-1">
                            <button
                              type="button"
                              onClick={() => openEditAssignment(a)}
                              className="p-1 text-[#475467] hover:text-[#004B87]"
                              data-testid={`edit-part-supplier-button-${i}`}
                            >
                              <PencilSimple size={14} />
                            </button>
                            <button
                              type="button"
                              onClick={() => deleteAssignment(a)}
                              className="p-1 text-[#475467] hover:text-[#B42318]"
                              data-testid={`delete-part-supplier-button-${i}`}
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

      {/* Assignment Add/Edit Dialog */}
      <Dialog open={assignmentDialogOpen} onOpenChange={setAssignmentDialogOpen}>
        <DialogContent className="max-w-md" data-testid="part-supplier-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">
              {editingAssignment ? "Edit Assignment" : `Assign Supplier to ${activeProductId}`}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <label className={labelCls}>Supplier *</label>
              <Select
                value={assignmentForm.supplier_id}
                onValueChange={(v) => setAssignmentForm((f) => ({ ...f, supplier_id: v }))}
                disabled={!!editingAssignment}
              >
                <SelectTrigger className="h-8 mt-1 text-[13px] rounded-sm border-[#D0D5DD]" data-testid="assignment-form-supplier-select">
                  <SelectValue placeholder="Pick a supplier" />
                </SelectTrigger>
                <SelectContent>
                  {suppliers.map((s) => (
                    <SelectItem key={s.id} value={s.id} data-testid={`assignment-supplier-option-${s.id}`}>
                      {s.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelCls}>Quota %</label>
                <input
                  type="number"
                  min="0"
                  max="100"
                  value={assignmentForm.quota_percent}
                  onChange={(e) => setAssignmentForm((f) => ({ ...f, quota_percent: e.target.value }))}
                  className={`${inputCls} mt-1`}
                  data-testid="assignment-form-quota-input"
                />
              </div>
              <div>
                <label className={labelCls}>Lead Time (Days)</label>
                <input
                  type="number"
                  min="0"
                  value={assignmentForm.lead_time_days}
                  onChange={(e) => setAssignmentForm((f) => ({ ...f, lead_time_days: e.target.value }))}
                  className={`${inputCls} mt-1`}
                  data-testid="assignment-form-lead-time-input"
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelCls}>Unit Price</label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={assignmentForm.unit_price}
                  onChange={(e) => setAssignmentForm((f) => ({ ...f, unit_price: e.target.value }))}
                  className={`${inputCls} mt-1`}
                  data-testid="assignment-form-unit-price-input"
                />
              </div>
              <div>
                <label className={labelCls}>Currency</label>
                <input
                  type="text"
                  value={assignmentForm.currency}
                  onChange={(e) => setAssignmentForm((f) => ({ ...f, currency: e.target.value }))}
                  className={`${inputCls} mt-1`}
                  data-testid="assignment-form-currency-input"
                />
              </div>
            </div>
            <div>
              <label className={labelCls}>Preference</label>
              <Select
                value={assignmentForm.preference}
                onValueChange={(v) => setAssignmentForm((f) => ({ ...f, preference: v }))}
              >
                <SelectTrigger className="h-8 mt-1 text-[13px] rounded-sm border-[#D0D5DD]" data-testid="assignment-form-preference-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="Preferred" data-testid="assignment-preference-option-preferred">Preferred</SelectItem>
                  <SelectItem value="Backup" data-testid="assignment-preference-option-backup">Backup</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className={labelCls}>Notes</label>
              <input
                type="text"
                value={assignmentForm.notes}
                onChange={(e) => setAssignmentForm((f) => ({ ...f, notes: e.target.value }))}
                className={`${inputCls} mt-1`}
                data-testid="assignment-form-notes-input"
              />
            </div>
            <Button
              type="button"
              onClick={saveAssignment}
              disabled={savingAssignment}
              className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm"
              data-testid="assignment-form-save-button"
            >
              {savingAssignment ? "Saving..." : editingAssignment ? "Save Changes" : "Assign Supplier"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
