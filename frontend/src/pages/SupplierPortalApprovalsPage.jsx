import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { CheckCircle, XCircle, FileText, Buildings, Shield, Clock } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { useAuth } from "@/contexts/AuthContext";

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

const ADDITIONAL_DOC_TYPES = [
  { key: "msme", label: "MSME" },
  { key: "bank", label: "Bank Details" },
];

export default function SupplierPortalApprovalsPage() {
  const { hasPageAccess } = useAuth();
  const canApprove = hasPageAccess("supplier_portal_admin");
  const [status, setStatus] = useState("pending");
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [rejectTarget, setRejectTarget] = useState(null);
  const [rejectReason, setRejectReason] = useState("");
  const [historyModal, setHistoryModal] = useState(null); // { accountId, docType, history }

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
      <Toaster position="top-right" richColors />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Supplier Portal Approvals</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>
      <div className="max-w-[1400px] mx-auto p-4 md:p-6">
        <div className="flex items-center gap-2 mb-1">
          <Buildings size={18} weight="fill" className="text-[#0076CC]" />
          <h1 className="font-sans text-base font-bold text-[#111827]">Supplier Portal Approvals</h1>
        </div>
        <p className="text-sm text-[#5B738B]">
          {canApprove
            ? "Review external vendor onboarding requests before they can sign in and view their Purchase Orders."
            : "View-only access to supplier documents."}
        </p>

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
                {status === "approved" && <th className="text-left px-3 py-2 font-semibold">Additional Documents</th>}
                <th className="text-left px-3 py-2 font-semibold">Status</th>
                {canApprove && <th className="text-right px-3 py-2 font-semibold">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={8} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-portal-approvals-loading">Loading...</td></tr>
              )}
              {!loading && accounts.length === 0 && (
                <tr><td colSpan={8} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-portal-approvals-empty">No {status} accounts.</td></tr>
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
                  {status === "approved" && (
                    <td className="px-3 py-2">
                      <div className="flex flex-col gap-1">
                        {ADDITIONAL_DOC_TYPES.map(({ key, label }) => {
                          const doc = a[`${key}_documents`] || [];
                          const latest = doc[doc.length - 1];
                          return (
                            <div key={key} className="flex items-center gap-1.5 text-xs">
                              {latest ? (
                                <a href={`${API}/admin/supplier-portal/${a._id}/documents/${key}`} target="_blank" rel="noreferrer" className="text-[#0076CC] flex items-center gap-1 underline" data-testid={`supplier-portal-approvals-${key}-link-${a._id}`}>
                                  <FileText size={12} /> {label}
                                </a>
                              ) : (
                                <span className="text-[#98A2B3] flex items-center gap-1"><FileText size={12} /> {label} - none</span>
                              )}
                              {doc.length > 1 && (
                                <button
                                  onClick={() => setHistoryModal({ accountId: a._id, docType: key, label, history: doc })}
                                  className="text-[#5B738B] hover:underline flex items-center gap-0.5"
                                  data-testid={`supplier-portal-approvals-${key}-history-${a._id}`}
                                >
                                  <Clock size={11} /> ({doc.length})
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </td>
                  )}
                  <td className="px-3 py-2">
                    <Badge className={STATUS_BADGE[a.status]}>{a.status}</Badge>
                  </td>
                  {canApprove && (
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
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Dialog open={!!historyModal} onOpenChange={(o) => !o && setHistoryModal(null)}>
        <DialogContent className="max-w-md rounded-sm" data-testid="supplier-portal-approvals-history-dialog">
          <DialogHeader>
            <DialogTitle className="font-sans">{historyModal?.label} - Version History</DialogTitle>
            <DialogDescription>Older versions are kept for reference.</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {historyModal?.history?.slice().reverse().map((rev) => (
              <a
                key={rev.version}
                href={`${API}/admin/supplier-portal/${historyModal.accountId}/documents/${historyModal.docType}?version=${rev.version}`}
                target="_blank" rel="noreferrer"
                className="flex items-center justify-between text-sm border border-[#CBD3DB] rounded-sm p-2 hover:bg-[#F5F6F7]"
                data-testid={`supplier-portal-approvals-history-item-${rev.version}`}
              >
                <span>v{rev.version} - {rev.filename}</span>
                <span className="text-xs text-[#5B738B]">{rev.uploaded_at ? new Date(rev.uploaded_at).toLocaleDateString("en-IN") : ""}</span>
              </a>
            ))}
          </div>
        </DialogContent>
      </Dialog>

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
