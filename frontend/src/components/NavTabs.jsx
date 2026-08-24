import { Link, useLocation } from "react-router-dom";
import { CaretDown, UserCircle, SignOut, ShieldCheck, List } from "@phosphor-icons/react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetTrigger,
  SheetClose,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Accordion,
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
} from "@/components/ui/accordion";
import { useAuth } from "@/contexts/AuthContext";

// Labels follow global MRP/ERP naming conventions (SAP/Oracle/Infor-style
// module names) - renamed Feb 2026 as part of the "Materials Hub" rebrand.
// Routes are unchanged; only the displayed label changed. `page` matches
// backend/auth_service.py's PAGE_CATALOG keys - a tab only renders if the
// signed-in user has that page.
const TABS = [
  { to: "/", label: "BOM Management", testId: "nav-bom-explorer", page: "bom_explorer" },
  { to: "/purchasing-plan", label: "Procurement Planning", testId: "nav-purchasing-plan", page: "purchasing_plan" },
  { to: "/production-confirmation", label: "Production Confirmation", testId: "nav-production-confirmation", page: "production_confirmation" },
];

const INVENTORY_SUBTABS = [
  { to: "/inventory", label: "Stock Overview", testId: "nav-inventory-stock-overview", page: "inventory" },
  { to: "/inventory/inter-plant-transfer", label: "Inter Plant Stock Transfer", testId: "nav-inventory-stock-transfer", page: "inventory" },
  // Aug 2026, user's explicit ask: links out to the existing Store
  // Approval screen (public/unauthenticated route, unchanged) - just a
  // shortcut into it from the main nav, no backend permission change.
  { to: "/storeapproval", label: "Goods Issue", testId: "nav-inventory-goods-issue", page: "store_approval" },
];

const PURCHASING_STRATEGY_SUBTABS = [
  { to: "/purchasing-strategy/supplier-master", label: "Supplier Master", testId: "nav-purchasing-strategy-supplier-master", page: "supplier_master" },
  { to: "/purchasing-strategy/quota-allocation", label: "Quota Allocation", testId: "nav-purchasing-strategy-quota-allocation", page: "quota_allocation" },
];

const ADMIN_SUBTABS = [
  { to: "/admin", label: "Component Master", testId: "nav-admin-component-master", page: "admin" },
  { to: "/admin/sap-write", label: "SAP Write", testId: "nav-admin-sap-write", page: "admin_sap_write" },
  { to: "/admin/l1-l2-report", label: "L1/L2 Item Report", testId: "nav-admin-l1l2-report", page: "admin" },
  { to: "/admin/create-material", label: "Create Material", testId: "nav-admin-create-material", page: "admin_create_material" },
];

