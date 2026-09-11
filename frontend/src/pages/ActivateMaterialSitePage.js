import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { Shield, LockSimple, WarningCircle, CheckCircle, XCircle, MagnifyingGlass } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

// Same lightweight session-scoped passcode gate as CreateMaterialPage.js -
// this writes a real, permanent change to an existing material in live SAP.
const PASSCODE = "admin";
const SESSION_KEY = "activate_material_site_unlocked";

const inputCls =
  "h-9 px-3 text-sm rounded-sm border border-[#D0D5DD] text-[#101828] focus:outline-none focus:border-[#004B87] focus:ring-1 focus:ring-[#004B87] w-full";

// Sep 11 2026, user's explicit ask: "activate material with all the tabs
// including valuation". Planning/Logistics/Availability Confirmation are
// committed by SAP as ONE atomic bundle (confirmed live, sap_material_
// create_client.py) - shown as a single combined result. Valuation is a
// separate SOAP call with its own prerequisite (an Account Determination
// Group set up in SAP Business Configuration) - shown separately so a
// Valuation-only failure is never confused with the other 3 tabs failing.
const RESULT_ROWS = [
  { key: "planning_logistics", label: "Planning / Logistics / Availability Confirmation" },
  { key: "valuation", label: "Valuation" },
];

