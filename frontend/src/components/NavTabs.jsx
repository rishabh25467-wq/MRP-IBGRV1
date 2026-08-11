import { Link, useLocation } from "react-router-dom";
import { CaretDown } from "@phosphor-icons/react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

// Labels follow global MRP/ERP naming conventions (SAP/Oracle/Infor-style
// module names) - renamed Feb 2026 as part of the "Materials Hub" rebrand.
// Routes are unchanged; only the displayed label changed.
const TABS = [
  { to: "/", label: "BOM Management", testId: "nav-bom-explorer" },
  { to: "/purchasing-plan", label: "Procurement Planning", testId: "nav-purchasing-plan" },
  { to: "/production-plan", label: "Production Planning", testId: "nav-production-plan" },
  { to: "/inventory", label: "Inventory Management", testId: "nav-inventory" },
];

const PURCHASING_STRATEGY_SUBTABS = [
  { to: "/purchasing-strategy/supplier-master", label: "Supplier Master", testId: "nav-purchasing-strategy-supplier-master" },
  { to: "/purchasing-strategy/quota-allocation", label: "Quota Allocation", testId: "nav-purchasing-strategy-quota-allocation" },
];

const ADMIN_SUBTABS = [
  { to: "/admin", label: "Component Master", testId: "nav-admin-component-master" },
  { to: "/admin/sap-write", label: "SAP Write", testId: "nav-admin-sap-write" },
];

export const NavTabs = () => {
  const { pathname } = useLocation();
  const adminActive = ADMIN_SUBTABS.some((t) => t.to === pathname);
  const purchasingStrategyActive = PURCHASING_STRATEGY_SUBTABS.some((t) => t.to === pathname);
  return (
    <div className="flex items-center gap-1">
      {TABS.map((tab) => {
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
          {PURCHASING_STRATEGY_SUBTABS.map((tab) => (
            <DropdownMenuItem key={tab.to} asChild>
              <Link to={tab.to} className="w-full cursor-pointer" data-testid={tab.testId}>
                {tab.label}
              </Link>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
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
          {ADMIN_SUBTABS.map((tab) => (
            <DropdownMenuItem key={tab.to} asChild>
              <Link to={tab.to} className="w-full cursor-pointer" data-testid={tab.testId}>
                {tab.label}
              </Link>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
};
