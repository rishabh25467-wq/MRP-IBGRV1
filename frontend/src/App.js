import { BrowserRouter, Routes, Route } from "react-router-dom";
import BomExplorerPage from "@/pages/BomExplorerPage";
import PurchasingPlanPage from "@/pages/PurchasingPlanPage";
import ProductionPlanPage from "@/pages/ProductionPlanPage";
import AdminPage from "@/pages/AdminPage";
import InventoryPage from "@/pages/InventoryPage";
import SupplierMasterPage from "@/pages/SupplierMasterPage";
import QuotaAllocationPage from "@/pages/QuotaAllocationPage";
import SapWritePage from "@/pages/SapWritePage";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<BomExplorerPage />} />
        <Route path="/purchasing-plan" element={<PurchasingPlanPage />} />
        <Route path="/production-plan" element={<ProductionPlanPage />} />
        <Route path="/inventory" element={<InventoryPage />} />
        <Route path="/purchasing-strategy/supplier-master" element={<SupplierMasterPage />} />
        <Route path="/purchasing-strategy/quota-allocation" element={<QuotaAllocationPage />} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="/admin/sap-write" element={<SapWritePage />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
