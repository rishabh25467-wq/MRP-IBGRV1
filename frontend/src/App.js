import { BrowserRouter, Routes, Route } from "react-router-dom";
import BomExplorerPage from "@/pages/BomExplorerPage";
import PurchasingPlanPage from "@/pages/PurchasingPlanPage";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<BomExplorerPage />} />
        <Route path="/purchasing-plan" element={<PurchasingPlanPage />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