export default function ActivateMaterialSitePage() {
  const [unlocked, setUnlocked] = useState(() => sessionStorage.getItem(SESSION_KEY) === "true");
  const [passcodeInput, setPasscodeInput] = useState("");
  const [passcodeError, setPasscodeError] = useState(null);

  const [materialId, setMaterialId] = useState("");
  const [looking, setLooking] = useState(false);
  const [material, setMaterial] = useState(null);
  const [lookupError, setLookupError] = useState(null);

  const [sites, setSites] = useState([]);
  const [siteId, setSiteId] = useState("");
  const [activating, setActivating] = useState(false);
  const [result, setResult] = useState(null);

  useEffect(() => {
    axios.get(`${API}/purchase-orders/sites`).then(({ data }) => setSites(data.sites || [])).catch(() => {});
  }, []);

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

  const lookup = async (e) => {
    e.preventDefault();
    if (!materialId.trim()) return;
    setLooking(true);
    setLookupError(null);
    setMaterial(null);
    setResult(null);
    setSiteId("");
    try {
      const { data } = await axios.get(`${API}/admin/material-sites/lookup/${encodeURIComponent(materialId.trim())}`);
      setMaterial(data);
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Lookup failed";
      setLookupError(detail);
      toast.error("Could not find material", { description: detail });
    } finally {
      setLooking(false);
    }
  };

  const activate = async () => {
    if (!material || !siteId) return;
    setActivating(true);
    setResult(null);
    try {
      const { data } = await axios.post(`${API}/admin/material-sites/activate`, {
        product_id: materialId.trim(),
        site_id: siteId,
      });
      setResult(data);
      if (data.planning_logistics === "ok" && data.valuation === "ok") {
        toast.success(`${materialId.trim()} activated at ${siteId}`, { description: "Planning/Logistics/Availability + Valuation all confirmed in SAP" });
      } else if (data.planning_logistics === "ok") {
        toast.warning(`${materialId.trim()} partially activated at ${siteId}`, { description: "Planning/Logistics/Availability confirmed - Valuation needs attention (see below)" });
      } else {
        toast.error("SAP rejected the activation", { description: "See the result below for details" });
      }
    } catch (err) {
      toast.error("Could not activate", { description: err?.response?.data?.detail || err.message });
    } finally {
      setActivating(false);
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
              <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Activate Material Site</span>
            </div>
          </div>
          <div className="w-px h-7 bg-white/25 shrink-0" />
          <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
            <NavTabs />
          </div>
          <SapConnectionStatus />
          <ErpConnectionStatus />
        </header>
        <main className="flex-1 flex items-center justify-center">
          <form
            onSubmit={unlock}
            className="bg-white border border-[#D0D5DD] rounded-sm p-6 w-full max-w-sm flex flex-col items-center gap-3 shadow-[0_1px_3px_0_rgba(16,24,40,0.1)]"
            data-testid="activate-material-site-passcode-form"
          >
            <div className="w-10 h-10 rounded-full bg-[#FFFAEB] flex items-center justify-center">
              <LockSimple size={18} weight="bold" className="text-[#B54708]" />
            </div>
            <h1 className="font-heading text-sm font-bold text-[#1D2939]">Restricted Area</h1>
            <p className="text-[13px] text-[#475467] text-center">
              This page activates an existing Material at a new site in live SAP. Enter the admin passcode to continue.
            </p>
            <input
              type="password"
              autoFocus
              placeholder="Passcode"
              value={passcodeInput}
              onChange={(e) => { setPasscodeInput(e.target.value); setPasscodeError(null); }}
              className={inputCls}
              data-testid="activate-material-site-passcode-input"
            />
            {passcodeError && (
              <p className="text-xs text-[#B42318]" data-testid="activate-material-site-passcode-error">{passcodeError}</p>
            )}
            <Button type="submit" className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm" data-testid="activate-material-site-passcode-submit">
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
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Activate Material Site</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <div className="shrink-0 w-8" />
      </header>

      <main className="flex-1 overflow-auto p-4 space-y-4 max-w-xl mx-auto w-full">
        <div className="bg-[#FFFAEB] border border-[#FEDF89] rounded-sm p-3 flex items-start gap-2.5" data-testid="activate-material-site-warning-banner">
          <WarningCircle size={16} weight="fill" className="text-[#B54708] mt-0.5 shrink-0" />
          <div className="text-[13px] text-[#7A4504]">
            <span className="font-bold">Careful: </span>
            this makes a real, permanent change to an existing Material in SAP. Planning/Logistics/Availability
            Confirmation activate reliably; Valuation ALSO requires an Account Determination Group already set up
            in SAP Business Configuration for that Company + Product Category - if that hasn't been done yet,
            Valuation will show SAP's own rejection reason below instead of silently failing.
          </div>
        </div>

        <form onSubmit={lookup} className="bg-white border border-[#D0D5DD] rounded-sm p-4 flex items-end gap-2" data-testid="activate-material-site-lookup-form">
          <div className="flex-1">
            <Label className="text-xs font-bold text-[#344054]">Material ID</Label>
            <input
              value={materialId}
              onChange={(e) => setMaterialId(e.target.value)}
              placeholder="e.g. PALL-373518"
              className={inputCls}
              data-testid="activate-material-site-id-input"
            />
          </div>
          <Button type="submit" disabled={!materialId.trim() || looking} className="h-9 bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm flex items-center gap-1.5" data-testid="activate-material-site-lookup-button">
            <MagnifyingGlass size={14} weight="bold" />
            {looking ? "Looking up..." : "Lookup"}
          </Button>
        </form>

        {lookupError && (
          <p className="text-xs text-[#B42318]" data-testid="activate-material-site-lookup-error">{lookupError}</p>
        )}

        {material && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3" data-testid="activate-material-site-card">
            <div>
              <div className="font-bold text-sm" data-testid="activate-material-site-description">{materialId.trim()} - {material.description || "(no description)"}</div>
              <div className="text-xs text-[#475467] mt-1">
                Category: <span className="font-mono">{material.product_category_id || "unknown"}</span> · Base UoM: <span className="font-mono">{material.base_unit || "-"}</span>
              </div>
              <div className="text-xs text-[#475467] mt-1" data-testid="activate-material-site-active-sites">
                Already active at: {material.active_sites.length ? material.active_sites.join(", ") : "no sites yet"}
              </div>
            </div>

            <div>
              <Label className="text-xs font-bold text-[#344054]">Site to activate</Label>
              <Select value={siteId} onValueChange={(v) => { setSiteId(v); setResult(null); }}>
                <SelectTrigger data-testid="activate-material-site-select-trigger">
                  <SelectValue placeholder="Select a site" />
                </SelectTrigger>
                <SelectContent>
                  {sites.map((s) => (
                    <SelectItem key={s} value={s} data-testid={`activate-material-site-option-${s}`}>
                      {s}{material.active_sites.includes(s) ? " (already active)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <Button
              onClick={activate}
              disabled={!siteId || activating}
              className="h-9 w-full bg-[#004B87] hover:bg-[#003A6A] text-white text-sm rounded-sm"
              data-testid="activate-material-site-submit-button"
            >
              {activating ? "Activating in SAP..." : `Activate ${materialId.trim()} at ${siteId || "..."}`}
            </Button>

            {result && (
              <div className="border-t border-[#EAECF0] pt-3 space-y-1.5" data-testid="activate-material-site-result">
                {RESULT_ROWS.map((row) => {
                  const value = result[row.key];
                  const ok = value === "ok";
                  return (
                    <div key={row.key} className="flex items-start gap-2 text-[13px]" data-testid={`activate-material-site-result-${row.key}`}>
                      {ok ? (
                        <CheckCircle size={16} weight="fill" className="text-[#12B76A] mt-0.5 shrink-0" />
                      ) : (
                        <XCircle size={16} weight="fill" className="text-[#F04438] mt-0.5 shrink-0" />
                      )}
                      <div>
                        <span className="font-semibold">{row.label}:</span>{" "}
                        {ok ? <span className="text-[#027A48]">Confirmed active in SAP</span> : <span className="text-[#B42318]">{value}</span>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
