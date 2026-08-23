import { BrowserRouter, Routes, Route, useLocation } from "react-router-dom";
import BomExplorerPage from "@/pages/BomExplorerPage";
import PurchasingPlanPage from "@/pages/PurchasingPlanPage";
import ProductionPlanPage from "@/pages/ProductionPlanPage";
import ProductionConfirmationPage from "@/pages/ProductionConfirmationPage";
import AdminPage from "@/pages/AdminPage";
import InventoryPage from "@/pages/InventoryPage";
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

// /storeapproval is intentionally left OUTSIDE the login gate - the
// warehouse/store team processing stock requests should not need an
// Entra ID account (explicit user choice, Store Approval workflow).
function AuthGate({ children }) {
  const { user, loading, isPendingAccess } = useAuth();
  const location = useLocation();

  if (location.pathname.startsWith("/storeapproval")) return children;
  if (loading) return null;
  if (!user) return <LoginPage />;
  if (isPendingAccess) return <PendingAccessPage />;
  return children;
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AuthGate>
          <Routes>
            <Route path="/" element={<ProtectedRoute page="bom_explorer"><BomExplorerPage /></ProtectedRoute>} />
            <Route path="/purchasing-plan" element={<ProtectedRoute page="purchasing_plan"><PurchasingPlanPage /></ProtectedRoute>} />
            <Route path="/production-plan" element={<ProtectedRoute page="production_plan"><ProductionPlanPage /></ProtectedRoute>} />
            <Route path="/production-confirmation" element={<ProtectedRoute page="production_confirmation"><ProductionConfirmationPage /></ProtectedRoute>} />
            <Route path="/inventory" element={<ProtectedRoute page="inventory"><InventoryPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/supplier-master" element={<ProtectedRoute page="supplier_master"><SupplierMasterPage /></ProtectedRoute>} />
            <Route path="/purchasing-strategy/quota-allocation" element={<ProtectedRoute page="quota_allocation"><QuotaAllocationPage /></ProtectedRoute>} />
            <Route path="/admin" element={<ProtectedRoute page="admin"><AdminPage /></ProtectedRoute>} />
            <Route path="/admin/sap-write" element={<ProtectedRoute page="admin_sap_write"><SapWritePage /></ProtectedRoute>} />
            <Route path="/admin/create-material" element={<ProtectedRoute page="admin_create_material"><CreateMaterialPage /></ProtectedRoute>} />
            <Route path="/admin/l1-l2-report" element={<ProtectedRoute page="admin"><L1L2ReportPage /></ProtectedRoute>} />
            <Route path="/admin/access-management" element={<ProtectedRoute superAdminOnly><AccessManagementPage /></ProtectedRoute>} />
            <Route path="/storeapproval" element={<StoreApprovalPage />} />
            <Route path="/storeapproval/journal" element={<StoreApprovalPage />} />
            <Route path="/storeapproval/balance" element={<StoreApprovalPage />} />
            <Route path="/storeapproval/movements" element={<StoreApprovalPage />} />
            <Route path="/storeapproval/request/:requestId" element={<StoreApprovalPage />} />
          </Routes>
        </AuthGate>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
