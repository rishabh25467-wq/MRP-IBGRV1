import { useState, useEffect, useMemo, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import * as XLSX from "xlsx";
import {
  Database,
  Shield,
  MagnifyingGlass,
  ArrowClockwise,
  SortAscending,
  SortDescending,
  Sparkle,
  Plus,
  Tag,
  X,
  CloudArrowUp,
  DownloadSimple,
  UploadSimple,
  Scales,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, SelectSeparator } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;
const ADD_NEW_CATEGORY_VALUE = "__add_new_category__";

const PHYSICAL_FIELDS = [
  { key: "net_weight_kg", label: "Net Weight", unit: "kg" },
  { key: "surface_area_sqin", label: "Surface Area", unit: "in²" },
];

const formatDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

export default function AdminPage() {
  const [items, setItems] = useState([]);
  const [categoriesTaxonomy, setCategoriesTaxonomy] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [selected, setSelected] = useState(new Set());
  const [savingIds, setSavingIds] = useState(new Set());
  const [mslInputs, setMslInputs] = useState({});
  const [leadTimeInputs, setLeadTimeInputs] = useState({});
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 50;
  const [pushDialogItem, setPushDialogItem] = useState(null);
  const [pushDialogSapData, setPushDialogSapData] = useState(null);
  const [pushDialogLoading, setPushDialogLoading] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [physicalDialogItem, setPhysicalDialogItem] = useState(null);
  const [physicalSapData, setPhysicalSapData] = useState(null);
  const [physicalSapLoading, setPhysicalSapLoading] = useState(false);
  const [physicalInputs, setPhysicalInputs] = useState({});
  const [physicalSaving, setPhysicalSaving] = useState(false);
  const [physicalPushing, setPhysicalPushing] = useState(false);
  const [recategorizing, setRecategorizing] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [importing, setImporting] = useState(false);
  const [pushAllOpen, setPushAllOpen] = useState(false);
  const [pushAllStatus, setPushAllStatus] = useState("idle"); // idle | running | done | failed
  const [pushAllProgress, setPushAllProgress] = useState({ processed: 0, total: 0 });
  const [pushAllResult, setPushAllResult] = useState(null);
  const [pushAllError, setPushAllError] = useState(null);
  const [sortConfig, setSortConfig] = useState({ field: "product_id", direction: "asc" });
  const [categoryDialogOpen, setCategoryDialogOpen] = useState(false);
  const [pendingRowForNewCategory, setPendingRowForNewCategory] = useState(null);
  const [newCategoryName, setNewCategoryName] = useState("");
  const [addingCategory, setAddingCategory] = useState(false);
  const [deletingCategory, setDeletingCategory] = useState(null);

  const loadComponents = async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/components`);
      setItems(data.items);
      setCategoriesTaxonomy(data.categories);
    } catch (err) {
      toast.error("Could not load components", { description: err?.response?.data?.detail || err.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadComponents();
  }, []);

  const markSaving = (productId, isSaving) => {
    setSavingIds((prev) => {
      const next = new Set(prev);
      isSaving ? next.add(productId) : next.delete(productId);
      return next;
    });
  };

  const updateCategory = async (productId, category) => {
    markSaving(productId, true);
    try {
      const { data } = await axios.patch(`${API}/admin/components/${encodeURIComponent(productId)}`, { category });
      setItems((prev) => prev.map((it) => (it.product_id === productId ? data : it)));
      toast.success(`${productId} set to "${category}"`);
    } catch (err) {
      toast.error("Could not save category", { description: err?.response?.data?.detail || err.message });
    } finally {
      markSaving(productId, false);
    }
  };

  const handleCategorySelect = (productId, value) => {
    if (value === ADD_NEW_CATEGORY_VALUE) {
      setPendingRowForNewCategory(productId);
      setNewCategoryName("");
      setCategoryDialogOpen(true);
      return;
    }
    updateCategory(productId, value);
  };

  const openCategoryManager = () => {
    setPendingRowForNewCategory(null);
    setNewCategoryName("");
    setCategoryDialogOpen(true);
  };

  const submitNewCategory = async () => {
    const name = newCategoryName.trim();
    if (!name) return;
    setAddingCategory(true);
    try {
      const { data } = await axios.post(`${API}/admin/categories`, { name });
      setCategoriesTaxonomy(data.categories);
      toast.success(`Category "${name}" added`);
      if (pendingRowForNewCategory) {
        await updateCategory(pendingRowForNewCategory, name);
        setCategoryDialogOpen(false);
      }
      setNewCategoryName("");
      setPendingRowForNewCategory(null);
    } catch (err) {
      toast.error("Could not add category", { description: err?.response?.data?.detail || err.message });
    } finally {
      setAddingCategory(false);
    }
  };

  const removeCategory = async (name) => {
    setDeletingCategory(name);
    try {
      const { data } = await axios.delete(`${API}/admin/categories/${encodeURIComponent(name)}`);
      setCategoriesTaxonomy(data.categories);
      toast.success(`Category "${name}" removed`);
    } catch (err) {
      toast.error("Could not remove category", { description: err?.response?.data?.detail || err.message });
    } finally {
      setDeletingCategory(null);
    }
  };

  const saveMsl = async (productId) => {
    const raw = mslInputs[productId];
    if (raw === undefined) return;
    const msl = raw === "" ? 0 : Number(raw);
    if (Number.isNaN(msl)) {
      toast.error("MSL must be a number");
      return;
    }
    markSaving(productId, true);
    try {
      const { data } = await axios.patch(`${API}/admin/components/${encodeURIComponent(productId)}`, { msl });
      setItems((prev) => prev.map((it) => (it.product_id === productId ? data : it)));
      setMslInputs((prev) => {
        const next = { ...prev };
        delete next[productId];
        return next;
      });
    } catch (err) {
      toast.error("Could not save MSL", { description: err?.response?.data?.detail || err.message });
    } finally {
      markSaving(productId, false);
    }
  };

  const saveLeadTime = async (productId) => {
    const raw = leadTimeInputs[productId];
    if (raw === undefined) return;
    const leadTimeDays = raw === "" ? 0 : Number(raw);
    if (Number.isNaN(leadTimeDays)) {
      toast.error("Lead Time must be a number");
      return;
    }
    markSaving(productId, true);
    try {
      const { data } = await axios.patch(`${API}/admin/components/${encodeURIComponent(productId)}`, { lead_time_days: leadTimeDays });
      setItems((prev) => prev.map((it) => (it.product_id === productId ? data : it)));
      setLeadTimeInputs((prev) => {
        const next = { ...prev };
        delete next[productId];
        return next;
      });
    } catch (err) {
      toast.error("Could not save Lead Time", { description: err?.response?.data?.detail || err.message });
    } finally {
      markSaving(productId, false);
    }
  };

  const openPushDialog = async (item) => {
    setPushDialogItem(item);
    setPushDialogSapData(null);
    setPushDialogLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/components/${encodeURIComponent(item.product_id)}/sap-planning`);
      setPushDialogSapData(data);
    } catch (err) {
      setPushDialogSapData({ error: err?.response?.data?.detail || err.message });
    } finally {
      setPushDialogLoading(false);
    }
  };

  const confirmPushToSap = async () => {
    if (!pushDialogItem) return;
    const productId = pushDialogItem.product_id;
    setPushing(true);
    try {
      const { data } = await axios.post(`${API}/admin/components/${encodeURIComponent(productId)}/push-to-sap`);
      toast.success(`Pushed to SAP across ${data.planning_areas_updated} planning area${data.planning_areas_updated === 1 ? "" : "s"}`, {
        description: `${productId}: Safety Stock ${data.safety_stock ?? "—"}, Lead Time ${data.lead_time_days ?? "—"} day(s)`,
      });
      setItems((prev) =>
        prev.map((it) => (it.product_id === productId ? { ...it, sap_pushed_at: new Date().toISOString() } : it))
      );
      setPushDialogItem(null);
    } catch (err) {
      toast.error("Push to SAP failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setPushing(false);
    }
  };

  const openPhysicalDialog = (item) => {
    setPhysicalDialogItem(item);
    setPhysicalSapData(null);
    setPhysicalInputs(Object.fromEntries(PHYSICAL_FIELDS.map(({ key }) => [key, item[key] ?? ""])));
  };

  const pullPhysicalFromSap = async () => {
    if (!physicalDialogItem) return;
    setPhysicalSapLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/components/${encodeURIComponent(physicalDialogItem.product_id)}/sap-physical-attributes`);
      setPhysicalSapData(data);
    } catch (err) {
      setPhysicalSapData({ error: err?.response?.data?.detail || err.message });
    } finally {
      setPhysicalSapLoading(false);
    }
  };

  const savePhysicalLocally = async () => {
    if (!physicalDialogItem) return;
    const productId = physicalDialogItem.product_id;
    const payload = {};
    for (const { key } of PHYSICAL_FIELDS) {
      const raw = physicalInputs[key];
      if (raw !== "" && raw !== undefined) {
        const num = Number(raw);
        if (Number.isNaN(num)) {
          toast.error(`${key} must be a number`);
          return;
        }
        payload[key] = num;
      }
    }
    if (Object.keys(payload).length === 0) {
      toast.error("Enter at least one value first");
      return;
    }
    setPhysicalSaving(true);
    try {
      const { data } = await axios.patch(`${API}/admin/components/${encodeURIComponent(productId)}`, payload);
      setItems((prev) => prev.map((it) => (it.product_id === productId ? data : it)));
      setPhysicalDialogItem(data);
      toast.success(`Saved weight/dimensions for ${productId}`);
    } catch (err) {
      toast.error("Could not save", { description: err?.response?.data?.detail || err.message });
    } finally {
      setPhysicalSaving(false);
    }
  };

  const pushPhysicalToSap = async () => {
    if (!physicalDialogItem) return;
    const productId = physicalDialogItem.product_id;
    setPhysicalPushing(true);
    try {
      const { data } = await axios.post(`${API}/admin/components/${encodeURIComponent(productId)}/push-physical-attributes-to-sap`);
      toast.success(`Pushed ${data.pushed_fields.length} field(s) to SAP`, { description: productId });
      setItems((prev) =>
        prev.map((it) => (it.product_id === productId ? { ...it, sap_physical_pushed_at: new Date().toISOString() } : it))
      );
      setPhysicalDialogItem(null);
    } catch (err) {
      toast.error("Push to SAP failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setPhysicalPushing(false);
    }
  };

  const toggleSelect = (productId) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(productId) ? next.delete(productId) : next.add(productId);
      return next;
    });
  };

  const toggleSelectAllVisible = () => {
    setSelected((prev) => {
      const allVisible = pagedItems.map((it) => it.product_id);
      const allSelected = allVisible.every((id) => prev.has(id));
      const next = new Set(prev);
      allVisible.forEach((id) => (allSelected ? next.delete(id) : next.add(id)));
      return next;
    });
  };

  const recategorizeSelected = async () => {
    if (selected.size === 0) return;
    setRecategorizing(true);
    try {
      const { data } = await axios.post(`${API}/admin/components/recategorize`, {
        product_ids: Array.from(selected),
      });
      setItems((prev) =>
        prev.map((it) =>
          data.categories[it.product_id]
            ? { ...it, category: data.categories[it.product_id], category_source: "ai" }
            : it
        )
      );
      const changedCount = Object.keys(data.categories).length;
      toast.success(`Re-categorized ${changedCount} item${changedCount === 1 ? "" : "s"}`, {
        description: data.skipped_manual.length
          ? `Skipped ${data.skipped_manual.length} manually-corrected item(s) to protect your fixes.`
          : undefined,
      });
      setSelected(new Set());
    } catch (err) {
      toast.error("Re-categorization failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setRecategorizing(false);
    }
  };

  const toggleSort = (field) => {
    setSortConfig((prev) =>
      prev.field === field ? { field, direction: prev.direction === "asc" ? "desc" : "asc" } : { field, direction: "asc" }
    );
  };

  const backfillSapLinks = async () => {
    setBackfilling(true);
    try {
      const { data } = await axios.post(`${API}/admin/components/backfill-sap-links`);
      if (data.updated > 0) {
        toast.success(`Linked ${data.updated} component${data.updated === 1 ? "" : "s"} to SAP`, {
          description: "Push to SAP is now available for them. Refreshing list...",
        });
        loadComponents();
      } else {
        toast.info("Nothing to backfill", { description: "Every component already has a SAP link or none exist in the cache yet." });
      }
    } catch (err) {
      toast.error("Backfill failed", { description: err?.response?.data?.detail || err.message });
    } finally {
      setBackfilling(false);
    }
  };

  const exportMslLeadTimeTemplate = () => {
    const rows = items.map((it) => ({
      "Product ID": it.product_id,
      Description: it.description || "",
      Category: it.category || "",
      MSL: it.msl ?? "",
      "Lead Time (Days)": it.lead_time_days ?? "",
      "Has SAP Link": it.has_sap_link ? "Yes" : "No",
    }));
    const worksheet = XLSX.utils.json_to_sheet(rows);
    worksheet["!cols"] = [{ wch: 18 }, { wch: 40 }, { wch: 16 }, { wch: 10 }, { wch: 16 }, { wch: 12 }];
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, "MSL & Lead Time");
    XLSX.writeFile(workbook, "Component_MSL_LeadTime_Template.xlsx");
    toast.success("Template downloaded", {
      description: "Edit the MSL / Lead Time (Days) columns, then use Import from Excel to bring your changes back in.",
    });
  };

  const handleImportFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;

    setImporting(true);
    try {
      const buffer = await file.arrayBuffer();
      const workbook = XLSX.read(buffer, { type: "array" });
      const sheet = workbook.Sheets[workbook.SheetNames[0]];
      const rows = XLSX.utils.sheet_to_json(sheet);

      let updated = 0;
      const notFound = [];
      const CONCURRENCY = 8;
      for (let i = 0; i < rows.length; i += CONCURRENCY) {
        const batch = rows.slice(i, i + CONCURRENCY);
        await Promise.all(
          batch.map(async (row) => {
            const productId = row["Product ID"];
            if (!productId) return;
            const payload = {};
            if (row["MSL"] !== undefined && row["MSL"] !== "") payload.msl = Number(row["MSL"]);
            if (row["Lead Time (Days)"] !== undefined && row["Lead Time (Days)"] !== "") {
              payload.lead_time_days = Number(row["Lead Time (Days)"]);
            }
            if (Object.keys(payload).length === 0) return;
            try {
              await axios.patch(`${API}/admin/components/${encodeURIComponent(productId)}`, payload);
              updated += 1;
            } catch (err) {
              notFound.push(productId);
            }
          })
        );
      }

      toast.success(`Imported ${updated} row${updated === 1 ? "" : "s"}`, {
        description: notFound.length ? `${notFound.length} product ID(s) not found: ${notFound.slice(0, 5).join(", ")}${notFound.length > 5 ? "..." : ""}` : "MSL / Lead Time updated. Refreshing list...",
      });
      loadComponents();
    } catch (err) {
      toast.error("Import failed", { description: err.message });
    } finally {
      setImporting(false);
    }
  };

  const startPushAllToSap = async () => {
    setPushAllOpen(true);
    setPushAllStatus("running");
    setPushAllProgress({ processed: 0, total: 0 });
    setPushAllResult(null);
    setPushAllError(null);
    try {
      const { data } = await axios.post(`${API}/admin/components/push-all-to-sap`);
      const jobId = data.job_id;
      const poll = async () => {
        const { data: job } = await axios.get(`${API}/admin/components/push-all-to-sap/${jobId}`);
        if (job.progress) setPushAllProgress(job.progress);
        if (job.status === "running") {
          setTimeout(poll, 1500);
        } else if (job.status === "done") {
          setPushAllStatus("done");
          setPushAllResult(job.result);
          loadComponents();
        } else {
          setPushAllStatus("failed");
          setPushAllError(job.error);
        }
      };
      poll();
    } catch (err) {
      setPushAllStatus("failed");
      setPushAllError(err?.response?.data?.detail || err.message);
    }
  };

  const filteredSorted = useMemo(() => {
    const q = search.trim().toLowerCase();
    let result = items.filter((it) => {
      const matchesSearch =
        !q || it.product_id.toLowerCase().includes(q) || (it.description || "").toLowerCase().includes(q);
      const matchesCategory =
        categoryFilter === "all" ||
        (categoryFilter === "uncategorized" ? !it.category : it.category === categoryFilter);
      return matchesSearch && matchesCategory;
    });
    result = [...result].sort((a, b) => {
      const numericFields = ["msl", "lead_time_days"];
      if (numericFields.includes(sortConfig.field)) {
        const av = a[sortConfig.field] ?? -Infinity;
        const bv = b[sortConfig.field] ?? -Infinity;
        const cmp = av - bv;
        return sortConfig.direction === "asc" ? cmp : -cmp;
      }
      const av = (a[sortConfig.field] || "").toString().toLowerCase();
      const bv = (b[sortConfig.field] || "").toString().toLowerCase();
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortConfig.direction === "asc" ? cmp : -cmp;
    });
    return result;
  }, [items, search, categoryFilter, sortConfig]);

  const totalPages = Math.max(1, Math.ceil(filteredSorted.length / PAGE_SIZE));
  const pagedItems = filteredSorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  useEffect(() => {
    setPage(1);
  }, [search, categoryFilter, sortConfig]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [totalPages, page]);

  const allVisibleSelected = pagedItems.length > 0 && pagedItems.every((it) => selected.has(it.product_id));
  const SortIcon = sortConfig.direction === "asc" ? SortAscending : SortDescending;

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]">
      <Toaster position="top-right" />

      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Master Data</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <div className="shrink-0 w-8" />
      </header>

      <div className="bg-white border-b border-[#D0D5DD] p-2 flex items-center gap-3 shrink-0 flex-wrap">
        <div className="relative">
          <MagnifyingGlass size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
          <input
            type="text"
            placeholder="Search product ID or description..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-8 w-64 pl-7 pr-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
            data-testid="admin-search-input"
          />
        </div>
        <div className="flex items-center gap-1.5">
          <label htmlFor="admin-category-filter" className="font-heading text-xs font-bold text-[#475467] uppercase">
            Category
          </label>
          <Select value={categoryFilter} onValueChange={setCategoryFilter}>
            <SelectTrigger id="admin-category-filter" className="h-8 w-48 text-[13px] rounded-sm border-[#D0D5DD]" data-testid="admin-category-filter">
              <SelectValue placeholder="All Categories" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Categories</SelectItem>
              <SelectItem value="uncategorized">Uncategorized</SelectItem>
              {categoriesTaxonomy.map((cat) => (
                <SelectItem key={cat} value={cat} data-testid={`admin-category-filter-option-${cat}`}>
                  {cat}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button
          type="button"
          variant="outline"
          onClick={loadComponents}
          disabled={loading}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
          data-testid="admin-refresh-button"
        >
          <ArrowClockwise size={13} className={`mr-1.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={openCategoryManager}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
          data-testid="admin-manage-categories-button"
        >
          <Tag size={13} className="mr-1.5" />
          Manage Categories ({categoriesTaxonomy.length})
        </Button>
        <Button
          type="button"
          onClick={recategorizeSelected}
          disabled={selected.size === 0 || recategorizing}
          className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm"
          data-testid="admin-recategorize-button"
        >
          <Sparkle size={13} className={`mr-1.5 ${recategorizing ? "animate-pulse" : ""}`} />
          {recategorizing ? "Re-Categorising..." : `Re-Categorise Selected (${selected.size})`}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={backfillSapLinks}
          disabled={backfilling}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
          data-testid="admin-backfill-sap-links-button"
          title="Fill in SAP links for components already sitting in the BOM cache from a past explosion, so Push to SAP becomes available for them"
        >
          <CloudArrowUp size={13} className={`mr-1.5 ${backfilling ? "animate-pulse" : ""}`} />
          {backfilling ? "Backfilling..." : "Backfill SAP Links"}
        </Button>
        <div className="w-px h-6 bg-[#D0D5DD]" />
        <Button
          type="button"
          variant="outline"
          onClick={exportMslLeadTimeTemplate}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
          data-testid="admin-export-excel-button"
          title="Download MSL / Lead Time for every component as an Excel file to edit offline"
        >
          <DownloadSimple size={13} className="mr-1.5" />
          Export to Excel
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => document.getElementById("admin-import-file-input").click()}
          disabled={importing}
          className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
          data-testid="admin-import-excel-button"
          title="Upload an edited MSL / Lead Time Excel file to update components in bulk"
        >
          <UploadSimple size={13} className={`mr-1.5 ${importing ? "animate-pulse" : ""}`} />
          {importing ? "Importing..." : "Import from Excel"}
        </Button>
        <input
          id="admin-import-file-input"
          type="file"
          accept=".xlsx,.xls"
          onChange={handleImportFile}
          className="hidden"
          data-testid="admin-import-file-input"
        />
        <Button
          type="button"
          onClick={startPushAllToSap}
          className="h-8 bg-[#B54708] hover:bg-[#93370D] text-white text-xs rounded-sm"
          data-testid="admin-push-all-to-sap-button"
          title="Push MSL / Lead Time to SAP for every linked component that has a value set"
        >
          <CloudArrowUp size={13} className="mr-1.5" />
          Push All to SAP
        </Button>
        <span className="text-xs text-[#475467] ml-auto font-sans" data-testid="admin-item-count">
          {filteredSorted.length} of {items.length} components
        </span>
      </div>

      <main className="flex-1 overflow-auto p-3">
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-auto" data-testid="admin-components-table-container">
          <table className="w-full text-[13px] border-collapse">
            <thead className="sticky top-0 z-[1]">
              <tr>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 w-8">
                  <Checkbox
                    checked={allVisibleSelected}
                    onCheckedChange={toggleSelectAllVisible}
                    data-testid="admin-select-all-checkbox"
                  />
                </th>
                <th
                  onClick={() => toggleSort("product_id")}
                  className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                  data-testid="admin-sort-product-id"
                >
                  <span className="inline-flex items-center gap-1">
                    Product ID
                    {sortConfig.field === "product_id" && <SortIcon size={11} weight="bold" />}
                  </span>
                </th>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                  Description
                </th>
                <th
                  onClick={() => toggleSort("category")}
                  className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                  data-testid="admin-sort-category"
                >
                  <span className="inline-flex items-center gap-1">
                    Category
                    {sortConfig.field === "category" && <SortIcon size={11} weight="bold" />}
                  </span>
                </th>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                  Source
                </th>
                <th
                  onClick={() => toggleSort("msl")}
                  className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                  data-testid="admin-sort-msl"
                >
                  <span className="inline-flex items-center gap-1">
                    MSL
                    {sortConfig.field === "msl" && <SortIcon size={11} weight="bold" />}
                  </span>
                </th>
                <th
                  onClick={() => toggleSort("lead_time_days")}
                  className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                  data-testid="admin-sort-lead-time"
                >
                  <span className="inline-flex items-center gap-1">
                    Lead Time (Days)
                    {sortConfig.field === "lead_time_days" && <SortIcon size={11} weight="bold" />}
                  </span>
                </th>
                <th
                  onClick={() => toggleSort("updated_at")}
                  className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide cursor-pointer hover:bg-[#DDE1E8] select-none"
                  data-testid="admin-sort-updated-at"
                >
                  <span className="inline-flex items-center gap-1">
                    Updated
                    {sortConfig.field === "updated_at" && <SortIcon size={11} weight="bold" />}
                  </span>
                </th>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                  SAP Push
                </th>
                <th className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase tracking-wide">
                  Weight/Area
                </th>
              </tr>
            </thead>
            <tbody>
              {pagedItems.map((it, i) => (
                <tr
                  key={it.product_id}
                  className={`${i % 2 === 0 ? "bg-white" : "bg-[#F9FAFB]"} hover:bg-[#F0F4F8] transition-colors duration-150 ${savingIds.has(it.product_id) ? "opacity-60" : ""}`}
                  data-testid={`admin-row-${it.product_id}`}
                >
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    <Checkbox
                      checked={selected.has(it.product_id)}
                      onCheckedChange={() => toggleSelect(it.product_id)}
                      data-testid={`admin-select-${it.product_id}`}
                    />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1 font-medium text-[#101828]">{it.product_id}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-[#101828]">{it.description || "—"}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    <Select
                      value={it.category || "__none__"}
                      onValueChange={(val) => handleCategorySelect(it.product_id, val)}
                      disabled={savingIds.has(it.product_id)}
                    >
                      <SelectTrigger className="h-7 text-xs rounded-sm border-[#D0D5DD]" data-testid={`admin-category-select-${it.product_id}`}>
                        <SelectValue placeholder="Uncategorized" />
                      </SelectTrigger>
                      <SelectContent>
                        {categoriesTaxonomy.map((cat) => (
                          <SelectItem key={cat} value={cat}>
                            {cat}
                          </SelectItem>
                        ))}
                        <SelectSeparator />
                        <SelectItem value={ADD_NEW_CATEGORY_VALUE} data-testid={`admin-add-category-option-${it.product_id}`}>
                          <span className="inline-flex items-center gap-1 text-[#004B87]">
                            <Plus size={11} weight="bold" />
                            Add New Category...
                          </span>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    <Badge
                      variant="outline"
                      className={
                        it.category_source === "manual"
                          ? "bg-[#E5F0FA] text-[#004B87] border-[#B8D4ED] rounded text-xs"
                          : "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6] rounded text-xs"
                      }
                      data-testid={`admin-source-badge-${it.product_id}`}
                    >
                      {it.category_source === "manual" ? "Manual" : it.category ? "AI" : "—"}
                    </Badge>
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    <input
                      type="number"
                      min="0"
                      step="any"
                      placeholder="0"
                      value={mslInputs[it.product_id] ?? (it.msl ?? "")}
                      onChange={(e) => setMslInputs((prev) => ({ ...prev, [it.product_id]: e.target.value }))}
                      onBlur={() => saveMsl(it.product_id)}
                      onKeyDown={(e) => e.key === "Enter" && saveMsl(it.product_id)}
                      disabled={savingIds.has(it.product_id)}
                      className="h-7 w-20 px-1.5 text-xs border border-[#D0D5DD] rounded-sm bg-white text-[#101828] tabular-nums focus:outline-none focus:ring-1 focus:ring-[#004B87]"
                      data-testid={`admin-msl-input-${it.product_id}`}
                    />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    <input
                      type="number"
                      min="0"
                      step="any"
                      placeholder="0"
                      value={leadTimeInputs[it.product_id] ?? (it.lead_time_days ?? "")}
                      onChange={(e) => setLeadTimeInputs((prev) => ({ ...prev, [it.product_id]: e.target.value }))}
                      onBlur={() => saveLeadTime(it.product_id)}
                      onKeyDown={(e) => e.key === "Enter" && saveLeadTime(it.product_id)}
                      disabled={savingIds.has(it.product_id)}
                      className="h-7 w-20 px-1.5 text-xs border border-[#D0D5DD] rounded-sm bg-white text-[#101828] tabular-nums focus:outline-none focus:ring-1 focus:ring-[#004B87]"
                      data-testid={`admin-lead-time-input-${it.product_id}`}
                    />
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1 text-xs text-[#667085]">{formatDate(it.updated_at)}</td>
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    {it.has_sap_link ? (
                      <div className="flex items-center gap-1.5">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => openPushDialog(it)}
                          disabled={it.msl == null && it.lead_time_days == null}
                          className="h-6 text-xs rounded-sm border-[#D0D5DD] text-[#344054] px-2"
                          data-testid={`push-to-sap-button-${it.product_id}`}
                        >
                          <CloudArrowUp size={12} className="mr-1" />
                          Push to SAP
                        </Button>
                        {it.sap_pushed_at && (
                          <span className="text-xs text-[#667085]" title={formatDate(it.sap_pushed_at)}>
                            <ArrowClockwise size={10} className="inline mr-0.5" />
                            {formatDate(it.sap_pushed_at)}
                          </span>
                        )}
                      </div>
                    ) : (
                      <span className="text-xs text-[#98A2B3]" title="Open this part in BOM Explorer or a Purchasing Plan run first to capture its SAP link">
                        No SAP link yet
                      </span>
                    )}
                  </td>
                  <td className="border border-[#D0D5DD] px-2 py-1">
                    <div className="flex items-center gap-1.5">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => openPhysicalDialog(it)}
                        className="h-6 text-xs rounded-sm border-[#D0D5DD] text-[#344054] px-2"
                        data-testid={`weight-dims-button-${it.product_id}`}
                      >
                        <Scales size={12} className="mr-1" />
                        Weight/Area
                      </Button>
                      {it.sap_physical_pushed_at && (
                        <span className="text-xs text-[#667085]" title={formatDate(it.sap_physical_pushed_at)}>
                          <ArrowClockwise size={10} className="inline mr-0.5" />
                          {formatDate(it.sap_physical_pushed_at)}
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {filteredSorted.length === 0 && (
                <tr>
                  <td colSpan={10} className="border border-[#D0D5DD] text-center py-8 text-[13px] text-[#475467]" data-testid="admin-no-components">
                    {loading
                      ? "Loading components..."
                      : items.length === 0
                      ? "No components discovered yet - run a BOM Explorer search or generate a Purchasing Plan first"
                      : "No components match your filters"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {filteredSorted.length > 0 && (
          <div className="flex items-center justify-between mt-2 px-1" data-testid="admin-pagination">
            <span className="text-xs text-[#475467] font-sans">
              Showing {(page - 1) * PAGE_SIZE + 1}-{Math.min(page * PAGE_SIZE, filteredSorted.length)} of {filteredSorted.length}
            </span>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="h-7 text-xs rounded-sm border-[#D0D5DD] text-[#344054] px-2"
                data-testid="admin-pagination-prev"
              >
                Previous
              </Button>
              <span className="text-xs text-[#475467] font-sans tabular-nums" data-testid="admin-pagination-page-indicator">
                Page {page} of {totalPages}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="h-7 text-xs rounded-sm border-[#D0D5DD] text-[#344054] px-2"
                data-testid="admin-pagination-next"
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </main>

      <Dialog
        open={categoryDialogOpen}
        onOpenChange={(open) => {
          setCategoryDialogOpen(open);
          if (!open) setPendingRowForNewCategory(null);
        }}
      >
        <DialogContent className="max-w-md" data-testid="manage-categories-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">
              {pendingRowForNewCategory ? `Add New Category for ${pendingRowForNewCategory}` : "Manage Categories"}
            </DialogTitle>
          </DialogHeader>
          <div className="flex flex-wrap gap-1.5 max-h-40 overflow-auto" data-testid="manage-categories-list">
            {categoriesTaxonomy.map((cat) => (
              <Badge
                key={cat}
                variant="outline"
                className={`bg-[#F9FAFB] text-[#344054] border-[#D0D5DD] rounded text-xs pr-1 gap-1 ${deletingCategory === cat ? "opacity-50" : ""}`}
                data-testid={`category-badge-${cat}`}
              >
                {cat}
                <button
                  type="button"
                  onClick={() => removeCategory(cat)}
                  disabled={deletingCategory === cat}
                  className="ml-0.5 rounded-full hover:bg-[#E4E7EC] p-0.5 transition-colors"
                  aria-label={`Remove ${cat}`}
                  data-testid={`remove-category-button-${cat}`}
                >
                  <X size={10} weight="bold" />
                </button>
              </Badge>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              placeholder="New category name..."
              value={newCategoryName}
              onChange={(e) => setNewCategoryName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitNewCategory()}
              className="h-8 flex-1 px-2 text-[13px] rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87]"
              data-testid="new-category-name-input"
              autoFocus
            />
            <Button
              type="button"
              onClick={submitNewCategory}
              disabled={!newCategoryName.trim() || addingCategory}
              className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm shrink-0"
              data-testid="submit-new-category-button"
            >
              <Plus size={13} className="mr-1" />
              {addingCategory ? "Adding..." : pendingRowForNewCategory ? "Add & Apply" : "Add Category"}
            </Button>
          </div>
          <p className="text-xs text-[#667085] font-sans">
            New categories are immediately available for manual selection and future AI categorization.
          </p>
        </DialogContent>
      </Dialog>

      <Dialog open={!!pushDialogItem} onOpenChange={(open) => !open && setPushDialogItem(null)}>
        <DialogContent className="max-w-md" data-testid="push-to-sap-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">Push to SAP: {pushDialogItem?.product_id}</DialogTitle>
          </DialogHeader>
          {pushDialogLoading ? (
            <div className="flex items-center gap-2 text-sm text-[#475467] py-4" data-testid="push-to-sap-loading">
              <ArrowClockwise size={14} className="animate-spin" />
              Pulling current SAP values for comparison...
            </div>
          ) : pushDialogSapData?.error ? (
            <div className="text-sm text-[#B42318] py-2" data-testid="push-to-sap-error">
              Could not load SAP values: {pushDialogSapData.error}
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-2 text-sm py-2" data-testid="push-to-sap-comparison">
              <div className="font-heading text-xs font-bold text-[#667085] uppercase">Field</div>
              <div className="font-heading text-xs font-bold text-[#667085] uppercase">Current SAP</div>
              <div className="font-heading text-xs font-bold text-[#004B87] uppercase">Pushing</div>
              <div className="text-[#344054]">Safety Stock</div>
              <div className="tabular-nums text-[#667085]" data-testid="push-to-sap-current-safety-stock">
                {pushDialogSapData?.safety_stock ?? "—"} {pushDialogSapData?.unit_code || ""}
              </div>
              <div className="tabular-nums font-bold text-[#004B87]">{pushDialogItem?.msl ?? "—"}</div>
              <div className="text-[#344054]">Lead Time</div>
              <div className="tabular-nums text-[#667085]" data-testid="push-to-sap-current-lead-time">
                {pushDialogSapData?.lead_time_days ?? "—"} day(s)
              </div>
              <div className="tabular-nums font-bold text-[#004B87]">{pushDialogItem?.lead_time_days ?? "—"} day(s)</div>
            </div>
          )}
          <p className="text-xs text-[#667085] font-sans">
            Applies identically to all {pushDialogSapData?.planning_area_count || ""} Supply Planning Area(s) for this material in SAP.
          </p>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setPushDialogItem(null)}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="push-to-sap-cancel-button"
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={confirmPushToSap}
              disabled={pushing || pushDialogLoading || !!pushDialogSapData?.error}
              className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm"
              data-testid="push-to-sap-confirm-button"
            >
              <CloudArrowUp size={13} className="mr-1.5" />
              {pushing ? "Pushing..." : "Confirm Push"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!physicalDialogItem} onOpenChange={(open) => !open && setPhysicalDialogItem(null)}>
        <DialogContent className="max-w-lg" data-testid="weight-dims-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">Weight &amp; Surface Area: {physicalDialogItem?.product_id}</DialogTitle>
            <DialogDescription>View, save locally, and push Net Weight and Surface Area to SAP.</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={pullPhysicalFromSap}
              disabled={physicalSapLoading}
              className="h-7 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="weight-dims-pull-from-sap-button"
            >
              <ArrowClockwise size={12} className={`mr-1 ${physicalSapLoading ? "animate-spin" : ""}`} />
              {physicalSapLoading ? "Pulling..." : "Pull Current SAP Values"}
            </Button>
          </div>
          {physicalSapData?.error && (
            <div className="text-sm text-[#B42318] py-1" data-testid="weight-dims-sap-error">
              Could not load SAP values: {physicalSapData.error}
            </div>
          )}
          <div className="grid grid-cols-4 gap-x-2 gap-y-2 text-sm py-1" data-testid="weight-dims-form">
            <div className="font-heading text-xs font-bold text-[#667085] uppercase">Field</div>
            <div className="font-heading text-xs font-bold text-[#667085] uppercase">Current SAP</div>
            <div className="font-heading text-xs font-bold text-[#004B87] uppercase col-span-2">Local Value (this app)</div>
            {PHYSICAL_FIELDS.map(({ key, label, unit }) => (
              <Fragment key={key}>
                <div className="text-[#344054] self-center">{label}</div>
                <div className="tabular-nums text-[#667085] self-center" data-testid={`weight-dims-sap-${key}`}>
                  {physicalSapData?.attributes?.[key] != null ? `${physicalSapData.attributes[key].toFixed(3)} ${unit}` : "—"}
                </div>
                <input
                  type="number"
                  step="any"
                  placeholder="0"
                  value={physicalInputs[key] ?? ""}
                  onChange={(e) => setPhysicalInputs((prev) => ({ ...prev, [key]: e.target.value }))}
                  className="h-7 col-span-1 px-1.5 text-xs border border-[#D0D5DD] rounded-sm bg-white text-[#101828] tabular-nums focus:outline-none focus:ring-1 focus:ring-[#004B87]"
                  data-testid={`weight-dims-input-${key}`}
                />
                <div className="text-xs text-[#667085] self-center">{unit}</div>
              </Fragment>
            ))}
          </div>
          <p className="text-xs text-[#667085] font-sans">
            "Save Locally" stores values in this app only. "Push to SAP" writes the local values above into SAP's Material master (requires the "Manage Materials" service to be authorized on your SAP tenant).
          </p>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setPhysicalDialogItem(null)}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="weight-dims-cancel-button"
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={savePhysicalLocally}
              disabled={physicalSaving}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="weight-dims-save-locally-button"
            >
              {physicalSaving ? "Saving..." : "Save Locally"}
            </Button>
            <Button
              type="button"
              onClick={pushPhysicalToSap}
              disabled={physicalPushing}
              className="h-8 bg-[#004B87] hover:bg-[#003A6A] text-white text-xs rounded-sm"
              data-testid="weight-dims-push-to-sap-button"
            >
              <CloudArrowUp size={13} className="mr-1.5" />
              {physicalPushing ? "Pushing..." : "Push to SAP"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={pushAllOpen} onOpenChange={(open) => !open && pushAllStatus !== "running" && setPushAllOpen(false)}>
        <DialogContent className="max-w-md" data-testid="push-all-to-sap-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading text-base">Push All to SAP</DialogTitle>
          </DialogHeader>
          {pushAllStatus === "running" && (
            <div className="py-4 space-y-3" data-testid="push-all-running">
              <div className="flex items-center gap-2 text-sm text-[#475467]">
                <ArrowClockwise size={14} className="animate-spin" />
                Pushing {pushAllProgress.processed} of {pushAllProgress.total || "?"} component(s)...
              </div>
              {pushAllProgress.total > 0 && (
                <div className="w-full h-2 bg-[#EAECF0] rounded-full overflow-hidden">
                  <div
                    className="h-full bg-[#004B87] transition-all duration-300"
                    style={{ width: `${(pushAllProgress.processed / pushAllProgress.total) * 100}%` }}
                  />
                </div>
              )}
              <p className="text-xs text-[#98A2B3]">This can take a while against the live SAP tenant - feel free to leave this open.</p>
            </div>
          )}
          {pushAllStatus === "done" && pushAllResult && (
            <div className="py-2 space-y-2" data-testid="push-all-result">
              <p className="text-sm text-[#101828]">
                Pushed <span className="font-bold">{pushAllResult.pushed}</span> of{" "}
                <span className="font-bold">{pushAllResult.total}</span> component(s) successfully.
              </p>
              {pushAllResult.failed.length > 0 && (
                <div>
                  <p className="text-xs text-[#B42318] font-bold mb-1">{pushAllResult.failed.length} failed:</p>
                  <div className="max-h-40 overflow-auto border border-[#FDA29B] rounded-sm bg-[#FEF3F2] p-2 space-y-1" data-testid="push-all-failed-list">
                    {pushAllResult.failed.map((f) => (
                      <div key={f.product_id} className="text-xs text-[#912018]">
                        <span className="font-bold">{f.product_id}</span>: {f.error}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {pushAllResult.total === 0 && (
                <p className="text-xs text-[#98A2B3]">No components had both a SAP link and an MSL/Lead Time value set.</p>
              )}
            </div>
          )}
          {pushAllStatus === "failed" && (
            <div className="py-2 text-sm text-[#B42318]" data-testid="push-all-error">
              {pushAllError}
            </div>
          )}
          <div className="flex justify-end">
            <Button
              type="button"
              variant="outline"
              onClick={() => setPushAllOpen(false)}
              disabled={pushAllStatus === "running"}
              className="h-8 text-xs rounded-sm border-[#D0D5DD] text-[#344054]"
              data-testid="push-all-close-button"
            >
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
