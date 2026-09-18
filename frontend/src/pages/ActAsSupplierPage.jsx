import { useState, useEffect, useMemo } from "react";
import "@/App.css";
import axios from "axios";
import { Buildings, Shield, MagnifyingGlass, Truck, X, Key, UserSwitch } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Toaster, toast } from "@/components/ui/sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandList, CommandEmpty, CommandGroup, CommandItem } from "@/components/ui/command";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  return String(detail);
}

const cartKey = (po) => `${po.po_number}::${po.item_number}`;

export default function ActAsSupplierPage() {
  const [accounts, setAccounts] = useState([]);
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [selectedAccount, setSelectedAccount] = useState(null);
  const [accountSearchOpen, setAccountSearchOpen] = useState(false);
  const [accountQuery, setAccountQuery] = useState("");

  const [pos, setPos] = useState([]);
  const [posLoading, setPosLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [cart, setCart] = useState({});
  const [reviewOpen, setReviewOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [successCode, setSuccessCode] = useState(null);

  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resetting, setResetting] = useState(false);

  useEffect(() => {
    axios.get(`${API}/admin/act-as-supplier/accounts`)
      .then(({ data }) => setAccounts(data.accounts || []))
      .catch((err) => toast.error("Could not load supplier accounts", { description: err?.response?.data?.detail || err.message }))
      .finally(() => setAccountsLoading(false));
  }, []);

  const accountMatches = useMemo(() => {
    const q = accountQuery.trim().toLowerCase();
    if (!q) return accounts.slice(0, 8);
    return accounts.filter((a) => a.vendor_code?.toLowerCase().includes(q) || a.company_name?.toLowerCase().includes(q) || a.email?.toLowerCase().includes(q)).slice(0, 8);
  }, [accountQuery, accounts]);

  const loadPos = async (accountId) => {
    setPosLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/act-as-supplier/${accountId}/purchase-orders`);
      setPos(data.purchase_orders || []);
    } catch (err) {
      toast.error("Could not load this supplier's Purchase Orders", { description: err?.response?.data?.detail || err.message });
      setPos([]);
    } finally {
      setPosLoading(false);
    }
  };

  const selectAccount = (account) => {
    setSelectedAccount(account);
    setAccountSearchOpen(false);
    setAccountQuery("");
    setCart({});
    setSearch("");
    setNewPassword("");
    setConfirmPassword("");
    loadPos(account._id);
  };

  const filteredPos = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return pos;
    return pos.filter((po) => [po.po_number, po.item_number, po.product_id, po.description].some((v) => (v || "").toString().toLowerCase().includes(q)));
  }, [pos, search]);

  const toggleCartItem = (po, checked) => {
    setCart((prev) => {
      const next = { ...prev };
      const key = cartKey(po);
      if (checked) {
        next[key] = { po_number: po.po_number, item_number: po.item_number, product_id: po.product_id, description: po.description, unit_of_measure: po.unit_of_measure, remaining_qty: po.remaining_qty, ship_qty: "" };
      } else {
        delete next[key];
      }
      return next;
    });
  };

  const updateCartQty = (key, value) => setCart((prev) => ({ ...prev, [key]: { ...prev[key], ship_qty: value } }));
  const removeFromCart = (key) => setCart((prev) => { const next = { ...prev }; delete next[key]; return next; });
  const cartItems = Object.entries(cart).map(([key, v]) => ({ key, ...v }));

  const submitShipment = async () => {
    setSubmitting(true);
    setSubmitError("");
    try {
      const items = cartItems.map((c) => ({ po_number: c.po_number, item_number: c.item_number, ship_qty: parseFloat(c.ship_qty) }));
      const { data } = await axios.post(`${API}/admin/act-as-supplier/${selectedAccount._id}/shipments`, { items });
      setCart({});
      setConfirmOpen(false);
      setReviewOpen(false);
      setSuccessCode(data._id);
      loadPos(selectedAccount._id);
    } catch (err) {
      setSubmitError(formatApiErrorDetail(err.response?.data?.detail));
    } finally {
      setSubmitting(false);
    }
  };

  const resetPassword = async () => {
    if (newPassword.length < 8) {
      toast.error("Password must be at least 8 characters");
      return;
    }
    if (newPassword !== confirmPassword) {
      toast.error("Passwords don't match");
      return;
    }
    setResetting(true);
    try {
      await axios.post(`${API}/admin/act-as-supplier/${selectedAccount._id}/reset-password`, { new_password: newPassword });
      toast.success(`New password emailed to ${selectedAccount.email}`);
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      toast.error("Could not reset password", { description: err?.response?.data?.detail || err.message });
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F5F6F7] font-sans" data-testid="act-as-supplier-page">
      <Toaster position="top-right" richColors />
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Act as Supplier</span>
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
          <UserSwitch size={18} weight="fill" className="text-[#0076CC]" />
          <h1 className="font-sans text-base font-bold text-[#111827]">Act as Supplier</h1>
        </div>
        <p className="text-sm text-[#5B738B]">Create a shipment on behalf of a supplier, or reset their portal password - every action here is tagged with your name for traceability.</p>

        <div className="mt-5 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-5 max-w-lg">
          <Label htmlFor="act-as-supplier-search" className="text-xs text-[#5B738B]">Select an Approved Supplier</Label>
          <Popover open={accountSearchOpen} onOpenChange={setAccountSearchOpen}>
            <PopoverTrigger asChild>
              <div className="relative mt-1">
                <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
                <Input
                  id="act-as-supplier-search"
                  value={selectedAccount ? `${selectedAccount.vendor_code} - ${selectedAccount.company_name}` : accountQuery}
                  onChange={(e) => { setSelectedAccount(null); setAccountQuery(e.target.value); setAccountSearchOpen(true); }}
                  onFocus={() => setAccountSearchOpen(true)}
                  placeholder={accountsLoading ? "Loading suppliers..." : "Search vendor code, company or email..."}
                  className="rounded-sm border-[#CBD3DB] pl-8"
                  data-testid="act-as-supplier-search-input"
                  autoComplete="off"
                />
              </div>
            </PopoverTrigger>
            <PopoverContent className="p-0 w-[--radix-popover-trigger-width]" align="start" onOpenAutoFocus={(e) => e.preventDefault()}>
              <Command shouldFilter={false}>
                <CommandList data-testid="act-as-supplier-search-results">
                  {accountMatches.length === 0 && <CommandEmpty>No approved supplier matches.</CommandEmpty>}
                  <CommandGroup>
                    {accountMatches.map((a) => (
                      <CommandItem key={a._id} value={a._id} onSelect={() => selectAccount(a)} className="cursor-pointer" data-testid={`act-as-supplier-option-${a._id}`}>
                        <span className="font-data font-semibold text-[#111827] mr-2">{a.vendor_code}</span>
                        <span className="text-[#5B738B] truncate">{a.company_name} · {a.email}</span>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </CommandList>
              </Command>
            </PopoverContent>
          </Popover>
        </div>

        {selectedAccount && (
          <>
            <div className="mt-6 flex items-center justify-between flex-wrap gap-3">
              <h2 className="font-heading text-lg font-bold text-[#0F172A]">
                {selectedAccount.company_name}'s Open Purchase Orders
              </h2>
              <div className="relative">
                <MagnifyingGlass size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#475569]" />
                <Input
                  placeholder="Search PO #, Item #, or description..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="h-8 pl-8 pr-2 w-72 text-[13px] rounded-sm border-[#E2E8F0]"
                  data-testid="act-as-supplier-po-search-input"
                />
              </div>
            </div>

            {posLoading && <div className="mt-4 text-sm text-[#475569]" data-testid="act-as-supplier-po-loading">Loading Purchase Orders...</div>}

            {!posLoading && filteredPos.length === 0 && (
              <div className="mt-4 bg-white border border-[#E2E8F0] rounded-sm shadow-sm p-6 text-center text-sm text-[#475569]" data-testid="act-as-supplier-po-empty">
                No open Purchase Orders for this supplier right now.
              </div>
            )}

            {!posLoading && filteredPos.length > 0 && (
              <div className="mt-3 bg-white border border-[#E2E8F0] rounded-sm shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto" data-testid="act-as-supplier-po-table">
                <table className="w-full text-[13px] border-collapse">
                  <thead className="bg-[#F1F5F9] text-[#344054] text-xs font-bold font-heading uppercase tracking-wide">
                    <tr>
                      <th className="border border-[#E2E8F0] p-1.5 w-8"></th>
                      <th className="border border-[#E2E8F0] p-1.5 text-left">PO Number</th>
                      <th className="border border-[#E2E8F0] p-1.5 text-left">Item Code</th>
                      <th className="border border-[#E2E8F0] p-1.5 text-left">Description</th>
                      <th className="border border-[#E2E8F0] p-1.5 text-right">Open Qty</th>
                      <th className="border border-[#E2E8F0] p-1.5 text-right w-28">Ship Qty</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredPos.filter((po) => po.remaining_qty > 0).map((po, i) => {
                      const key = cartKey(po);
                      const inCart = !!cart[key];
                      return (
                        <tr key={i} className="bg-white odd:bg-[#F9FAFB]" data-testid={`act-as-supplier-po-row-${po.po_number}-${po.item_number}`}>
                          <td className="border border-[#E2E8F0] px-2 py-1">
                            <Checkbox checked={inCart} onCheckedChange={(c) => toggleCartItem(po, !!c)} data-testid={`act-as-supplier-po-checkbox-${po.po_number}-${po.item_number}`} />
                          </td>
                          <td className="border border-[#E2E8F0] px-2 py-1 font-data">{po.po_number}</td>
                          <td className="border border-[#E2E8F0] px-2 py-1 font-data text-xs">{po.product_id || "—"}</td>
                          <td className="border border-[#E2E8F0] px-2 py-1">{po.description || "-"}</td>
                          <td className="border border-[#E2E8F0] px-2 py-1 text-right font-data">{po.remaining_qty} {po.unit_of_measure}</td>
                          <td className="border border-[#E2E8F0] px-2 py-1 text-right">
                            <Input
                              type="number"
                              disabled={!inCart}
                              value={inCart ? cart[key].ship_qty : ""}
                              onChange={(e) => updateCartQty(key, e.target.value)}
                              className="h-7 w-24 text-right rounded-sm border-[#E2E8F0] text-xs"
                              data-testid={`act-as-supplier-po-qty-input-${po.po_number}-${po.item_number}`}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {cartItems.length > 0 && (
              <div className="fixed bottom-0 left-0 right-0 md:left-60 bg-white border-t border-[#E2E8F0] shadow-[0_-2px_8px_0_rgba(16,24,40,0.08)] px-4 py-3 flex items-center justify-between z-20" data-testid="act-as-supplier-cart-bar">
                <span className="text-sm text-[#0F172A] font-semibold" data-testid="act-as-supplier-cart-count">{cartItems.length} item{cartItems.length > 1 ? "s" : ""} selected</span>
                <Button onClick={() => setReviewOpen(true)} className="rounded-sm bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150" data-testid="act-as-supplier-review-shipment-button">
                  <Truck size={14} className="mr-1" /> Review Shipment
                </Button>
              </div>
            )}

            <div className="mt-8 bg-white border border-[#CBD3DB] rounded-sm shadow-sm p-5 max-w-lg" data-testid="act-as-supplier-reset-password-card">
              <div className="flex items-center gap-2 mb-3">
                <Key size={16} weight="fill" className="text-[#0076CC]" />
                <h2 className="font-sans text-sm font-bold text-[#111827]">Reset Password for {selectedAccount.company_name}</h2>
              </div>
              <div className="space-y-3">
                <div>
                  <Label htmlFor="act-as-supplier-new-password" className="text-xs text-[#5B738B]">New Password</Label>
                  <Input
                    id="act-as-supplier-new-password"
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="At least 8 characters"
                    className="rounded-sm border-[#CBD3DB] mt-1"
                    data-testid="act-as-supplier-new-password-input"
                  />
                </div>
                <div>
                  <Label htmlFor="act-as-supplier-confirm-password" className="text-xs text-[#5B738B]">Confirm Password</Label>
                  <Input
                    id="act-as-supplier-confirm-password"
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-type the password"
                    className="rounded-sm border-[#CBD3DB] mt-1"
                    data-testid="act-as-supplier-confirm-password-input"
                  />
                </div>
                <p className="text-[11px] text-[#98A2B3]">The new password is emailed directly to {selectedAccount.email}.</p>
                <Button
                  onClick={resetPassword}
                  disabled={resetting || !newPassword || !confirmPassword}
                  className="rounded-sm bg-[#0076CC] hover:bg-[#005A9E] transition-colors duration-150"
                  data-testid="act-as-supplier-reset-password-button"
                >
                  {resetting ? "Setting & Emailing..." : "Set & Email New Password"}
                </Button>
              </div>
            </div>
          </>
        )}
      </div>

      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent className="rounded-sm max-w-3xl" data-testid="act-as-supplier-review-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Review Shipment (on behalf of {selectedAccount?.company_name})</DialogTitle>
            <DialogDescription>Confirm the items and quantities before generating the shipment code.</DialogDescription>
          </DialogHeader>
          <div className="max-h-72 overflow-y-auto border border-[#E2E8F0] rounded-sm">
            <table className="w-full text-xs">
              <thead className="bg-[#F8FAFC] sticky top-0">
                <tr>
                  <th className="border-b border-[#E2E8F0] p-1.5 text-left">PO / Item</th>
                  <th className="border-b border-[#E2E8F0] p-1.5 text-left">Description</th>
                  <th className="border-b border-[#E2E8F0] p-1.5 text-right">Open Qty</th>
                  <th className="border-b border-[#E2E8F0] p-1.5 text-right">Ship Qty</th>
                  <th className="border-b border-[#E2E8F0] p-1.5"></th>
                </tr>
              </thead>
              <tbody>
                {cartItems.map((c) => (
                  <tr key={c.key} className="border-t border-[#E2E8F0]" data-testid={`act-as-supplier-review-row-${c.key}`}>
                    <td className="px-1.5 py-1.5 font-data whitespace-nowrap">PO {c.po_number} · {c.item_number}</td>
                    <td className="px-1.5 py-1.5">{c.description || "—"}</td>
                    <td className="px-1.5 py-1.5 text-right font-data text-[#475569]">{c.remaining_qty ?? "—"} {c.unit_of_measure}</td>
                    <td className="px-1.5 py-1.5">
                      <div className="flex items-center justify-end gap-1">
                        <Input type="number" value={c.ship_qty} onChange={(e) => updateCartQty(c.key, e.target.value)} className="h-8 w-20 text-right rounded-sm border-[#E2E8F0] text-xs" data-testid={`act-as-supplier-review-qty-input-${c.key}`} />
                        <span className="text-[#475569] w-8">{c.unit_of_measure}</span>
                      </div>
                    </td>
                    <td className="px-1.5 py-1.5 text-center">
                      <button onClick={() => removeFromCart(c.key)} className="text-[#991B1B] hover:bg-[#991B1B]/10 rounded-sm p-1" data-testid={`act-as-supplier-review-remove-${c.key}`}>
                        <X size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setReviewOpen(false)}>Cancel</Button>
            <Button onClick={() => setConfirmOpen(true)} disabled={cartItems.length === 0} className="rounded-sm bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150" data-testid="act-as-supplier-review-confirm-button">
              Confirm Shipment
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="rounded-sm" data-testid="act-as-supplier-confirm-generate-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Generate Shipment Code?</DialogTitle>
            <DialogDescription>
              This creates one shipment covering {cartItems.length} item{cartItems.length > 1 ? "s" : ""} on behalf of {selectedAccount?.company_name} - flagged with your name for traceability. Are you sure?
            </DialogDescription>
          </DialogHeader>
          {submitError && <div className="text-sm text-[#991B1B]" data-testid="act-as-supplier-submit-error">{submitError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-sm" onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button onClick={submitShipment} disabled={submitting} className="rounded-sm bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150" data-testid="act-as-supplier-confirm-generate-button">
              {submitting ? "Generating..." : "Yes, Generate Code"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={!!successCode} onOpenChange={(o) => !o && setSuccessCode(null)}>
        <DialogContent className="rounded-sm" data-testid="act-as-supplier-success-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Shipment Created</DialogTitle>
            <DialogDescription>Share this code with {selectedAccount?.company_name} for their shipment paperwork - it's how the warehouse matches it on arrival.</DialogDescription>
          </DialogHeader>
          <div className="text-center py-4 bg-[#F8FAFC] border border-[#E2E8F0] rounded-sm">
            <div className="text-3xl font-data font-bold tracking-widest text-[#1E40AF]" data-testid="act-as-supplier-success-code">{successCode}</div>
          </div>
          <Button onClick={() => setSuccessCode(null)} className="rounded-sm bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150" data-testid="act-as-supplier-success-close-button">Done</Button>
        </DialogContent>
      </Dialog>
    </div>
  );
}
