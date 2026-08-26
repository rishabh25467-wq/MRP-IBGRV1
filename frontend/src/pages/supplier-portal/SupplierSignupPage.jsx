import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Buildings, CheckCircle, UploadSimple } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { supplierApi } from "@/lib/supplierPortalApi";

function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}

const FIELD_DEFS = [
  { name: "vendor_code", label: "Vendor Code (as per SAP)", placeholder: "e.g. S1822" },
  { name: "company_name", label: "Company Name", placeholder: "Your registered business name" },
  { name: "email", label: "Email", placeholder: "you@company.com", type: "email" },
  { name: "password", label: "Password", placeholder: "At least 8 characters", type: "password" },
  { name: "gst_number", label: "GST Number", placeholder: "22AAAAA0000A1Z5" },
  { name: "pan_number", label: "PAN Number", placeholder: "AAAAA0000A" },
];

export default function SupplierSignupPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ vendor_code: "", company_name: "", email: "", password: "", gst_number: "", pan_number: "" });
  const [gstDoc, setGstDoc] = useState(null);
  const [panDoc, setPanDoc] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (!gstDoc || !panDoc) {
      setError("Please upload both your GST certificate and PAN card.");
      return;
    }
    setSubmitting(true);
    try {
      const fd = new FormData();
      Object.entries(form).forEach(([k, v]) => fd.append(k, v));
      fd.append("gst_doc", gstDoc);
      fd.append("pan_doc", panDoc);
      await supplierApi.post("/signup", fd, { headers: { "Content-Type": "multipart/form-data" } });
      setDone(true);
    } catch (e) {
      setError(formatApiErrorDetail(e.response?.data?.detail) || e.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    return (
      <div className="min-h-screen bg-[#0F172A] flex items-center justify-center p-4" data-testid="supplier-signup-success">
        <div className="max-w-md w-full bg-white rounded-2xl p-8 text-center shadow-2xl">
          <CheckCircle size={48} weight="fill" className="text-[#F59E0B] mx-auto mb-4" />
          <h1 className="font-heading text-xl font-bold text-[#0F172A]">Request Submitted</h1>
          <p className="text-sm text-[#475467] mt-2">
            Thanks for registering. Your onboarding request is now pending review by our purchasing team.
            You'll be able to sign in once your account is approved.
          </p>
          <Link to="/supplier-portal/login">
            <Button className="mt-6 w-full bg-[#0F172A] hover:bg-[#1E293B]" data-testid="supplier-signup-go-to-login">Go to Sign In</Button>
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#0F172A] flex items-center justify-center p-4" data-testid="supplier-signup-page">
      <div className="max-w-lg w-full bg-white rounded-2xl p-8 shadow-2xl">
        <div className="flex items-center gap-2 mb-1">
          <Buildings size={24} weight="fill" className="text-[#F59E0B]" />
          <span className="font-heading font-bold text-[#0F172A] tracking-tight">Supplier Portal</span>
        </div>
        <h1 className="font-heading text-lg font-bold text-[#0F172A] mt-3">Vendor Onboarding</h1>
        <p className="text-sm text-[#667085] mt-1">Register with your SAP vendor code to start viewing your Purchase Orders online.</p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          {FIELD_DEFS.map((f) => (
            <div key={f.name}>
              <Label className="text-xs font-semibold text-[#344054]">{f.label}</Label>
              <Input
                type={f.type || "text"}
                placeholder={f.placeholder}
                value={form[f.name]}
                onChange={(e) => setForm({ ...form, [f.name]: e.target.value })}
                required
                className="mt-1"
                data-testid={`supplier-signup-input-${f.name}`}
              />
            </div>
          ))}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs font-semibold text-[#344054]">GST Certificate</Label>
              <label className="mt-1 flex items-center gap-2 border border-dashed border-[#D0D5DD] rounded-lg px-3 py-2 text-xs text-[#667085] cursor-pointer hover:border-[#F59E0B]">
                <UploadSimple size={16} />
                <span className="truncate">{gstDoc ? gstDoc.name : "Upload file"}</span>
                <input type="file" accept=".pdf,.png,.jpg,.jpeg" className="hidden" onChange={(e) => setGstDoc(e.target.files?.[0] || null)} data-testid="supplier-signup-gst-doc-input" />
              </label>
            </div>
            <div>
              <Label className="text-xs font-semibold text-[#344054]">PAN Card</Label>
              <label className="mt-1 flex items-center gap-2 border border-dashed border-[#D0D5DD] rounded-lg px-3 py-2 text-xs text-[#667085] cursor-pointer hover:border-[#F59E0B]">
                <UploadSimple size={16} />
                <span className="truncate">{panDoc ? panDoc.name : "Upload file"}</span>
                <input type="file" accept=".pdf,.png,.jpg,.jpeg" className="hidden" onChange={(e) => setPanDoc(e.target.files?.[0] || null)} data-testid="supplier-signup-pan-doc-input" />
              </label>
            </div>
          </div>

          {error && <div className="text-sm text-[#B42318] bg-[#FEF3F2] border border-[#FDA29B] rounded-lg px-3 py-2" data-testid="supplier-signup-error">{error}</div>}

          <Button type="submit" disabled={submitting} className="w-full bg-[#0F172A] hover:bg-[#1E293B]" data-testid="supplier-signup-submit-button">
            {submitting ? "Submitting..." : "Submit for Approval"}
          </Button>
        </form>

        <p className="text-xs text-[#667085] text-center mt-5">
          Already registered? <Link to="/supplier-portal/login" className="text-[#0F172A] font-semibold underline" data-testid="supplier-signup-login-link">Sign in</Link>
        </p>
      </div>
    </div>
  );
}
