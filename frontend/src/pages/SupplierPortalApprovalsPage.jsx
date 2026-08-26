import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { CheckCircle, XCircle, FileText, Buildings } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
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
  pending: "bg-[#E3A008]/15 text-[#8A6116] rounded-sm",
  approved: "bg-[#10B981]/15 text-[#0B7A56] rounded-sm",
  rejected: "bg-[#E02424]/10 text-[#B91C1C] rounded-sm",
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
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="supplier-portal-approvals-page">
      <NavTabs />
      <Toaster position="top-right" richColors />
      <div className="max-w-6xl mx-auto p-4 md:p-6">
        <div className="flex items-center gap-2 mb-1">
          <Buildings size={18} weight="fill" className="text-[#0076CC]" />
          <h1 className="font-sans text-base font-bold text-[#111827]">Supplier Portal Approvals</h1>
        </div>
        <p className="text-sm text-[#5B738B]">Review external vendor onboarding requests before they can sign in and view their Purchase Orders.</p>

        <div className="flex items-center gap-4 mt-5 border-b border-[#CBD3DB]">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setStatus(t.key)}
              className={`px-1 pb-2 text-sm font-semibold font-sans border-b-2 transition-colors duration-150 ${
                status === t.key ? "border-[#0076CC] text-[#0076CC]" : "border-transparent text-[#5B738B] hover:text-[#111827]"
              }`}
              data-testid={`supplier-portal-approvals-tab-${t.key}`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="mt-4 bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
              <tr>
                <th className="text-left px-3 py-2 font-semibold">Vendor Code</th>
                <th className="text-left px-3 py-2 font-semibold">Company</th>
                <th className="text-left px-3 py-2 font-semibold">Email</th>
                <th className="text-left px-3 py-2 font-semibold">GST / PAN</th>
                <th className="text-left px-3 py-2 font-semibold">Documents</th>
                <th className="text-left px-3 py-2 font-semibold">Status</th>
                <th className="text-right px-3 py-2 font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={7} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-portal-approvals-loading">Loading...</td></tr>
              )}
              {!loading && accounts.length === 0 && (
                <tr><td colSpan={7} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-portal-approvals-empty">No {status} accounts.</td></tr>
              )}
              {!loading && accounts.map((a) => (
                <tr key={a._id} className="border-b border-[#CBD3DB]" data-testid={`supplier-portal-approvals-row-${a._id}`}>
                  <td className="px-3 py-2 font-data font-medium">{a.vendor_code}</td>
                  <td className="px-3 py-2">{a.company_name}</td>
                  <td className="px-3 py-2 text-[#5B738B]">{a.email}</td>
                  <td className="px-3 py-2 text-xs text-[#5B738B] font-data">{a.gst_number}<br />{a.pan_number}</td>
                  <td className="px-3 py-2">
                    <div className="flex gap-2">
                      <a href={`${API}/admin/supplier-portal/${a._id}/documents/gst`} target="_blank" rel="noreferrer" className="text-[#0076CC] text-xs flex items-center gap-1 underline" data-testid={`supplier-portal-approvals-gst-link-${a._id}`}>
                        <FileText size={12} /> GST
                      </a>
                      <a href={`${API}/admin/supplier-portal/${a._id}/documents/pan`} target="_blank" rel="noreferrer" className="text-[#0076CC] text-xs flex items-center gap-1 underline" data-testid={`supplier-portal-approvals-pan-link-${a._id}`}>
                        <FileText size={12} /> PAN
                      </a>
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <Badge className={STATUS_BADGE[a.status]}>{a.status}</Badge>
                  </td>
                  <td className="px-3 py-2 text-right">
                    {a.status === "pending" && (
                      <div className="flex gap-2 justify-end">
                        <Button size="sm" onClick={() => approve(a._id)} className="rounded-sm bg-[#10B981] hover:bg-[#0B7A56] transition-colors duration-150" data-testid={`supplier-portal-approvals-approve-${a._id}`}>
                          <CheckCircle size={14} className="mr-1" /> Approve
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setRejectTarget(a._id)} className="rounded-sm border-[#E02424]/40 text-[#B91C1C]" data-testid={`supplier-portal-approvals-reject-${a._id}`}>
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
        <DialogContent className="rounded-sm" data-testid="supplier-portal-approvals-reject-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">Reject Supplier Onboarding</DialogTitle>
          </DialogHeader>
          <Textarea
            placeholder="Reason (shown to the vendor)"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            className="rounded-sm border-[#CBD3DB]"
            data-testid="supplier-portal-approvals-reject-reason-input"
          />
          <div className="flex justify-end gap-2 mt-3">
            <Button variant="outline" className="rounded-sm" onClick={() => setRejectTarget(null)}>Cancel</Button>
            <Button onClick={reject} className="rounded-sm bg-[#E02424] hover:bg-[#B91C1C] transition-colors duration-150" data-testid="supplier-portal-approvals-reject-confirm-button">Confirm Reject</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
