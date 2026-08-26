import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;

// Deliberately a SEPARATE axios instance from the one AuthContext.jsx
// configures globally (axios.defaults...) - that default instance has a
// 401 interceptor that redirects to Microsoft login, which would be
// completely wrong for the Supplier Portal (a wrong password here should
// show an inline error, not bounce the vendor to an Entra ID sign-in page
// they have no account on).
export const supplierApi = axios.create({
  baseURL: `${BACKEND_URL}/api/supplier-portal`,
  withCredentials: true,
});
