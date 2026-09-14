import { Link, useLocation } from "react-router-dom";
import { CaretDown, UserCircle, SignOut, ShieldCheck, List, Flask } from "@phosphor-icons/react";
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
// Reorganized Sep 4 2026, user's explicit ask: menu groups now match real
// ERP module boundaries (Procurement vs Supplier/Vendor lifecycle vs true
// Master Data vs Administration) instead of dumping supplier-portal admin
// actions under "Master Data" just because that's where they happened to
// land first. Routes are unchanged; only labels/grouping changed. `page`
// matches backend/auth_service.py's PAGE_CATALOG keys - a tab only
// renders if the signed-in user has that page.
const TABS = [
  { to: "/", label: "BOM Management", testId: "nav-bom-explorer", page: "bom_explorer" },
  { to: "/production-confirmation", label: "Production Confirmation", testId: "nav-production-confirmation", page: "production_confirmation" },
];

const PROCUREMENT_SUBTABS = [
  { to: "/purchasing-plan", label: "Procurement Planning", testId: "nav-procurement-planning", page: "purchasing_plan" },
  // Aug 2026: Purchase Order Creation automation - writes real POs into SAP ByDesign.
  { to: "/purchasing-strategy/purchase-order-create", label: "Create Purchase Order", testId: "nav-procurement-purchase-order-create", page: "purchase_order" },
  // Sep 14 2026, user's explicit ask: standalone Service Purchase Order
  // form - full copy of the Create Purchase Order flow, same permission.
  { to: "/purchasing-strategy/service-purchase-order-create", label: "Service Purchase Order", testId: "nav-procurement-service-purchase-order-create", page: "purchase_order" },
  // Sep 4 2026, user's explicit ask: visibility into POs already created via this app.
  { to: "/purchasing-strategy/created-purchase-orders", label: "Created POs", testId: "nav-procurement-created-purchase-orders", page: "created_purchase_orders" },
  // Sep 4 2026, user's explicit ask: pick any vendor, see their open PO lines.
  { to: "/purchasing-strategy/open-purchase-orders", label: "Open Purchase Orders", testId: "nav-procurement-open-purchase-orders", page: "open_purchase_orders" },
  { to: "/purchasing-strategy/quota-allocation", label: "Quota Allocation", testId: "nav-procurement-quota-allocation", page: "quota_allocation" },
  // Sep 5 2026, user's explicit ask: moved out of Supplier Management -
  // it's a procurement/receiving action, not a supplier-record one.
  { to: "/admin/grn-approval", label: "Vendor Goods Receipt", testId: "nav-procurement-vendor-goods-receipt", page: "vendor_goods_receipt" },
];

const SUPPLIER_MANAGEMENT_SUBTABS = [
  { to: "/purchasing-strategy/supplier-master", label: "Supplier Master", testId: "nav-supplier-management-supplier-master", page: "supplier_master" },
  { to: "/admin/supplier-portal-invite", label: "Invite Supplier", testId: "nav-supplier-management-supplier-portal-invite", page: "supplier_portal_invite" },
  { to: "/admin/supplier-portal-approvals", label: "Supplier Portal Approvals", testId: "nav-supplier-management-supplier-portal-approvals", page: ["supplier_portal_admin", "supplier_portal_documents"] },
  // Sep 2 2026, user's explicit ask: quick shortcut into the Supplier
  // Portal (separate vendor-JWT auth, not `vms_session`) so staff can log
  // in and use its vendor-impersonation search to view/act on any
  // vendor's own shipment-creation dashboard.
  { to: "/supplier-portal/login", label: "Supplier Dashboard", testId: "nav-supplier-management-supplier-dashboard", page: "supplier_dashboard" },
];

const INVENTORY_SUBTABS = [
  { to: "/inventory", label: "Stock Overview", testId: "nav-inventory-stock-overview", page: "inventory" },
  // Aug 27 2026, user's explicit ask: own grantable right, separate from
  // Stock Overview above.
  { to: "/inventory/inter-plant-transfer", label: "Inter Plant Stock Transfer", testId: "nav-inventory-stock-transfer", page: "stock_transfer" },
  // Sep 3 2026, user's explicit ask: now its own grantable right
  // (was: no `page` key at all, visible to any logged-in user, Aug 28
  // 2026 "to start with" decision).
  { to: "/inventory/inbound-receipts", label: "Inbound STO Receipt", testId: "nav-inventory-inbound-receipts", page: "inbound_stock_transfer" },
  // Aug 2026, user's explicit ask: links out to the existing Store
  // Approval screen (public/unauthenticated route, unchanged) - just a
  // shortcut into it from the main nav, no backend permission change.
  // Sep 5 2026, user's explicit ask: relabeled "Goods Issue" -> "Store
  // Goods Issue" for clarity (still the same store_approval page/route).
  { to: "/storeapproval", label: "Store Goods Issue", testId: "nav-inventory-goods-issue", page: "store_approval" },
];

// Sep 4 2026, user's explicit ask: only true reference/master data left
// here now - transactional supplier-portal actions moved to Supplier
// Management above, raw admin tools moved to Administration below.
const MASTER_DATA_SUBTABS = [
  { to: "/admin", label: "Component Master", testId: "nav-master-data-component-master", page: "admin" },
  { to: "/admin/create-material", label: "Create Material", testId: "nav-master-data-create-material", page: "admin_create_material" },
  { to: "/admin/activate-material-site", label: "Activate Material Site", testId: "nav-master-data-activate-material-site", page: "admin_activate_material_site" },
  { to: "/admin/l1-l2-report", label: "L1/L2 Item Report", testId: "nav-master-data-l1l2-report", page: "admin" },
  { to: "/admin/closing-inventory-report", label: "Closing Inventory Report", testId: "nav-master-data-closing-inventory-report", page: "admin" },
];

