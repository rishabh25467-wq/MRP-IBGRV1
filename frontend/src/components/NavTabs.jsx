import { Link, useLocation } from "react-router-dom";
import { CaretDown, UserCircle, SignOut, ShieldCheck } from "@phosphor-icons/react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/contexts/AuthContext";

// Labels follow global MRP/ERP naming conventions (SAP/Oracle/Infor-style
// module names) - renamed Feb 2026 as part of the "Materials Hub" rebrand.
// Routes are unchanged; only the displayed label changed. `page` matches
// backend/auth_service.py's PAGE_CATALOG keys - a tab only renders if the
// signed-in user has that page.
const TABS = [
  { to: "/", label: "BOM Management", testId: "nav-bom-explorer", page: "bom_explorer" },
  { to: "/purchasing-plan", label: "Procurement Planning", testId: "nav-purchasing-plan", page: "purchasing_plan" },
  { to: "/production-plan", label: "Production Planning", testId: "nav-production-plan", page: "production_plan" },
  { to: "/inventory", label: "Inventory Management", testId: "nav-inventory", page: "inventory" },
];

const PURCHASING_STRATEGY_SUBTABS = [
  { to: "/purchasing-strategy/supplier-master", label: "Supplier Master", testId: "nav-purchasing-strategy-supplier-master", page: "supplier_master" },
  { to: "/purchasing-strategy/quota-allocation", label: "Quota Allocation", testId: "nav-purchasing-strategy-quota-allocation", page: "quota_allocation" },
];

const ADMIN_SUBTABS = [
  { to: "/admin", label: "Component Master", testId: "nav-admin-component-master", page: "admin" },
  { to: "/admin/sap-write", label: "SAP Write", testId: "nav-admin-sap-write", page: "admin_sap_write" },
];

export const NavTabs = () => {
  const { pathname } = useLocation();
  const { user, hasPageAccess, logout } = useAuth();
  const visibleTabs = TABS.filter((t) => hasPageAccess(t.page));
  const visiblePurchasingStrategySubtabs = PURCHASING_STRATEGY_SUBTABS.filter((t) => hasPageAccess(t.page));
  const visibleAdminSubtabs = ADMIN_SUBTABS.filter((t) => hasPageAccess(t.page));
  const adminActive = ADMIN_SUBTABS.some((t) => t.to === pathname);
  const purchasingStrategyActive = PURCHASING_STRATEGY_SUBTABS.some((t) => t.to === pathname);
  return (
    <div className="flex items-center gap-1 w-full">
      {visibleTabs.map((tab) => {
        const active = pathname === tab.to;
        return (
          <Link
            key={tab.to}
            to={tab.to}
            className={`px-3.5 py-1.5 rounded-full text-[13px] font-bold font-heading transition-colors duration-150 ${
              active ? "bg-white text-[#0B6B74]" : "text-white/85 hover:bg-white/15 hover:text-white"
            }`}
            data-testid={tab.testId}
          >
            {tab.label}
          </Link>
        );
      })}
      {visiblePurchasingStrategySubtabs.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger
            className={`flex items-center gap-1 px-3.5 py-1.5 rounded-full text-[13px] font-bold font-heading transition-colors duration-150 outline-none ${
              purchasingStrategyActive ? "bg-white text-[#0B6B74]" : "text-white/85 hover:bg-white/15 hover:text-white"
            }`}
            data-testid="nav-purchasing-strategy"
          >
            Supplier Management
            <CaretDown size={10} weight="bold" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[180px] bg-white border border-[#D0D5DD]" data-testid="nav-purchasing-strategy-dropdown-content">
            {visiblePurchasingStrategySubtabs.map((tab) => (
              <DropdownMenuItem key={tab.to} asChild>
                <Link to={tab.to} className="w-full cursor-pointer" data-testid={tab.testId}>
                  {tab.label}
                </Link>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      {visibleAdminSubtabs.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger
            className={`flex items-center gap-1 px-3.5 py-1.5 rounded-full text-[13px] font-bold font-heading transition-colors duration-150 outline-none ${
              adminActive ? "bg-white text-[#0B6B74]" : "text-white/85 hover:bg-white/15 hover:text-white"
            }`}
            data-testid="nav-admin"
          >
            Master Data
            <CaretDown size={10} weight="bold" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[180px] bg-white border border-[#D0D5DD]" data-testid="nav-admin-dropdown-content">
            {visibleAdminSubtabs.map((tab) => (
              <DropdownMenuItem key={tab.to} asChild>
                <Link to={tab.to} className="w-full cursor-pointer" data-testid={tab.testId}>
                  {tab.label}
                </Link>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      {user && (
        <DropdownMenu>
          <DropdownMenuTrigger
            className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[13px] font-bold font-heading text-white/85 hover:bg-white/15 hover:text-white transition-colors duration-150 outline-none shrink-0"
            data-testid="nav-user-menu"
          >
            <UserCircle size={16} weight="fill" />
            {user.name || user.email}
            <CaretDown size={10} weight="bold" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[220px] bg-white border border-[#D0D5DD]" data-testid="nav-user-menu-dropdown-content">
            <div className="px-3 py-2 text-xs text-[#667085] truncate">{user.email}</div>
            <DropdownMenuSeparator />
            {user.role === "super_admin" && (
              <DropdownMenuItem asChild>
                <Link to="/admin/access-management" className="w-full cursor-pointer flex items-center gap-2" data-testid="nav-access-management">
                  <ShieldCheck size={14} weight="bold" />
                  Access Management
                </Link>
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onClick={logout} className="cursor-pointer flex items-center gap-2 text-[#B42318]" data-testid="nav-sign-out">
              <SignOut size={14} weight="bold" />
              Sign Out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
};
