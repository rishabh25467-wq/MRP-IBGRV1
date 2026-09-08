import { BrowserRouter, Routes, Route, Navigate, useLocation, useParams } from "react-router-dom";
import BomExplorerPage from "@/pages/BomExplorerPage";
import PurchasingPlanPage from "@/pages/PurchasingPlanPage";
import ProductionPlanPage from "@/pages/ProductionPlanPage";
import ProductionConfirmationPage from "@/pages/ProductionConfirmationPage";
import ProductionConfirmationTestPage from "@/pages/ProductionConfirmationTestPage";
import AdminPage from "@/pages/AdminPage";
import InventoryPage from "@/pages/InventoryPage";
import StockTransferPage from "@/pages/StockTransferPage";
import InboundReceiptsPage from "@/pages/InboundReceiptsPage";
import DeliveryNotePage from "@/pages/DeliveryNotePage";
import GatePassPage from "@/pages/GatePassPage";
import SupplierMasterPage from "@/pages/SupplierMasterPage";
import QuotaAllocationPage from "@/pages/QuotaAllocationPage";
import PurchaseOrderPage from "@/pages/PurchaseOrderPage";
import CreatedPurchaseOrdersPage from "@/pages/CreatedPurchaseOrdersPage";
import OpenPurchaseOrdersPage from "@/pages/OpenPurchaseOrdersPage";
import SapWritePage from "@/pages/SapWritePage";
import CreateMaterialPage from "@/pages/CreateMaterialPage";
import L1L2ReportPage from "@/pages/L1L2ReportPage";
import StoreApprovalPage from "@/pages/StoreApprovalPage";
import LoginPage from "@/pages/LoginPage";
import PendingAccessPage from "@/pages/PendingAccessPage";
import AccessManagementPage from "@/pages/AccessManagementPage";
import SupplierPortalApprovalsPage from "@/pages/SupplierPortalApprovalsPage";
import SupplierPortalInvitePage from "@/pages/SupplierPortalInvitePage";
import PlaywrightReliabilityReportPage from "@/pages/PlaywrightReliabilityReportPage";
import GrnApprovalPage from "@/pages/GrnApprovalPage";
import SupplierSignupPage from "@/pages/supplier-portal/SupplierSignupPage";
import SupplierLoginPage from "@/pages/supplier-portal/SupplierLoginPage";
import SupplierPendingPage from "@/pages/supplier-portal/SupplierPendingPage";
import SupplierDashboardPage from "@/pages/supplier-portal/SupplierDashboardPage";
import SupplierShipmentsPage from "@/pages/supplier-portal/SupplierShipmentsPage";
import SupplierDocumentsPage from "@/pages/supplier-portal/SupplierDocumentsPage";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { SupplierAuthProvider, useSupplierAuth } from "@/contexts/SupplierAuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Footer } from "@/components/Footer";
import { ConcurrencyBadge } from "@/components/ConcurrencyBadge";

// Aug 2026: Store Approval now requires Entra ID login like every other
// page (was previously exempted here) - user's explicit ask, now that
// the actor's name comes from the signed-in session, not a manual box.
function AuthGate({ children }) {
  const { user, loading, isPendingAccess } = useAuth();

  if (loading) return null;
  // User's explicit ask (Sep 2026): build-version footer must show on
  // EVERY page - Login/Pending-Access used to bypass the layout wrapper
  // that carries <Footer/> below, so they never got it.
  if (!user) return <div className="min-h-screen flex flex-col"><div className="flex-1"><LoginPage /></div><Footer /></div>;
  if (isPendingAccess) return <div className="min-h-screen flex flex-col"><div className="flex-1"><PendingAccessPage /></div><Footer /></div>;
  return children;
}