const ADMINISTRATION_SUBTABS = [
  { to: "/admin/sap-write", label: "SAP Write", testId: "nav-administration-sap-write", page: "admin_sap_write" },
];

export const NavTabs = () => {
  const { pathname } = useLocation();
  const { user, hasPageAccess, logout } = useAuth();
  const visibleTabs = TABS.filter((t) => hasPageAccess(t.page));
  const visibleProcurementSubtabs = PROCUREMENT_SUBTABS.filter((t) => hasPageAccess(t.page));
  const visibleSupplierManagementSubtabs = SUPPLIER_MANAGEMENT_SUBTABS.filter((t) => hasPageAccess(t.page));
  const visibleInventorySubtabs = INVENTORY_SUBTABS.filter((t) => !t.page || hasPageAccess(t.page));
  const visibleMasterDataSubtabs = MASTER_DATA_SUBTABS.filter((t) => hasPageAccess(t.page));
  const visibleAdministrationSubtabs = ADMINISTRATION_SUBTABS.filter((t) => hasPageAccess(t.page));
  const procurementActive = PROCUREMENT_SUBTABS.some((t) => t.to === pathname);
  const supplierManagementActive = SUPPLIER_MANAGEMENT_SUBTABS.some((t) => t.to === pathname);
  const inventoryActive = pathname === "/inventory" || pathname === "/inventory/inter-plant-transfer" || pathname === "/inventory/inbound-receipts" || pathname.startsWith("/storeapproval");
  const masterDataActive = MASTER_DATA_SUBTABS.some((t) => t.to === pathname);
  const administrationActive = ADMINISTRATION_SUBTABS.some((t) => t.to === pathname);

  const dropdownGroups = [
    { key: "procurement", label: "Procurement", testId: "nav-procurement", active: procurementActive, tabs: visibleProcurementSubtabs },
    { key: "supplier-management", label: "Supplier Management", testId: "nav-supplier-management", active: supplierManagementActive, tabs: visibleSupplierManagementSubtabs },
    { key: "inventory", label: "Inventory Management", testId: "nav-inventory", active: inventoryActive, tabs: visibleInventorySubtabs },
    { key: "master-data", label: "Master Data", testId: "nav-master-data", active: masterDataActive, tabs: visibleMasterDataSubtabs },
    { key: "administration", label: "Administration", testId: "nav-administration", active: administrationActive, tabs: visibleAdministrationSubtabs },
  ];

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
      {dropdownGroups.map((group) => group.tabs.length > 0 && (
        <DropdownMenu key={group.key}>
          <DropdownMenuTrigger
            className={`flex items-center gap-1 px-3.5 py-1.5 rounded-full text-[13px] font-bold font-heading transition-colors duration-150 outline-none ${
              group.active ? "bg-white text-[#0B6B74]" : "text-white/85 hover:bg-white/15 hover:text-white"
            }`}
            data-testid={group.testId}
          >
            {group.label}
            <CaretDown size={10} weight="bold" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[200px] bg-white border border-[#D0D5DD]" data-testid={`${group.testId}-dropdown-content`}>
            {group.tabs.map((tab) => (
              <DropdownMenuItem key={tab.to} asChild>
                <Link to={tab.to} className="w-full cursor-pointer" data-testid={tab.testId}>
                  {tab.label}
                </Link>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ))}

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
            {/* Aug 2026 - admin-only test page for multi-Reporting-Point
                production models (e.g. PL-0037A_2's Blanking/Bending/Forming
                split) - deliberately NOT in PAGE_CATALOG/allowed_pages, so it
                can never be granted to a regular "user" account. */}
            {(user.role === "super_admin" || user.role === "admin") && (
              <DropdownMenuItem asChild>
                <Link to="/admin/production-confirmation-test" className="w-full cursor-pointer flex items-center gap-2" data-testid="nav-production-confirmation-test">
                  <Flask size={14} weight="bold" />
                  Production Confirmation (Test)
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

              {dropdownGroups.some((g) => g.tabs.length > 0) && (
                <Accordion type="multiple" className="mt-1">
                  {dropdownGroups.map((group) => group.tabs.length > 0 && (
                    <AccordionItem key={group.key} value={group.key} className="border-b-0">
                      <AccordionTrigger
                        className={`px-3 min-h-11 text-[14px] font-bold font-heading no-underline hover:no-underline ${
                          group.active ? "text-[#0B6B74]" : "text-[#344054]"
                        }`}
                        data-testid={`mobile-${group.testId}`}
                      >
                        {group.label}
                      </AccordionTrigger>
                      <AccordionContent className="pl-3">
                        {group.tabs.map((tab) => (
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
                  ))}
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
                {(user.role === "super_admin" || user.role === "admin") && (
                  <SheetClose asChild>
                    <Link
                      to="/admin/production-confirmation-test"
                      className="flex items-center gap-2 min-h-11 px-3 rounded-lg text-[14px] font-bold text-[#344054] hover:bg-slate-100"
                      data-testid="mobile-nav-production-confirmation-test"
                    >
                      <Flask size={16} weight="bold" />
                      Production Confirmation (Test)
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
