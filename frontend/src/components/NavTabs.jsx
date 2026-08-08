import { Link, useLocation } from "react-router-dom";
import { CaretDown } from "@phosphor-icons/react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const TABS = [
  { to: "/", label: "BOM Explorer", testId: "nav-bom-explorer" },
  { to: "/purchasing-plan", label: "Purchasing Plan", testId: "nav-purchasing-plan" },
  { to: "/production-plan", label: "Production Plan", testId: "nav-production-plan" },
  { to: "/inventory", label: "Inventory", testId: "nav-inventory" },
  { to: "/suppliers", label: "Suppliers", testId: "nav-suppliers" },
];

const ADMIN_SUBTABS = [
  { to: "/admin", label: "Component Master", testId: "nav-admin-component-master" },
  { to: "/admin/sap-write", label: "SAP Write", testId: "nav-admin-sap-write" },
];

export const NavTabs = () => {
  const { pathname } = useLocation();
  const adminActive = ADMIN_SUBTABS.some((t) => t.to === pathname);
  return (
    <div className="flex items-center gap-1">
      {TABS.map((tab) => {
        const active = pathname === tab.to;
        return (
          <Link
            key={tab.to}
            to={tab.to}
            className={`px-3 py-1 rounded-sm text-xs font-bold font-heading transition-colors ${
              active ? "bg-white/20 text-white" : "text-white/70 hover:bg-white/10 hover:text-white"
            }`}
            data-testid={tab.testId}
          >
            {tab.label}
          </Link>
        );
      })}
      <DropdownMenu>
        <DropdownMenuTrigger
          className={`flex items-center gap-1 px-3 py-1 rounded-sm text-xs font-bold font-heading transition-colors outline-none ${
            adminActive ? "bg-white/20 text-white" : "text-white/70 hover:bg-white/10 hover:text-white"
          }`}
          data-testid="nav-admin"
        >
          Admin
          <CaretDown size={10} weight="bold" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-[180px]" data-testid="nav-admin-dropdown-content">
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