// Aug 2026 bug fix: "/" was hard-gated to bom_explorer, so a user granted
// ONLY a different page (e.g. just "inventory") landed on their bookmarked
// home URL and hit a hard "Access Denied" even though they legitimately
// have access to part of the app - isPendingAccess above doesn't catch
// this since it only fires when allowed_pages is completely empty. Send
// them straight to the first page they actually have instead.
const FIRST_ACCESSIBLE_PAGE_ROUTES = [
  ["purchasing_plan", "/purchasing-plan"],
  ["production_plan", "/production-plan"],
  ["production_confirmation", "/production-confirmation"],
  ["inventory", "/inventory"],
  ["supplier_master", "/purchasing-strategy/supplier-master"],
  ["quota_allocation", "/purchasing-strategy/quota-allocation"],
  ["admin", "/admin"],
  ["admin_sap_write", "/admin/sap-write"],
  ["admin_create_material", "/admin/create-material"],
  ["store_approval", "/storeapproval"],
  ["supplier_portal_admin", "/admin/supplier-portal-approvals"],
  ["supplier_portal_documents", "/admin/supplier-portal-approvals"],
  // Sep 5 2026, same bug-fix pattern for the newly split-out permissions.
  ["supplier_portal_invite", "/admin/supplier-portal-invite"],
  ["vendor_goods_receipt", "/admin/grn-approval"],
  ["created_purchase_orders", "/purchasing-strategy/created-purchase-orders"],
  ["open_purchase_orders", "/purchasing-strategy/open-purchase-orders"],
  ["purchase_order", "/purchasing-strategy/purchase-order-create"],
  ["stock_transfer", "/inventory/inter-plant-transfer"],
  ["inbound_stock_transfer", "/inventory/inbound-receipts"],
];

function HomeRoute() {
  const { hasPageAccess } = useAuth();
  if (hasPageAccess("bom_explorer")) return <BomExplorerPage />;
  const firstAccessible = FIRST_ACCESSIBLE_PAGE_ROUTES.find(([page]) => hasPageAccess(page));
  if (firstAccessible) return <Navigate to={firstAccessible[1]} replace />;
  return (
    <div className="min-h-screen bg-[#F9FAFB] flex items-center justify-center p-4" data-testid="no-accessible-pages">
      <div className="text-center">
        <h1 className="font-heading text-lg font-bold text-[#101828]">No Pages Granted Yet</h1>
        <p className="text-sm text-[#667085] mt-1">Ask your IT admin to grant you access to a page.</p>
      </div>
    </div>
  );
}

// Aug 2026 - external Supplier Portal: separate JWT-based user base
// (vendors, not Azure AD identities), so it must never be wrapped by the
// internal AuthGate below (which forces a Microsoft sign-in for anyone
// without a `vms_session` cookie - suppliers don't have one at all).
// Aug 28 2026, user's explicit ask: the URL must clearly show WHICH
// vendor is being viewed instead of a bare "/dashboard" - redirects to
// the account's own vendor_code the first time, then that code stays in
// the URL (also doubles as the mechanism for the testing-only vendor
// impersonation search - see SupplierDashboardPage.jsx).
function SupplierPortalGate({ page }) {
  const { account, loading } = useSupplierAuth();
  const { vendorCode } = useParams();
  if (loading) return null;
  if (!account) return <Navigate to="/supplier-portal/login" replace />;
  if (account.status !== "approved") return <SupplierPendingPage />;
  if (!vendorCode) return <Navigate to={`/supplier-portal/${page}/${account.vendor_code}`} replace />;
  return page === "shipments" ? <SupplierShipmentsPage /> : page === "documents" ? <SupplierDocumentsPage /> : <SupplierDashboardPage />;
}

