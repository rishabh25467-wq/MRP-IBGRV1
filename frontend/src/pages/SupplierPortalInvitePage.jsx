import { useState, useEffect, useMemo } from "react";
import "@/App.css";
import axios from "axios";
import { PaperPlaneTilt, Buildings, Shield, EnvelopeSimple, Warning, ArrowsClockwise, MagnifyingGlass, X, Plus } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Toaster, toast } from "@/components/ui/sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandList, CommandEmpty, CommandGroup, CommandItem } from "@/components/ui/command";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const SIGNUP_STATUS_BADGE = {
  not_signed_up: { label: "Not signed up yet", className: "bg-[#98A2B3]/15 text-[#5B738B] rounded-sm" },
  pending: { label: "Pending Approval", className: "bg-[#E3A008]/15 text-[#8A6116] rounded-sm" },
  approved: { label: "Approved", className: "bg-[#10B981]/15 text-[#0B7A56] rounded-sm" },
  rejected: { label: "Rejected", className: "bg-[#E02424]/10 text-[#B91C1C] rounded-sm" },
};

export default function SupplierPortalInvitePage() {
  const [companyName, setCompanyName] = useState("");
  const [vendorCode, setVendorCode] = useState("");
  const [emails, setEmails] = useState([]);
  const [emailInput, setEmailInput] = useState("");
  const [sending, setSending] = useState(false);
  const [invites, setInvites] = useState([]);
  const [loading, setLoading] = useState(true);
  const [resendingId, setResendingId] = useState(null);
  const [confirmedDuplicate, setConfirmedDuplicate] = useState(false);

  // Sep 3 2026, user's explicit ask: "search SAP vendor code, autofill
  // other details" - the local `suppliers` collection (synced from SAP,
  // see SupplierMasterPage's "Sync from SAP") already has vendor
  // code/name/email for ~3000 vendors, fetched once here the same way
  // SupplierMasterPage does.
  const [suppliers, setSuppliers] = useState([]);
  const [vendorSearchOpen, setVendorSearchOpen] = useState(false);
  const [vendorQuery, setVendorQuery] = useState("");
  // Sep 3 2026 BUG FOUND + FIXED (user report: "when I change supplier
  // code, old email still shows of other vendor") - selectVendor only
  // ever ADDED the new vendor's on-file email when the chip list was
  // empty, so switching to a second vendor left the first vendor's
  // auto-filled email sitting there unchanged. Track which email (if
  // any) was auto-filled from the CURRENTLY selected vendor, so
  // switching vendors removes exactly that one and adds the new
  // vendor's - any email the user typed in manually is left alone.
  const [autoFilledEmail, setAutoFilledEmail] = useState(null);

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
    axios.get(`${API}/suppliers`).then(({ data }) => setSuppliers(data || [])).catch(() => setSuppliers([]));
  }, []);

  const vendorMatches = useMemo(() => {
    const q = vendorQuery.trim().toLowerCase();
    if (!q) return [];
    return suppliers
      .filter((s) => s.sap_internal_id?.toLowerCase().includes(q) || s.name?.toLowerCase().includes(q))
      .slice(0, 8);
  }, [vendorQuery, suppliers]);

  const selectVendor = (supplier) => {
    setCompanyName(supplier.name);
    setVendorCode(supplier.sap_internal_id || "");
    const newEmail = supplier.email ? supplier.email.toLowerCase() : null;
    setEmails((prev) => {
      const withoutOld = autoFilledEmail ? prev.filter((e) => e !== autoFilledEmail) : prev;
      if (!newEmail || withoutOld.includes(newEmail)) return withoutOld;
      return [...withoutOld, newEmail];
    });
    setAutoFilledEmail(newEmail);
    setVendorSearchOpen(false);
    setVendorQuery("");
  };

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

  const addEmail = () => {
    const candidate = emailInput.trim().toLowerCase();
    if (!candidate) return;
    if (!EMAIL_RE.test(candidate)) {
      toast.error(`"${candidate}" doesn't look like a valid email`);
      return;
    }
    if (emails.includes(candidate)) {
      setEmailInput("");
      return;
    }
    setEmails([...emails, candidate]);
    setEmailInput("");
  };

  const removeEmail = (candidate) => setEmails(emails.filter((e) => e !== candidate));

  const sendInvite = async (e) => {
    e.preventDefault();
    if (emails.length === 0) {
      toast.error("Add at least one email address");
      return;
    }
    if (existingInviteForCode && !confirmedDuplicate) {
      setConfirmedDuplicate(true);
      return;
    }
    setSending(true);
    const succeeded = [];
    const failed = [];
    for (const recipient of emails) {
      try {
        await axios.post(`${API}/admin/supplier-portal/invites`, { company_name: companyName, vendor_code: vendorCode, email: recipient });
        succeeded.push(recipient);
      } catch (err) {
        failed.push({ email: recipient, detail: err?.response?.data?.detail || err.message });
      }
    }
    setSending(false);
    if (succeeded.length > 0) {
      toast.success(`Invite sent to ${succeeded.length} email${succeeded.length > 1 ? "s" : ""}: ${succeeded.join(", ")}`);
    }
    failed.forEach((f) => toast.error(`Could not invite ${f.email}`, { description: f.detail }));
    if (failed.length === 0) {
      setCompanyName("");
      setVendorCode("");
      setEmails([]);
      setAutoFilledEmail(null);
      setConfirmedDuplicate(false);
    } else {
      setEmails(failed.map((f) => f.email));
    }
    loadInvites();
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

      <div className="max-w-[1400px] mx-auto p-4 md:p-6">
        <div className="flex items-center gap-2 mb-1">
          <Buildings size={18} weight="fill" className="text-[#0076CC]" />
          <h1 className="font-sans text-base font-bold text-[#111827]">Invite Supplier</h1>
        </div>
        <p className="text-sm text-[#5B738B]">Send a supplier an email with their Vendor Code and the Supplier Portal signup link - sent from your own mailbox.</p>

        <form onSubmit={sendInvite} className="mt-5 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-5 space-y-4">
          <div>
            <Label htmlFor="invite-vendor-search" className="text-xs text-[#5B738B]">Search SAP Vendor</Label>
            <Popover open={vendorSearchOpen} onOpenChange={setVendorSearchOpen}>
              <PopoverTrigger asChild>
                <div className="relative mt-1">
                  <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                  <Input
                    id="invite-vendor-search"
                    value={vendorQuery}
                    onChange={(e) => { setVendorQuery(e.target.value); setVendorSearchOpen(true); }}
                    onFocus={() => setVendorSearchOpen(true)}
                    placeholder="Type a vendor code or company name, e.g. H1330"
                    className="rounded-sm border-[#CBD3DB] pl-8 font-data"
                    data-testid="supplier-invite-vendor-search-input"
                    autoComplete="off"
                  />
                </div>
              </PopoverTrigger>
              <PopoverContent className="p-0 w-[--radix-popover-trigger-width]" align="start" onOpenAutoFocus={(e) => e.preventDefault()}>
                <Command shouldFilter={false}>
                  <CommandList data-testid="supplier-invite-vendor-search-results">
                    {vendorQuery.trim() && vendorMatches.length === 0 && (
                      <CommandEmpty>No SAP vendor matches "{vendorQuery}"</CommandEmpty>
                    )}
                    <CommandGroup>
                      {vendorMatches.map((s) => (
                        <CommandItem
                          key={s.id}
                          value={s.sap_internal_id}
                          onSelect={() => selectVendor(s)}
                          className="cursor-pointer"
                          data-testid={`supplier-invite-vendor-option-${s.sap_internal_id}`}
                        >
                          <span className="font-data font-semibold text-[#111827] mr-2">{s.sap_internal_id}</span>
                          <span className="text-[#5B738B] truncate">{s.name}</span>
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
            <p className="text-[11px] text-[#98A2B3] mt-1">Selecting a vendor autofills Company Name and Vendor Code below.</p>
          </div>

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
            <Label htmlFor="invite-email" className="text-xs text-[#5B738B]">Supplier Email(s)</Label>
            <div className="flex gap-2 mt-1">
              <Input
                id="invite-email"
                type="email"
                value={emailInput}
                onChange={(e) => setEmailInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addEmail(); } }}
                placeholder="contact@supplier.com - press Enter to add"
                className="rounded-sm border-[#CBD3DB]"
                data-testid="supplier-invite-email-input"
              />
              <Button
                type="button"
                variant="outline"
                onClick={addEmail}
                className="rounded-sm border-[#0076CC]/40 text-[#0076CC] shrink-0"
                data-testid="supplier-invite-add-email-button"
              >
                <Plus size={14} className="mr-1" /> Add
              </Button>
            </div>
            {emails.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-2" data-testid="supplier-invite-email-chips">
                {emails.map((em) => (
                  <span
                    key={em}
                    className="inline-flex items-center gap-1 bg-[#0076CC]/10 text-[#0076CC] text-xs rounded-sm px-2 py-1"
                    data-testid={`supplier-invite-email-chip-${em}`}
                  >
                    {em}
                    <button type="button" onClick={() => removeEmail(em)} className="hover:text-[#B91C1C]" data-testid={`supplier-invite-remove-email-${em}`}>
                      <X size={12} weight="bold" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {emails.length === 0 && <p className="text-[11px] text-[#98A2B3] mt-1">Add one or more recipients - a separate invite is sent to each.</p>}
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
            {sending ? "Sending..." : existingInviteForCode ? "Send Anyway" : `Send Invite${emails.length > 1 ? `s (${emails.length})` : ""}`}
          </Button>
        </form>

        <h2 className="font-sans text-sm font-bold text-[#111827] mt-8 mb-2">Invite History</h2>
        <div className="bg-white border border-[#CBD3DB] rounded-sm shadow-sm overflow-x-auto">
          <table className="w-full text-sm border-collapse min-w-[900px]">
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
