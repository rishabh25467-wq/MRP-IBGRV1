import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Buildings } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}

export default function SupplierLoginPage() {
  const navigate = useNavigate();
  const { refresh } = useSupplierAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await supplierApi.post("/login", { email, password });
      await refresh();
      navigate("/supplier-portal/dashboard");
    } catch (e) {
      setError(formatApiErrorDetail(e.response?.data?.detail) || e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans flex items-center justify-center p-4" data-testid="supplier-login-page">
      <div className="max-w-sm w-full bg-white rounded-sm border border-[#CBD3DB] shadow-sm p-8">
        <div className="flex items-center gap-2 mb-1 pb-3 border-b border-[#CBD3DB]">
          <Buildings size={22} weight="fill" className="text-[#0076CC]" />
          <span className="font-sans font-bold text-[#111827] tracking-tight">Supplier Portal</span>
        </div>
        <h1 className="font-sans text-base font-bold text-[#111827] mt-4">Vendor Sign In</h1>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <div>
            <Label className="text-xs font-semibold text-[#5B738B]">Email</Label>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required className="mt-1 rounded-sm border-[#CBD3DB] focus:ring-2 focus:ring-[#4DA3E0] focus:outline-none" data-testid="supplier-login-email-input" />
          </div>
          <div>
            <Label className="text-xs font-semibold text-[#5B738B]">Password</Label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required className="mt-1 rounded-sm border-[#CBD3DB] focus:ring-2 focus:ring-[#4DA3E0] focus:outline-none" data-testid="supplier-login-password-input" />
          </div>

          {error && <div className="text-sm text-[#B91C1C] bg-[#E02424]/10 border border-[#E02424]/30 rounded-sm px-3 py-2" data-testid="supplier-login-error">{error}</div>}

          <Button type="submit" disabled={submitting} className="w-full rounded-sm bg-[#0076CC] hover:bg-[#4DA3E0] transition-colors duration-150" data-testid="supplier-login-submit-button">
            {submitting ? "Signing in..." : "Sign In"}
          </Button>
        </form>

        <p className="text-xs text-[#5B738B] text-center mt-5">
          New vendor? <Link to="/supplier-portal/signup" className="text-[#0076CC] font-semibold underline" data-testid="supplier-login-signup-link">Register here</Link>
        </p>
      </div>
    </div>
  );
}
