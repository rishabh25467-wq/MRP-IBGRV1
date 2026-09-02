import { useState, useEffect } from "react";
import "@/App.css";
import axios from "axios";
import { PaperPlaneTilt, Buildings, Shield, EnvelopeSimple, Warning, ArrowsClockwise } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const SIGNUP_STATUS_BADGE = {
  not_signed_up: { label: "Not signed up yet", className: "bg-[#98A2B3]/15 text-[#5B738B] rounded-sm" },
  pending: { label: "Pending Approval", className: "bg-[#E3A008]/15 text-[#8A6116] rounded-sm" },
  approved: { label: "Approved", className: "bg-[#10B981]/15 text-[#0B7A56] rounded-sm" },
  rejected: { label: "Rejected", className: "bg-[#E02424]/10 text-[#B91C1C] rounded-sm" },
};

export default function SupplierPortalInvitePage() {
  const [companyName, setCompanyName] = useState("");
  const [vendorCode, setVendorCode] = useState("");
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [invites, setInvites] = useState([]);
  const [loading, setLoading] = useState(true);
  const [resendingId, setResendingId] = useState(null);
  const [confirmedDuplicate, setConfirmedDuplicate] = useState(false);

  const loadInvites = async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/supplier-portal/invites`);
      setInvites(data.invites || []);
    } catch (err) {
      toast.error("Could not load invite history", { description: err?.response?.data?.detail || err.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInvites();
  }, []);

  // User's explicit ask: warn before re-inviting a vendor code that's
  // already been sent an invite before - avoids emailing the same
  // supplier twice by mistake. Checked against the already-loaded
  // invite history, no extra backend call needed.
  const existingInviteForCode = vendorCode.trim()
    ? invites.find((inv) => inv.vendor_code.toUpperCase() === vendorCode.trim().toUpperCase())
    : null;

  useEffect(() => {
    setConfirmedDuplicate(false);
  }, [vendorCode]);

  const sendInvite = async (e) => {
    e.preventDefault();
    if (existingInviteForCode && !confirmedDuplicate) {
      setConfirmedDuplicate(true);
      return;
    }
    setSending(true);
    try {
      await axios.post(`${API}/admin/supplier-portal/invites`, { company_name: companyName, vendor_code: vendorCode, email });
      toast.success(`Invite sent to ${email}`);
      setCompanyName("");
      setVendorCode("");
      setEmail("");
      setConfirmedDuplicate(false);
      loadInvites();
    } catch (err) {
      toast.error("Could not send invite", { description: err?.response?.data?.detail || err.message });
    } finally {
      setSending(false);
    }
  };

  const resendInvite = async (invite) => {
    setResendingId(invite._id);
    try {
      await axios.post(`${API}/admin/supplier-portal/invites/${invite._id}/resend`);
      toast.success(`Invite resent to ${invite.email}`);
      loadInvites();
    } catch (err) {
      toast.error("Could not resend invite", { description: err?.response?.data?.detail || err.message });
    } finally {
      setResendingId(null);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="supplier-portal-invite-page">
      <Toaster position="top-right" richColors />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Invite Supplier</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
      </header>

      <div className="max-w-3xl mx-auto p-4 md:p-6">
        <div className="flex items-center gap-2 mb-1">
          <Buildings size={18} weight="fill" className="text-[#0076CC]" />
          <h1 className="font-sans text-base font-bold text-[#111827]">Invite Supplier</h1>
        </div>
        <p className="text-sm text-[#5B738B]">Send a supplier an email with their Vendor Code and the Supplier Portal signup link - sent from your own mailbox.</p>

        <form onSubmit={sendInvite} className="mt-5 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-5 space-y-4">
          <div>
            <Label htmlFor="invite-company-name" className="text-xs text-[#5B738B]">Company Name</Label>
            <Input
              id="invite-company-name"
              required
              value={companyName}
              onChange={(e) => setCompanyName(e.target.value)}
              placeholder="e.g. HAMIDI EXPORTS"
              className="rounded-sm border-[#CBD3DB] mt-1"
              data-testid="supplier-invite-company-name-input"
            />
          </div>
          <div>
            <Label htmlFor="invite-vendor-code" className="text-xs text-[#5B738B]">SAP Vendor Code</Label>
            <Input
              id="invite-vendor-code"
              required
              value={vendorCode}
              onChange={(e) => setVendorCode(e.target.value)}
              placeholder="e.g. H1330"
              className="rounded-sm border-[#CBD3DB] mt-1 font-data"
              data-testid="supplier-invite-vendor-code-input"
            />
          </div>
          <div>
            <Label htmlFor="invite-email" className="text-xs text-[#5B738B]">Supplier Email</Label>
            <Input
              id="invite-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="contact@supplier.com"
              className="rounded-sm border-[#CBD3DB] mt-1"
              data-testid="supplier-invite-email-input"
            />
          </div>

          {existingInviteForCode && (
            <div className="flex items-start gap-2 bg-[#E3A008]/10 border border-[#E3A008]/30 rounded-sm px-3 py-2" data-testid="supplier-invite-duplicate-warning">
              <Warning size={16} weight="fill" className="text-[#8A6116] mt-0.5 shrink-0" />
              <p className="text-xs text-[#8A6116]">
                Vendor code <span className="font-data font-semibold">{existingInviteForCode.vendor_code}</span> was already invited on{" "}
                {new Date(existingInviteForCode.created_at).toLocaleDateString()} to <span className="font-medium">{existingInviteForCode.email}</span>.
                {confirmedDuplicate ? " Click \"Send Anyway\" to send again." : " Click \"Send Invite\" again to confirm."}
              </p>
            </div>
          )}

          <Button
            type="submit"
            disabled={sending}
            className="rounded-sm bg-[#0076CC] hover:bg-[#005A9E] transition-colors duration-150"
            data-testid="supplier-invite-send-button"
          >
            <PaperPlaneTilt size={14} className="mr-1.5" />
            {sending ? "Sending..." : existingInviteForCode ? "Send Anyway" : "Send Invite"}
          </Button>
        </form>

        <h2 className="font-sans text-sm font-bold text-[#111827] mt-8 mb-2">Invite History</h2>
        <div className="bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead className="bg-[#F5F6F7] text-[#5B738B] text-xs uppercase">
              <tr>
                <th className="text-left px-3 py-2 font-semibold">Company</th>
                <th className="text-left px-3 py-2 font-semibold">Vendor Code</th>
                <th className="text-left px-3 py-2 font-semibold">Email</th>
                <th className="text-left px-3 py-2 font-semibold">Invited By</th>
                <th className="text-left px-3 py-2 font-semibold">Sent</th>
                <th className="text-left px-3 py-2 font-semibold">Signup Status</th>
                <th className="text-right px-3 py-2 font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={7} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-invite-history-loading">Loading...</td></tr>
              )}
              {!loading && invites.length === 0 && (
                <tr><td colSpan={7} className="px-3 py-6 text-center text-[#5B738B]" data-testid="supplier-invite-history-empty">No invites sent yet.</td></tr>
              )}
              {!loading && invites.map((inv) => {
                const badge = SIGNUP_STATUS_BADGE[inv.signup_status] || SIGNUP_STATUS_BADGE.not_signed_up;
                return (
                  <tr key={inv._id} className="border-b border-[#CBD3DB]" data-testid={`supplier-invite-history-row-${inv._id}`}>
                    <td className="px-3 py-2">{inv.company_name}</td>
                    <td className="px-3 py-2 font-data font-medium">{inv.vendor_code}</td>
                    <td className="px-3 py-2 text-[#5B738B] flex items-center gap-1"><EnvelopeSimple size={12} /> {inv.email}</td>
                    <td className="px-3 py-2 text-[#5B738B]">{inv.invited_by}</td>
                    <td className="px-3 py-2 text-[#5B738B]">
                      {new Date(inv.last_sent_at || inv.created_at).toLocaleString()}
                      {inv.resend_count > 0 && <span className="text-[10px] text-[#98A2B3]"> (resent {inv.resend_count}x)</span>}
                    </td>
                    <td className="px-3 py-2"><Badge className={badge.className}>{badge.label}</Badge></td>
                    <td className="px-3 py-2 text-right">
                      {inv.signup_status === "not_signed_up" && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={resendingId === inv._id}
                          onClick={() => resendInvite(inv)}
                          className="rounded-sm border-[#0076CC]/40 text-[#0076CC]"
                          data-testid={`supplier-invite-resend-button-${inv._id}`}
                        >
                          <ArrowsClockwise size={13} className="mr-1" /> {resendingId === inv._id ? "Resending..." : "Resend"}
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

