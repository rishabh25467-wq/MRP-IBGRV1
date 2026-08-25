import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import BomExplorerPage from "@/pages/BomExplorerPage";
import PurchasingPlanPage from "@/pages/PurchasingPlanPage";
import ProductionPlanPage from "@/pages/ProductionPlanPage";
import ProductionConfirmationPage from "@/pages/ProductionConfirmationPage";
import AdminPage from "@/pages/AdminPage";
import InventoryPage from "@/pages/InventoryPage";
import StockTransferPage from "@/pages/StockTransferPage";
import DeliveryNotePage from "@/pages/DeliveryNotePage";
import GatePassPage from "@/pages/GatePassPage";
import SupplierMasterPage from "@/pages/SupplierMasterPage";
import QuotaAllocationPage from "@/pages/QuotaAllocationPage";
import SapWritePage from "@/pages/SapWritePage";
import CreateMaterialPage from "@/pages/CreateMaterialPage";
import L1L2ReportPage from "@/pages/L1L2ReportPage";
import StoreApprovalPage from "@/pages/StoreApprovalPage";
import LoginPage from "@/pages/LoginPage";
import PendingAccessPage from "@/pages/PendingAccessPage";
import AccessManagementPage from "@/pages/AccessManagementPage";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Footer } from "@/components/Footer";

// Aug 2026: Store Approval now requires Entra ID login like every other
// page (was previously exempted here) - user's explicit ask, now that
// the actor's name comes from the signed-in session, not a manual box.
function AuthGate({ children }) {
  const { user, loading, isPendingAccess } = useAuth();

  if (loading) return null;
  if (!user) return <LoginPage />;
  if (isPendingAccess) return <PendingAccessPage />;
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

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AuthGate>
          <div className="min-h-screen flex flex-col">
            <div className="flex-1">
              <Routes>
                <Route path="/" element={<HomeRoute />} />
                <Route path="/purchasing-plan" element={<ProtectedRoute page="purchasing_plan"><PurchasingPlanPage /></ProtectedRoute>} />
                <Route path="/production-plan" element={<ProtectedRoute page="production_plan"><ProductionPlanPage /></ProtectedRoute>} />
                <Route path="/production-confirmation" element={<ProtectedRoute page="production_confirmation"><ProductionConfirmationPage /></ProtectedRoute>} />
                <Route path="/inventory" element={<ProtectedRoute page="inventory"><InventoryPage /></ProtectedRoute>} />
                <Route path="/inventory/inter-plant-transfer" element={<ProtectedRoute page="stock_transfer"><StockTransferPage /></ProtectedRoute>} />
                <Route path="/inventory/inter-plant-transfer/:stoId/delivery-note" element={<ProtectedRoute page="stock_transfer"><DeliveryNotePage /></ProtectedRoute>} />
                <Route path="/inventory/inter-plant-transfer/:stoId/gate-pass" element={<ProtectedRoute page="stock_transfer"><GatePassPage /></ProtectedRoute>} />
                <Route path="/purchasing-strategy/supplier-master" element={<ProtectedRoute page="supplier_master"><SupplierMasterPage /></ProtectedRoute>} />
                <Route path="/purchasing-strategy/quota-allocation" element={<ProtectedRoute page="quota_allocation"><QuotaAllocationPage /></ProtectedRoute>} />
                <Route path="/admin" element={<ProtectedRoute page="admin"><AdminPage /></ProtectedRoute>} />
                <Route path="/admin/sap-write" element={<ProtectedRoute page="admin_sap_write"><SapWritePage /></ProtectedRoute>} />
                <Route path="/admin/create-material" element={<ProtectedRoute page="admin_create_material"><CreateMaterialPage /></ProtectedRoute>} />
                <Route path="/admin/l1-l2-report" element={<ProtectedRoute page="admin"><L1L2ReportPage /></ProtectedRoute>} />
                <Route path="/admin/access-management" element={<ProtectedRoute superAdminOnly><AccessManagementPage /></ProtectedRoute>} />
                <Route path="/storeapproval" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
                <Route path="/storeapproval/journal" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
                <Route path="/storeapproval/balance" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
                <Route path="/storeapproval/movements" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
                <Route path="/storeapproval/request/:requestId" element={<ProtectedRoute page="store_approval"><StoreApprovalPage /></ProtectedRoute>} />
              </Routes>
            </div>
            <Footer />
          </div>
        </AuthGate>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
