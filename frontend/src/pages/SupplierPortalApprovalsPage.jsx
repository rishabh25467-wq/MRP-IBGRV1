import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { CheckCircle, XCircle, FileText, Buildings } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { NavTabs } from "@/components/NavTabs";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const TABS = [
  { key: "pending", label: "Pending" },
  { key: "approved", label: "Approved" },
  { key: "rejected", label: "Rejected" },
];

const STATUS_BADGE = {
  pending: "bg-[#FFFAEB] text-[#B54708] border border-[#FEDF89]",
  approved: "bg-[#ECFDF3] text-[#067647] border border-[#ABEFC6]",
  rejected: "bg-[#FEF3F2] text-[#B42318] border border-[#FDA29B]",
};

export default function SupplierPortalApprovalsPage() {
  const [status, setStatus] = useState("pending");
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [rejectTarget, setRejectTarget] = useState(null);
  const [rejectReason, setRejectReason] = useState("");

  const load = async (s) => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/supplier-portal/accounts`, { params: { status: s } });
      setAccounts(data.accounts || []);
    } catch (err) {
      toast.error("Could not load supplier accounts", { description: err?.response?.data?.detail || err.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(status);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const approve = async (id) => {
    try {
      await axios.post(`${API}/admin/supplier-portal/${id}/approve`);
      toast.success("Supplier approved");
      load(status);
    } catch (err) {
      toast.error("Approval failed", { description: err?.response?.data?.detail || err.message });
    }
  };

  const reject = async () => {
    try {
      await axios.post(`${API}/admin/supplier-portal/${rejectTarget}/reject`, { reason: rejectReason });
      toast.success("Supplier rejected");
      setRejectTarget(null);
      setRejectReason("");
      load(status);
    } catch (err) {
      toast.error("Rejection failed", { description: err?.response?.data?.detail || err.message });
    }
  };

  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="supplier-portal-approvals-page">
      <NavTabs />
      <Toaster position="top-right" richColors />
      <div className="max-w-6xl mx-auto p-6">
        <div className="flex items-center gap-2 mb-1">
          <Buildings size={20} weight="fill" className="text-[#0E7C86]" />
          <h1 className="font-heading text-lg font-bold text-[#101828]">Supplier Portal Approvals</h1>
        </div>
        <p className="text-sm text-[#667085]">Review external vendor onboarding requests before they can sign in and view their Purchase Orders.</p>

        <div className="flex items-center gap-2 mt-5">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setStatus(t.key)}
              className={`px-3.5 py-1.5 rounded-full text-[13px] font-bold font-heading transition-colors ${
                status === t.key ? "bg-[#0E7C86] text-white" : "bg-white text-[#475467] border border-[#D0D5DD]"
              }`}
              data-testid={`supplier-portal-approvals-tab-${t.key}`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="mt-4 bg-white border border-[#EAECF0] rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-[#F9FAFB] text-[#667085] text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-2">Vendor Code</th>
                <th className="text-left px-4 py-2">Company</th>
                <th className="text-left px-4 py-2">Email</th>
                <th className="text-left px-4 py-2">GST / PAN</th>
                <th className="text-left px-4 py-2">Documents</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={7} className="px-4 py-6 text-center text-[#667085]" data-testid="supplier-portal-approvals-loading">Loading...</td></tr>
              )}
              {!loading && accounts.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-6 text-center text-[#667085]" data-testid="supplier-portal-approvals-empty">No {status} accounts.</td></tr>
              )}
              {!loading && accounts.map((a) => (
                <tr key={a._id} className="border-t border-[#EAECF0]" data-testid={`supplier-portal-approvals-row-${a._id}`}>
                  <td className="px-4 py-2 font-medium">{a.vendor_code}</td>
                  <td className="px-4 py-2">{a.company_name}</td>
                  <td className="px-4 py-2 text-[#475467]">{a.email}</td>
                  <td className="px-4 py-2 text-xs text-[#475467]">{a.gst_number}<br />{a.pan_number}</td>
                  <td className="px-4 py-2">
                    <div className="flex gap-2">
                      <a href={`${API}/admin/supplier-portal/${a._id}/documents/gst`} target="_blank" rel="noreferrer" className="text-[#0E7C86] text-xs flex items-center gap-1 underline" data-testid={`supplier-portal-approvals-gst-link-${a._id}`}>
                        <FileText size={12} /> GST
                      </a>
                      <a href={`${API}/admin/supplier-portal/${a._id}/documents/pan`} target="_blank" rel="noreferrer" className="text-[#0E7C86] text-xs flex items-center gap-1 underline" data-testid={`supplier-portal-approvals-pan-link-${a._id}`}>
                        <FileText size={12} /> PAN
                      </a>
                    </div>
                  </td>
                  <td className="px-4 py-2">
                    <Badge className={STATUS_BADGE[a.status]}>{a.status}</Badge>
                  </td>
                  <td className="px-4 py-2 text-right">
                    {a.status === "pending" && (
                      <div className="flex gap-2 justify-end">
                        <Button size="sm" onClick={() => approve(a._id)} className="bg-[#067647] hover:bg-[#05633b]" data-testid={`supplier-portal-approvals-approve-${a._id}`}>
                          <CheckCircle size={14} className="mr-1" /> Approve
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setRejectTarget(a._id)} className="border-[#FDA29B] text-[#B42318]" data-testid={`supplier-portal-approvals-reject-${a._id}`}>
                          <XCircle size={14} className="mr-1" /> Reject
                        </Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Dialog open={!!rejectTarget} onOpenChange={(o) => !o && setRejectTarget(null)}>
        <DialogContent data-testid="supplier-portal-approvals-reject-dialog">
          <DialogHeader>
            <DialogTitle>Reject Supplier Onboarding</DialogTitle>
            <DialogDescription>This reason will be shown to the vendor the next time they sign in.</DialogDescription>
          </DialogHeader>
          <Textarea
            placeholder="Reason (shown to the vendor)"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            data-testid="supplier-portal-approvals-reject-reason-input"
          />
          <div className="flex justify-end gap-2 mt-3">
            <Button variant="outline" onClick={() => setRejectTarget(null)}>Cancel</Button>
            <Button onClick={reject} className="bg-[#B42318] hover:bg-[#912018]" data-testid="supplier-portal-approvals-reject-confirm-button">Confirm Reject</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
