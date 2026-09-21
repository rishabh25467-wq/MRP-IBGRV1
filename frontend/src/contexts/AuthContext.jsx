import { createContext, useContext, useState, useEffect, useCallback } from "react";
import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

// Cookies must ride along with every request for the session to work, and
// a 401 from ANY endpoint (every /api/* route except /auth/me requires a
// session - see backend/auth_service.py's auth_middleware) always means
// "not signed in" in this app, so sending the user straight to Microsoft
// login is the right global behavior rather than a dead-end error toast.
axios.defaults.withCredentials = true;
axios.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      window.location.href = `${API}/auth/login`;
      return new Promise(() => {});
    }
    return Promise.reject(error);
  }
);

// Every page in the app's nav that IT can independently grant/revoke on the
// Access Management page - kept in sync with backend/auth_service.py's
// PAGE_CATALOG (labels only live here; the source of truth for WHICH pages
// exist and what backend routes they gate is the backend).
export const PAGE_LABELS = {
  bom_explorer: "BOM Management",
  purchasing_plan: "Procurement Planning",
  production_plan: "Production Planning",
  production_confirmation: "Production Confirmation",
  // Sep 2026, user's explicit ask: the admin-only "test" variant is now
  // its own grantable right.
  production_confirmation_test: "Production Confirmation (Test)",
  inventory: "Stock Overview",
  // Aug 27 2026, user's explicit ask: split out of "inventory" into its
  // own grantable right.
  stock_transfer: "Inter Plant Stock Transfer",
  // Sep 3 2026, user's explicit ask: split out of "anyUser" into its own
  // grantable right (was previously open to any logged-in user).
  inbound_stock_transfer: "Inbound STO Receipt",
  supplier_master: "Supplier Master",
  quota_allocation: "Quota Allocation",
  admin: "Component Master (Admin)",
  admin_sap_write: "SAP Write (Admin)",
  admin_create_material: "Create Material (Admin)",
  admin_activate_material_site: "Activate Material Site (Admin)",
  store_approval: "Store Goods Issue",
  supplier_portal_admin: "Supplier Portal Approvals",
  purchase_order: "Purchase Order Creation",
  // Sep 5 2026, user's explicit ask: split out of the "supplier_portal_admin"/
  // "purchase_order" catch-alls above into their own grantable rights.
  supplier_portal_invite: "Invite Supplier",
  vendor_goods_receipt: "Vendor Goods Receipt",
  supplier_dashboard: "Supplier Dashboard",
  created_purchase_orders: "Created POs",
  open_purchase_orders: "Open Purchase Orders",
  // Sep 21 2026, user's explicit ask (real incident, PO 25271): Cancel PO/
  // Cancel Item is now its own grantable right, separate from just being
  // able to view Open Purchase Orders.
  po_cancel: "Cancel Purchase Order",
  // Sep 9 2026, user's explicit ask: separate view-only right for the
  // GST/PAN/MSME/Bank documents on the Supplier Portal Approvals page,
  // without the Approve/Reject actions (still gated behind
  // supplier_portal_admin only).
  supplier_portal_documents: "Supplier Additional Documents",
  // Sep 18 2026, user's explicit ask: lets staff create a shipment on
  // behalf of a supplier + reset a supplier's password.
  act_as_supplier: "Act as Supplier",
};

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await axios.get(`${API}/auth/me`);
      setUser(data.authenticated ? data : null);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = () => {
    window.location.href = `${API}/auth/login`;
  };

  const logout = async () => {
    await axios.post(`${API}/auth/logout`);
    setUser(null);
    window.location.href = "/";
  };

  // Sep 9 2026: also accepts an array of page keys - access is granted if
  // the user has ANY one of them (e.g. Supplier Portal Approvals is
  // reachable with EITHER supplier_portal_admin OR the newer
  // view-only supplier_portal_documents right).
  const hasPageAccess = (pageKey) =>
    !!user && (user.role === "super_admin" || (Array.isArray(pageKey)
      ? pageKey.some((k) => (user.allowed_pages || []).includes(k))
      : (user.allowed_pages || []).includes(pageKey)));

  const isPendingAccess = !!user && user.role !== "super_admin" && (user.allowed_pages || []).length === 0;

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh, hasPageAccess, isPendingAccess }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
