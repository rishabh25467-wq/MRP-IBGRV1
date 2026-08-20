import { useState } from "react";
import "@/App.css";
import axios from "axios";
import { Shield, LockSimple, WarningCircle, CheckCircle, Plus } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

// Same lightweight session-scoped passcode gate as SapWritePage.js - this
// creates a real, permanent Material master record in live SAP.
const PASSCODE = "admin";
const SESSION_KEY = "create_material_unlocked";

const COMMON_UOM_CODES = ["EA", "KGM", "MTR", "LTR", "PC", "SET", "BOX", "TO"];

const inputCls =
  "h-9 px-3 text-sm rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87] w-full";

export default function CreateMaterialPage() {
  const [unlocked, setUnlocked] = useState(() => sessionStorage.getItem(SESSION_KEY) === "true");
  const [passcodeInput, setPasscodeInput] = useState("");
  const [passcodeError, setPasscodeError] = useState(null);

  const [materialId, setMaterialId] = useState("");
  const [productCategoryId, setProductCategoryId] = useState("");
  const [baseUom, setBaseUom] = useState("EA");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  const [created, setCreated] = useState(null);
  const [undoing, setUndoing] = useState(false);

  const unlock = (e) => {
    e.preventDefault();
    if (passcodeInput === PASSCODE) {
      sessionStorage.setItem(SESSION_KEY, "true");
      setUnlocked(true);
      setPasscodeError(null);
    } else {
      setPasscodeError("Incorrect passcode");
    }
  };

  const canSubmit = materialId.trim() && productCategoryId.trim() && baseUom.trim() && description.trim() && !creating;

  const createMaterial = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setCreating(true);
    setCreateError(null);
    setCreated(null);
    try {
      const { data } = await axios.post(`${API}/admin/create-material`, {
        material_id: materialId.trim(),
        product_category_id: productCategoryId.trim(),
        base_uom: baseUom.trim(),
        description: description.trim(),
      });
      setCreated(data);
      toast.success("Material created in SAP", { description: `${data.material_id} (${data.uuid})` });
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Failed to create material";
      setCreateError(detail);
      toast.error("SAP rejected the material", { description: detail });
    } finally {
      setCreating(false);
    }
  };

  const undoCreate = async () => {
    if (!created) return;
    setUndoing(true);
    try {
      await axios.post(`${API}/admin/delete-material`, { material_id: created.material_id });
      toast.success("Deleted from SAP", { description: `${created.material_id} removed - it was still In Preparation` });
      setCreated(null);
      setMaterialId("");
      setProductCategoryId("");
      setDescription("");
    } catch (err) {
      toast.error("Could not delete", { description: err?.response?.data?.detail || err.message });
    } finally {
      setUndoing(false);
    }
  };

  if (!unlocked) {
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
              <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Create Material</span>
            </div>
          </div>
          <div className="w-px h-7 bg-white/25 shrink-0" />
          <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
            <NavTabs />
          </div>
          <div className="shrink-0 w-8" />
        </header>
        <main className="flex-1 flex items-center justify-center">
          <form
            onSubmit={unlock}
            className="bg-white border border-[#D0D5DD] rounded-sm p-6 w-full max-w-sm flex flex-col items-center gap-3 shadow-[0_1px_3px_0_rgba(16,24,40,0.1)]"
            data-testid="create-material-passcode-form"
          >
            <div className="w-10 h-10 rounded-full bg-[#FFFAEB] flex items-center justify-center">
              <LockSimple size={18} weight="bold" className="text-[#B54708]" />
            </div>
            <h1 className="font-heading text-sm font-bold text-[#1D2939]">Restricted Area</h1>
            <p className="text-[13px] text-[#475467] text-center">
              This page creates a new, permanent Material record in live SAP. Enter the admin passcode to continue.
            </p>
            <input
              type="password"
              autoFocus
              placeholder="Passcode"
              value={passcodeInput}
              onChange={(e) => { setPasscodeInput(e.target.value); setPasscodeError(null); }}
              className={inputCls}
              data-testid="create-material-passcode-input"
            />
            {passcodeError && (
              <p className="text-xs text-[#B42318]" data-testid="create-material-passcode-error">{passcodeError}</p>
            )}
            <Button type="submit" className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm" data-testid="create-material-passcode-submit">
              Unlock
            </Button>
          </form>
        </main>
      </div>
    );
  }

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
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Create Material</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <div className="shrink-0 w-8" />
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4 max-w-xl mx-auto w-full">
        <div className="bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-3 flex items-start gap-2.5" data-testid="create-material-warning-banner">
          <WarningCircle size={16} weight="fill" className="text-[#B54708] mt-0.5 shrink-0" />
          <div className="text-[13px] text-[#7A4504]">
            <span className="font-bold">Careful: </span>
            this creates a real, permanent Material ID in SAP (checked first that the ID doesn't already exist).
            It's created "In Preparation" only - not yet usable for purchasing, sales, or production until someone
            activates it further in SAP. Ask your SAP admin for the correct Product Category code for this material type.
          </div>
        </div>

        <form onSubmit={createMaterial} className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3" data-testid="create-material-form">
          <div>
            <Label className="text-xs font-bold text-[#344054]">Material ID</Label>
            <input
              value={materialId}
              onChange={(e) => setMaterialId(e.target.value)}
              placeholder="e.g. RAW-STEEL-05"
              className={inputCls}
              data-testid="create-material-id-input"
            />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Product Category ID</Label>
            <input
              value={productCategoryId}
              onChange={(e) => setProductCategoryId(e.target.value)}
              placeholder="e.g. SFG, RAW - ask your SAP admin for the exact code"
              className={inputCls}
              data-testid="create-material-category-input"
            />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Base UoM</Label>
            <Select value={baseUom} onValueChange={setBaseUom}>
              <SelectTrigger data-testid="create-material-uom-select-trigger">
                <SelectValue placeholder="EA" />
              </SelectTrigger>
              <SelectContent>
                {COMMON_UOM_CODES.map((code) => (
                  <SelectItem key={code} value={code} data-testid={`create-material-uom-option-${code}`}>{code}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Description (English)</Label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="e.g. Steel Rod 5mm"
              className={inputCls}
              data-testid="create-material-description-input"
            />
          </div>
          {createError && (
            <p className="text-xs text-[#B42318]" data-testid="create-material-error">{createError}</p>
          )}
          <Button
            type="submit"
            disabled={!canSubmit}
            className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm flex items-center justify-center gap-1.5"
            data-testid="create-material-submit-button"
          >
            <Plus size={14} weight="bold" />
            {creating ? "Creating in SAP..." : "Create Material in SAP"}
          </Button>
        </form>

        {created && (
          <div className="bg-[#ECFDF3] border border-[#ABEFC6] rounded-sm p-4 space-y-2" data-testid="create-material-success-card">
            <div className="flex items-center gap-2 text-[#027A48] font-bold text-sm">
              <CheckCircle size={16} weight="fill" />
              Created in SAP
            </div>
            <div className="text-[13px] text-[#065F46]">
              Material ID: <span className="font-mono font-bold" data-testid="created-material-id">{created.material_id}</span>
            </div>
            <div className="text-[13px] text-[#065F46]">
              UUID: <span className="font-mono" data-testid="created-material-uuid">{created.uuid}</span>
            </div>
            <Button
              variant="outline"
              onClick={undoCreate}
              disabled={undoing}
              className="h-8 text-xs border-[#FECDCA] text-[#B42318] hover:bg-[#FEF3F2]"
              data-testid="create-material-undo-button"
            >
              {undoing ? "Deleting..." : "Undo (delete this material)"}
            </Button>
          </div>
        )}
      </main>
    </div>
  );
}