function InternalApp() {
  return (
    <AuthGate>
      <div className="min-h-screen flex flex-col">
        <div className="flex-1">
          <Routes>
            <Route path="/" element={<HomeRoute />} />
            <Route path="/purchasing-plan" element={<ProtectedRoute page="purchasing_plan"><PurchasingPlanPage /></ProtectedRoute>} />
            <Route path="/production-plan" element={<ProtectedRoute page="production_plan"><ProductionPlanPage /></ProtectedRoute>} />
            <Route path="/production-confirmation" element={<ProtectedRoute page="production_confirmation"><ProductionConfirmationPage /></ProtectedRoute>} />
            {/* Aug 2026 - admin-only test page for multi-Reporting-Point
                models (RP10/RP20/END style) - superAdminOnly, not part of
                PAGE_CATALOG, so it can never be granted to a regular user. */}
            <Route path="/admin/production-confirmation-test" element={<ProtectedRoute superAdminOnly><ProductionConfirmationTestPage /></ProtectedRoute>} />
            <Route path="/inventory" element={<ProtectedRoute page="inventory"><InventoryPage /></ProtectedRoute>} />
            <Route path="/inventory/inter-plant-transfer" element={<ProtectedRoute page="stock_transfer"><StockTransferPage /></ProtectedRoute>} />
            <Route path="/inventory/inter-plant-transfer/:stoId/delivery-note" element={<ProtectedRoute page="stock_transfer"><DeliveryNotePage /></ProtectedRoute>} />
            <Route path="/inventory/inter-plant-transfer/:stoId/gate-pass" element={<ProtectedRoute page="stock_transfer"><GatePassPage /></ProtectedRoute>} />
            <Route path="/inventory/inbound-receipts" element={<ProtectedRoute page="inbound_stock_transfer"><InboundReceiptsPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/supplier-master" element={<ProtectedRoute page="supplier_master"><SupplierMasterPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/quota-allocation" element={<ProtectedRoute page="quota_allocation"><QuotaAllocationPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/purchase-order-create" element={<ProtectedRoute page="purchase_order"><PurchaseOrderPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/created-purchase-orders" element={<ProtectedRoute page="created_purchase_orders"><CreatedPurchaseOrdersPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/open-purchase-orders" element={<ProtectedRoute page="open_purchase_orders"><OpenPurchaseOrdersPage /></ProtectedRoute>} />
            <Route path="/admin" element={<ProtectedRoute page="admin"><AdminPage /></ProtectedRoute>} />
            <Route path="/admin/sap-write" element={<ProtectedRoute page="admin_sap_write"><SapWritePage /></ProtectedRoute>} />
            <Route path="/admin/create-material" element={<ProtectedRoute page="admin_create_material"><CreateMaterialPage /></ProtectedRoute>} />
            <Route path="/admin/l1-l2-report" element={<ProtectedRoute page="admin"><L1L2ReportPage /></ProtectedRoute>} />
            <Route path="/admin/access-management" element={<ProtectedRoute superAdminOnly><AccessManagementPage /></ProtectedRoute>} />
            <Route path="/admin/supplier-portal-approvals" element={<ProtectedRoute page={["supplier_portal_admin", "supplier_portal_documents"]}><SupplierPortalApprovalsPage /></ProtectedRoute>} />
            <Route path="/admin/supplier-portal-invite" element={<ProtectedRoute page="supplier_portal_invite"><SupplierPortalInvitePage /></ProtectedRoute>} />
            <Route path="/playwrightrate" element={<ProtectedRoute superAdminOnly><PlaywrightReliabilityReportPage /></ProtectedRoute>} />
            <Route path="/admin/grn-approval" element={<ProtectedRoute page="vendor_goods_receipt"><GrnApprovalPage /></ProtectedRoute>} />
            <Route path="/storeapproval" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
            <Route path="/storeapproval/journal" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
            <Route path="/storeapproval/balance" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
            <Route path="/storeapproval/movements" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
            <Route path="/storeapproval/request/:requestId" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
        <Footer />
        <ConcurrencyBadge />
      </div>
    </AuthGate>
  );
}

function AppShell() {
  const { pathname } = useLocation();
  if (pathname.startsWith("/supplier-portal")) {
    return (
      <div className="min-h-screen flex flex-col">
        <div className="flex-1">
          <Routes>
            <Route path="/supplier-portal/signup" element={<SupplierSignupPage />} />
            <Route path="/supplier-portal/login" element={<SupplierLoginPage />} />
            <Route path="/supplier-portal" element={<SupplierPortalGate page="dashboard" />} />
            <Route path="/supplier-portal/dashboard" element={<SupplierPortalGate page="dashboard" />} />
            <Route path="/supplier-portal/dashboard/:vendorCode" element={<SupplierPortalGate page="dashboard" />} />
            <Route path="/supplier-portal/shipments" element={<SupplierPortalGate page="shipments" />} />
            <Route path="/supplier-portal/shipments/:vendorCode" element={<SupplierPortalGate page="shipments" />} />
            <Route path="/supplier-portal/documents" element={<SupplierPortalGate page="documents" />} />
            <Route path="/supplier-portal/documents/:vendorCode" element={<SupplierPortalGate page="documents" />} />
          </Routes>
        </div>
        <Footer />
      </div>
    );
  }
  return <InternalApp />;
}

function App() {
  return (
    <AuthProvider>
      <SupplierAuthProvider>
        <BrowserRouter>
          <AppShell />
        </BrowserRouter>
      </SupplierAuthProvider>
    </AuthProvider>
  );
}

export default App;
