import { BrowserRouter, Routes, Route } from "react-router-dom";
import BomExplorerPage from "@/pages/BomExplorerPage";
import PurchasingPlanPage from "@/pages/PurchasingPlanPage";
import ProductionPlanPage from "@/pages/ProductionPlanPage";
import AdminPage from "@/pages/AdminPage";
import InventoryPage from "@/pages/InventoryPage";
import SuppliersPage from "@/pages/SuppliersPage";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<BomExplorerPage />} />
        <Route path="/purchasing-plan" element={<PurchasingPlanPage />} />
        <Route path="/production-plan" element={<ProductionPlanPage />} />
        <Route path="/inventory" element={<InventoryPage />} />
        <Route path="/suppliers" element={<SuppliersPage />} />
        <Route path="/admin" element={<AdminPage />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