export const NavTabs = () => {
  const { pathname } = useLocation();
  const { user, hasPageAccess, logout } = useAuth();
  const visibleTabs = TABS.filter((t) => hasPageAccess(t.page));
  const visiblePurchasingStrategySubtabs = PURCHASING_STRATEGY_SUBTABS.filter((t) => hasPageAccess(t.page));
  const visibleInventorySubtabs = INVENTORY_SUBTABS.filter((t) => hasPageAccess(t.page));
  const visibleAdminSubtabs = ADMIN_SUBTABS.filter((t) => hasPageAccess(t.page));
  const adminActive = ADMIN_SUBTABS.some((t) => t.to === pathname);
  const purchasingStrategyActive = PURCHASING_STRATEGY_SUBTABS.some((t) => t.to === pathname);
  const inventoryActive = pathname === "/inventory" || pathname === "/inventory/inter-plant-transfer" || pathname.startsWith("/storeapproval");
  return (
    <>
      {/* Desktop / large tablet nav - unchanged pill tabs */}
      <div className="hidden lg:flex items-center gap-1 w-full">
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
      {visibleInventorySubtabs.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger
            className={`flex items-center gap-1 px-3.5 py-1.5 rounded-full text-[13px] font-bold font-heading transition-colors duration-150 outline-none ${
              inventoryActive ? "bg-white text-[#0B6B74]" : "text-white/85 hover:bg-white/15 hover:text-white"
            }`}
            data-testid="nav-inventory"
          >
            Inventory Management
            <CaretDown size={10} weight="bold" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[180px] bg-white border border-[#D0D5DD]" data-testid="nav-inventory-dropdown-content">
            {visibleInventorySubtabs.map((tab) => (
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
            {(user.role === "super_admin" || user.role === "admin") && (
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

      {/* Mobile / tablet nav - hamburger trigger + off-canvas drawer */}
      <div className="flex lg:hidden items-center ml-auto">
        <Sheet>
          <SheetTrigger asChild>
            <button
              type="button"
              className="w-11 h-11 flex items-center justify-center rounded-lg text-white hover:bg-white/15 transition-colors duration-150 shrink-0"
              data-testid="mobile-menu-trigger"
              aria-label="Open menu"
            >
              <List size={22} weight="bold" />
            </button>
          </SheetTrigger>
          <SheetContent
            side="left"
            className="w-[85vw] max-w-sm p-0 flex flex-col bg-white z-[60]"
            data-testid="mobile-nav-drawer"
          >
            <SheetHeader className="px-4 pt-5 pb-3 border-b border-[#EAECF0] text-left">
              <SheetTitle className="text-[#0E7C86] font-heading">Materials Hub</SheetTitle>
            </SheetHeader>

            <div className="flex-1 overflow-y-auto px-2 py-2">
              {visibleTabs.map((tab) => {
                const active = pathname === tab.to;
                return (
                  <SheetClose asChild key={tab.to}>
                    <Link
                      to={tab.to}
                      className={`flex items-center min-h-11 px-3 rounded-lg text-[14px] font-bold font-heading transition-colors duration-150 ${
                        active ? "bg-[#0E7C86]/10 text-[#0B6B74]" : "text-[#344054] hover:bg-slate-100"
                      }`}
                      data-testid={`mobile-${tab.testId}`}
                    >
                      {tab.label}
                    </Link>
                  </SheetClose>
                );
              })}

              {(visiblePurchasingStrategySubtabs.length > 0 || visibleInventorySubtabs.length > 0 || visibleAdminSubtabs.length > 0) && (
                <Accordion type="multiple" className="mt-1">
                  {visiblePurchasingStrategySubtabs.length > 0 && (
                    <AccordionItem value="purchasing-strategy" className="border-b-0">
                      <AccordionTrigger
                        className={`px-3 min-h-11 text-[14px] font-bold font-heading no-underline hover:no-underline ${
                          purchasingStrategyActive ? "text-[#0B6B74]" : "text-[#344054]"
                        }`}
                        data-testid="mobile-nav-purchasing-strategy"
                      >
                        Supplier Management
                      </AccordionTrigger>
                      <AccordionContent className="pl-3">
                        {visiblePurchasingStrategySubtabs.map((tab) => (
                          <SheetClose asChild key={tab.to}>
                            <Link
                              to={tab.to}
                              className="flex items-center min-h-11 px-3 rounded-lg text-[14px] font-medium text-[#475467] hover:bg-slate-100"
                              data-testid={`mobile-${tab.testId}`}
                            >
                              {tab.label}
                            </Link>
                          </SheetClose>
                        ))}
                      </AccordionContent>
                    </AccordionItem>
                  )}
                  {visibleInventorySubtabs.length > 0 && (
                    <AccordionItem value="inventory" className="border-b-0">
                      <AccordionTrigger
                        className={`px-3 min-h-11 text-[14px] font-bold font-heading no-underline hover:no-underline ${
                          inventoryActive ? "text-[#0B6B74]" : "text-[#344054]"
                        }`}
                        data-testid="mobile-nav-inventory"
                      >
                        Inventory Management
                      </AccordionTrigger>
                      <AccordionContent className="pl-3">
                        {visibleInventorySubtabs.map((tab) => (
                          <SheetClose asChild key={tab.to}>
                            <Link
                              to={tab.to}
                              className="flex items-center min-h-11 px-3 rounded-lg text-[14px] font-medium text-[#475467] hover:bg-slate-100"
                              data-testid={`mobile-${tab.testId}`}
                            >
                              {tab.label}
                            </Link>
                          </SheetClose>
                        ))}
                      </AccordionContent>
                    </AccordionItem>
                  )}
                  {visibleAdminSubtabs.length > 0 && (
                    <AccordionItem value="admin" className="border-b-0">
                      <AccordionTrigger
                        className={`px-3 min-h-11 text-[14px] font-bold font-heading no-underline hover:no-underline ${
                          adminActive ? "text-[#0B6B74]" : "text-[#344054]"
                        }`}
                        data-testid="mobile-nav-admin"
                      >
                        Master Data
                      </AccordionTrigger>
                      <AccordionContent className="pl-3">
                        {visibleAdminSubtabs.map((tab) => (
                          <SheetClose asChild key={tab.to}>
                            <Link
                              to={tab.to}
                              className="flex items-center min-h-11 px-3 rounded-lg text-[14px] font-medium text-[#475467] hover:bg-slate-100"
                              data-testid={`mobile-${tab.testId}`}
                            >
                              {tab.label}
                            </Link>
                          </SheetClose>
                        ))}
                      </AccordionContent>
                    </AccordionItem>
                  )}
                </Accordion>
              )}
            </div>

            {user && (
              <div className="border-t border-[#EAECF0] px-4 py-3">
                <div className="flex items-center gap-2 mb-2" data-testid="mobile-nav-user-info">
                  <UserCircle size={20} weight="fill" className="text-[#667085]" />
                  <div className="flex flex-col min-w-0">
                    <span className="text-[13px] font-bold text-[#1D2939] truncate">{user.name || user.email}</span>
                    <span className="text-[11px] text-[#667085] truncate">{user.email}</span>
                  </div>
                </div>
                {(user.role === "super_admin" || user.role === "admin") && (
                  <SheetClose asChild>
                    <Link
                      to="/admin/access-management"
                      className="flex items-center gap-2 min-h-11 px-3 rounded-lg text-[14px] font-bold text-[#344054] hover:bg-slate-100"
                      data-testid="mobile-nav-access-management"
                    >
                      <ShieldCheck size={16} weight="bold" />
                      Access Management
                    </Link>
                  </SheetClose>
                )}
                <SheetClose asChild>
                  <button
                    type="button"
                    onClick={logout}
                    className="flex items-center gap-2 min-h-11 px-3 rounded-lg text-[14px] font-bold text-[#B42318] hover:bg-red-50 w-full"
                    data-testid="mobile-nav-sign-out"
                  >
                    <SignOut size={16} weight="bold" />
                    Sign Out
                  </button>
                </SheetClose>
              </div>
            )}
          </SheetContent>
        </Sheet>
      </div>
    </>
  );
};
