import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Buildings, SignOut, SquaresFour, Truck, FileText, ShieldWarning, List, X,
} from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

const NAV_ITEMS = [
  { key: "dashboard", label: "Dashboard", icon: SquaresFour, path: (v) => `/supplier-portal/dashboard/${v}` },
  { key: "shipments", label: "Shipments", icon: Truck, path: (v) => `/supplier-portal/shipments/${v}` },
  { key: "documents", label: "Documents", icon: FileText, path: (v) => `/supplier-portal/documents/${v}` },
  { key: "audits", label: "Audits & QC", icon: ShieldWarning, path: (v) => `/supplier-portal/audits-qc/${v}`, badge: "QMS" },
];

// Swiss High-Contrast redesign (design_guidelines.json) - persistent left
// sidebar shared shell for every /supplier-portal/* page. Structural/visual
// only - each page keeps its own data-fetching and functional logic.
export function SupplierPortalLayout({ active, vendorCode, isImpersonating, pageTitle, pageSubtitle, children }) {
  const { account, logout } = useSupplierAuth();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);

  const handleLogout = async () => { await logout(); navigate("/supplier-portal/login"); };

  const sidebarContent = (
    <div className="flex flex-col h-full">
      <div className="h-16 flex items-center gap-2 px-5 border-b border-white/10 shrink-0">
        <Buildings size={20} weight="fill" className="text-white" />
        <span className="font-heading font-bold tracking-tight text-white text-[15px]">Vendor Portal</span>
      </div>
      <nav className="flex-1 py-4 px-3 space-y-1" data-testid="supplier-sidebar-nav">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const isActive = active === item.key;
          return (
            <Link
              key={item.key}
              to={item.path(vendorCode)}
              onClick={() => setMobileOpen(false)}
              data-testid={`nav-${item.key === "audits" ? "audits-qc" : item.key}`}
              className={`flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors duration-150 ${
                isActive ? "bg-[#1E40AF] text-white shadow-sm" : "text-slate-300 hover:bg-white/10 hover:text-white"
              }`}
            >
              <Icon size={17} weight={isActive ? "fill" : "regular"} />
              <span className="flex-1">{item.label}</span>
              {item.badge && (
                <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-400/90 text-slate-900">{item.badge}</span>
              )}
            </Link>
          );
        })}
      </nav>
      <div className="p-3 border-t border-white/10">
        <Button
          onClick={handleLogout}
          variant="outline"
          size="sm"
          className="w-full rounded-lg border-white/15 bg-transparent text-slate-300 hover:bg-white/10 hover:text-white transition-colors duration-150"
          data-testid="supplier-sidebar-logout-button"
        >
          <SignOut size={14} className="mr-1.5" /> Sign Out
        </Button>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen bg-[#F8FAFC] font-sans flex" data-testid="supplier-portal-layout">
      <aside className="hidden md:flex md:w-60 bg-[#0F172A] shrink-0 fixed inset-y-0 left-0 z-30">{sidebarContent}</aside>

      {mobileOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setMobileOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-60 bg-[#0F172A]">{sidebarContent}</aside>
        </div>
      )}

      <div className="flex-1 md:ml-60 min-w-0">
        <header className="h-16 bg-white border-b border-[#E2E8F0] px-4 md:px-6 flex items-center justify-between gap-3 sticky top-0 z-20">
          <div className="flex items-center gap-3 min-w-0">
            <button
              onClick={() => setMobileOpen(true)}
              className="md:hidden text-slate-600 shrink-0"
              data-testid="supplier-mobile-nav-toggle"
            >
              <List size={22} />
            </button>
            <div className="min-w-0">
              <h1 className="font-heading text-lg md:text-xl font-bold text-[#0F172A] truncate">{pageTitle}</h1>
              {pageSubtitle && <p className="text-xs text-[#475569] font-data truncate">{pageSubtitle}</p>}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0" data-testid="supplier-vendor-badge">
            <div className="text-right hidden sm:block bg-[#EEF2FF] border border-[#BFDBFE] rounded-lg px-3 py-1.5">
              <div className="text-xs font-bold text-[#1E40AF] font-data">{vendorCode}</div>
              <div className="text-[11px] text-[#475569] truncate max-w-[160px]">{isImpersonating ? `Viewing: ${vendorCode}` : account?.company_name}</div>
            </div>
          </div>
        </header>
        <main className="p-4 md:p-6 pb-24">{children}</main>
      </div>
    </div>
  );
}
