import { createContext, useContext, useState, useEffect, useCallback } from "react";
import { supplierApi } from "@/lib/supplierPortalApi";

const SupplierAuthContext = createContext(null);

export const SupplierAuthProvider = ({ children }) => {
  const [account, setAccount] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await supplierApi.get("/me");
      setAccount(data.authenticated ? data : null);
    } catch {
      setAccount(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const logout = async () => {
    await supplierApi.post("/logout");
    setAccount(null);
  };

  return (
    <SupplierAuthContext.Provider value={{ account, loading, refresh, logout }}>
      {children}
    </SupplierAuthContext.Provider>
  );
};

export const useSupplierAuth = () => useContext(SupplierAuthContext);
