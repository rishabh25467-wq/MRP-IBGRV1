import { Lock } from "@phosphor-icons/react";
import { useAuth } from "@/contexts/AuthContext";

// Wraps a single page's <Route element>. App.js's top-level auth gate
// already handles "not logged in" and "pending access" globally, so this
// only needs to check ONE page-specific permission - kept separate from
// that gate so a super_admin-only page (Access Management) can be
// protected the same way as a regular page.
export const ProtectedRoute = ({ page, superAdminOnly = false, children }) => {
  const { user, hasPageAccess } = useAuth();
  // Aug 2026: "admin" is a new tier alongside super_admin that can also
  // access Access Management (user's explicit ask) - kept the existing
  // `superAdminOnly` prop name (single usage site) rather than renaming
  // it everywhere for what both roles are now allowed to see.
  const allowed = superAdminOnly ? (user?.role === "super_admin" || user?.role === "admin") : hasPageAccess(page);

  if (!allowed) {
    return (
      <div className="min-h-screen bg-[#F9FAFB] flex items-center justify-center p-4" data-testid="access-denied-page">
        <div className="text-center">
          <Lock size={32} weight="fill" className="text-[#98A2B3] mx-auto mb-3" />
          <h1 className="font-heading text-lg font-bold text-[#101828]">Access Denied</h1>
          <p className="text-sm text-[#667085] mt-1">You don't have permission to view this page.</p>
        </div>
      </div>
    );
  }

  return children;
};
