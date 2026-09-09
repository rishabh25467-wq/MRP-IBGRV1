import { useState, useEffect } from "react";
import axios from "axios";
import { Shield, ShieldCheck, MapPin } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { useAuth, PAGE_LABELS } from "@/contexts/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const PAGE_KEYS = Object.keys(PAGE_LABELS);

// Sep 5 2026, user's explicit ask: group the permission checkboxes by the
// same "Work Center" menu sections the nav uses (NavTabs.jsx), instead of
// one long flat list, so a specific right is easy to find. Any key not
// listed below (e.g. a page not linked into the nav yet) falls into "Other".
const WORK_CENTER_GROUPS = [
  { label: "BOM & Production", keys: ["bom_explorer", "production_plan", "production_confirmation"] },
  { label: "Procurement", keys: ["purchasing_plan", "purchase_order", "created_purchase_orders", "open_purchase_orders", "quota_allocation", "vendor_goods_receipt"] },
  { label: "Supplier Management", keys: ["supplier_master", "supplier_portal_invite", "supplier_portal_admin", "supplier_portal_documents", "supplier_dashboard"] },
  { label: "Inventory Management", keys: ["inventory", "stock_transfer", "inbound_stock_transfer", "store_approval"] },
  { label: "Master Data", keys: ["admin", "admin_create_material"] },
  { label: "Administration", keys: ["admin_sap_write"] },
];
const GROUPED_PAGE_KEYS = new Set(WORK_CENTER_GROUPS.flatMap((g) => g.keys));
const OTHER_PAGE_KEYS = PAGE_KEYS.filter((k) => !GROUPED_PAGE_KEYS.has(k));

// Aug 2026, user's explicit ask: a new "admin" tier between "user" and
// "super_admin" - full page access automatically like super_admin, can
// grant/revoke pages and set someone's role, but can NEVER hand out
// "admin"/"super_admin" (only an existing super_admin can) and can't
// touch a super_admin's row at all (enforced server-side too, see
// server.py's admin_update_user_access - this is display/UX only).
const ROLE_BADGE = {
  super_admin: { label: "Super Admin", className: "bg-[#0E7C86] text-white" },
  admin: { label: "Admin", className: "bg-[#175CD3] text-white" },
  user: { label: "User", className: "bg-[#F2F4F7] text-[#344054]" },
};

export default function AccessManagementPage() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState({});
  const [savingId, setSavingId] = useState(null);
  // Sep 5 2026, user's explicit ask ("filter by Access right, not role"):
  // filter the User Access list down to only people who currently hold a
  // specific permission - "all" shows everyone.
  const [accessFilter, setAccessFilter] = useState("all");

  // Store Binding (Aug 2026, user's explicit ask): restricts a "store
  // user" to only the site(s) bound here on the Store Assignment tab -
  // admin/super_admin are never restricted, so this only matters for
  // plain "user" role rows.
  const [knownSites, setKnownSites] = useState([]);
  const [siteDrafts, setSiteDrafts] = useState({});
  const [siteSavingId, setSiteSavingId] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const [{ data }, { data: sitesData }] = await Promise.all([
        axios.get(`${API}/admin/users`),
        axios.get(`${API}/admin/known-sites`),
      ]);
      setUsers(data.users || []);
      setKnownSites(sitesData.sites || []);
      const nextDrafts = {};
      const nextSiteDrafts = {};
      (data.users || []).forEach((u) => {
        nextDrafts[u._id] = { role: u.role || "user", allowed_pages: u.allowed_pages || [] };
        nextSiteDrafts[u._id] = u.bound_sites || [];
      });
      setDrafts(nextDrafts);
      setSiteDrafts(nextSiteDrafts);
    } catch (e) {
      toast.error("Failed to load users");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const togglePage = (userId, pageKey) => {
    setDrafts((prev) => {
      const current = prev[userId];
      const has = current.allowed_pages.includes(pageKey);
      return {
        ...prev,
        [userId]: {
          ...current,
          allowed_pages: has ? current.allowed_pages.filter((p) => p !== pageKey) : [...current.allowed_pages, pageKey],
        },
      };
    });
  };

  const setRole = (userId, role) => {
    setDrafts((prev) => ({ ...prev, [userId]: { ...prev[userId], role } }));
  };

  const isDirty = (u) => {
    const d = drafts[u._id];
    if (!d) return false;
    if (d.role !== (u.role || "user")) return true;
    const original = new Set(u.allowed_pages || []);
    const draft = new Set(d.allowed_pages);
    return original.size !== draft.size || [...original].some((p) => !draft.has(p));
  };

  const save = async (userId) => {
    setSavingId(userId);
    try {
      await axios.put(`${API}/admin/users/${userId}/access`, drafts[userId]);
      toast.success("Access updated");
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to update access");
    } finally {
      setSavingId(null);
    }
  };

  const toggleSite = (userId, site) => {
    setSiteDrafts((prev) => {
      const current = prev[userId] || [];
      const has = current.includes(site);
      return { ...prev, [userId]: has ? current.filter((s) => s !== site) : [...current, site] };
    });
  };

  const isSiteDirty = (u) => {
    const original = new Set(u.bound_sites || []);
    const draft = new Set(siteDrafts[u._id] || []);
    return original.size !== draft.size || [...original].some((s) => !draft.has(s));
  };

  const saveSites = async (userId) => {
    setSiteSavingId(userId);
    try {
      await axios.put(`${API}/admin/users/${userId}/store-sites`, { bound_sites: siteDrafts[userId] || [] });
      toast.success("Store site binding updated");
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to update site binding");
    } finally {
      setSiteSavingId(null);
    }
  };

  return (
    <div className="min-h-screen bg-[#F9FAFB] flex flex-col" data-testid="access-management-page">
      <Toaster position="top-right" />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Access Management</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0">
          <NavTabs />
        </div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>

      <div className="p-4 sm:p-6">
        <div className="flex items-center gap-2 mb-4">
          <ShieldCheck size={20} weight="fill" className="text-[#0E7C86]" />
          <h1 className="font-heading text-lg font-bold text-[#101828]">IT Access Management</h1>
        </div>

        <Tabs defaultValue="access" className="space-y-4">
          <TabsList data-testid="access-management-tabs">
            <TabsTrigger value="access" data-testid="tab-user-access">User Access</TabsTrigger>
            <TabsTrigger value="store-assignment" data-testid="tab-store-assignment">Store Assignment</TabsTrigger>
          </TabsList>

          <TabsContent value="access" className="space-y-4">
            <p className="text-sm text-[#667085]">
              Grant or revoke page-level access for anyone who has signed in with Microsoft. New sign-ins appear here
              automatically with no access until you grant it.
            </p>

            <div className="flex items-center gap-2" data-testid="access-management-access-filter-bar">
              <span className="text-xs font-bold text-[#344054] uppercase font-heading">Filter by Access Right</span>
              <Select value={accessFilter} onValueChange={setAccessFilter}>
                <SelectTrigger className="h-8 w-[220px] text-xs bg-white" data-testid="access-management-access-filter-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Users</SelectItem>
                  {WORK_CENTER_GROUPS.flatMap((group) => group.keys).concat(OTHER_PAGE_KEYS).map((pageKey) => (
                    <SelectItem key={pageKey} value={pageKey}>{PAGE_LABELS[pageKey] || pageKey}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {loading ? (
              <div className="text-sm text-[#667085]" data-testid="access-management-loading">Loading users...</div>
            ) : users.length === 0 ? (
              <div className="text-sm text-[#667085]" data-testid="access-management-empty">No one has signed in yet.</div>
            ) : (
              <div className="bg-white border border-[#E4E7EC] rounded-xl overflow-hidden">
                {users
                  .filter((u) => {
                    if (accessFilter === "all") return true;
                    const d = drafts[u._id];
                    const role = d?.role || u.role || "user";
                    if (role === "admin" || role === "super_admin") return true;
                    return (d?.allowed_pages || u.allowed_pages || []).includes(accessFilter);
                  })
                  .map((u) => {
                  const draft = drafts[u._id] || { role: "user", allowed_pages: [] };
                  const hasAllPages = draft.role === "admin" || draft.role === "super_admin";
                  const isSelf = currentUser && u.email === currentUser.email;
                  const viewerIsSuperAdmin = currentUser?.role === "super_admin";
                  return (
                    <div
                      key={u._id}
                      className="border-b border-[#E4E7EC] last:border-b-0 p-4"
                      data-testid={`access-management-row-${u._id}`}
                    >
                      <div className="flex items-center justify-between gap-4 flex-wrap">
                        <div>
                          <div className="text-sm font-bold text-[#101828]">
                            {u.name || u.email}
                            {isSelf && <span className="ml-2 text-xs text-[#98A2B3] font-normal">(you)</span>}
                          </div>
                          <div className="text-xs text-[#667085]">{u.email}</div>
                        </div>
                        <div className="flex items-center gap-3">
                          <Badge className={ROLE_BADGE[draft.role].className} data-testid={`access-management-role-badge-${u._id}`}>
                            {ROLE_BADGE[draft.role].label}
                          </Badge>
                          {viewerIsSuperAdmin ? (
                            <Select value={draft.role} onValueChange={(v) => setRole(u._id, v)} disabled={isSelf}>
                              <SelectTrigger className="h-8 w-[140px] text-xs bg-white" data-testid={`access-management-role-select-${u._id}`}>
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="user">User</SelectItem>
                                <SelectItem value="admin">Admin</SelectItem>
                                <SelectItem value="super_admin">Super Admin</SelectItem>
                              </SelectContent>
                            </Select>
                          ) : draft.role === "admin" ? (
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={isSelf}
                              onClick={() => setRole(u._id, "user")}
                              className="text-xs border-[#D0D5DD]"
                              data-testid={`access-management-toggle-role-${u._id}`}
                            >
                              Demote to User
                            </Button>
                          ) : draft.role === "super_admin" ? (
                            <span className="text-xs text-[#98A2B3]" data-testid={`access-management-locked-${u._id}`}>
                              Only a super admin can change this
                            </span>
                          ) : null}
                        </div>
                      </div>

                      {!hasAllPages ? (
                        <div className="mt-3 space-y-3">
                          {WORK_CENTER_GROUPS.concat(OTHER_PAGE_KEYS.length ? [{ label: "Other", keys: OTHER_PAGE_KEYS }] : []).map((group) => (
                            <div key={group.label} data-testid={`access-management-group-${u._id}-${group.label.replace(/\s+/g, "-").toLowerCase()}`}>
                              <div className="text-[11px] font-bold text-[#98A2B3] uppercase font-heading mb-1.5 tracking-wide">
                                {group.label}
                              </div>
                              <div className="flex flex-wrap gap-x-5 gap-y-2">
                                {group.keys.map((pageKey) => (
                                  <label
                                    key={pageKey}
                                    className="flex items-center gap-2 text-sm text-[#344054] cursor-pointer"
                                    data-testid={`access-management-page-checkbox-label-${u._id}-${pageKey}`}
                                  >
                                    <Checkbox
                                      checked={draft.allowed_pages.includes(pageKey)}
                                      onCheckedChange={() => togglePage(u._id, pageKey)}
                                      data-testid={`access-management-page-checkbox-${u._id}-${pageKey}`}
                                    />
                                    {PAGE_LABELS[pageKey] || pageKey}
                                  </label>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="mt-2 text-xs text-[#667085]">
                          {draft.role === "admin" ? "Admins" : "Super Admins"} automatically have every page.
                        </div>
                      )}

                      {isDirty(u) && (
                        <div className="mt-3">
                          <Button
                            size="sm"
                            disabled={savingId === u._id}
                            onClick={() => save(u._id)}
                            className="bg-[#0E7C86] hover:bg-[#0B6B74] text-white text-xs"
                            data-testid={`access-management-save-${u._id}`}
                          >
                            {savingId === u._id ? "Saving..." : "Save Changes"}
                          </Button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </TabsContent>

          <TabsContent value="store-assignment" className="space-y-4">
            <p className="text-sm text-[#667085]">
              Restrict a store user to only the site(s) checked below - their Store Approval queue, request table, and
              site dropdown will only ever show those sites. Admins and Super Admins always see every site, so they
              don't appear here.
            </p>

            {loading ? (
              <div className="text-sm text-[#667085]" data-testid="store-assignment-loading">Loading users...</div>
            ) : (
              <div className="bg-white border border-[#E4E7EC] rounded-xl overflow-hidden">
                {users.filter((u) => (u.role || "user") === "user").length === 0 ? (
                  <div className="p-4 text-sm text-[#667085]" data-testid="store-assignment-empty">
                    No "User" role accounts yet - promote/demote roles on the User Access tab first.
                  </div>
                ) : (
                  users.filter((u) => (u.role || "user") === "user").map((u) => {
                    const boundDraft = siteDrafts[u._id] || [];
                    return (
                      <div
                        key={u._id}
                        className="border-b border-[#E4E7EC] last:border-b-0 p-4"
                        data-testid={`store-assignment-row-${u._id}`}
                      >
                        <div className="flex items-center justify-between gap-4 flex-wrap mb-2">
                          <div>
                            <div className="text-sm font-bold text-[#101828]">{u.name || u.email}</div>
                            <div className="text-xs text-[#667085]">{u.email}</div>
                          </div>
                          <Badge
                            className={boundDraft.length ? "bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6]" : "bg-[#F2F4F7] text-[#667085]"}
                            data-testid={`store-assignment-count-badge-${u._id}`}
                          >
                            <MapPin size={11} weight="bold" className="mr-1" />
                            {boundDraft.length ? `${boundDraft.length} site${boundDraft.length === 1 ? "" : "s"} bound` : "No sites bound"}
                          </Badge>
                        </div>
                        {knownSites.length === 0 ? (
                          <p className="text-xs text-[#98A2B3]">No sites found yet - visit Inventory or Store Approval once to populate the site list.</p>
                        ) : (
                          <div className="flex flex-wrap gap-x-5 gap-y-2">
                            {knownSites.map((site) => (
                              <label
                                key={site}
                                className="flex items-center gap-2 text-sm text-[#344054] cursor-pointer"
                                data-testid={`store-assignment-site-checkbox-label-${u._id}-${site}`}
                              >
                                <Checkbox
                                  checked={boundDraft.includes(site)}
                                  onCheckedChange={() => toggleSite(u._id, site)}
                                  data-testid={`store-assignment-site-checkbox-${u._id}-${site}`}
                                />
                                {site}
                              </label>
                            ))}
                          </div>
                        )}
                        {isSiteDirty(u) && (
                          <div className="mt-3">
                            <Button
                              size="sm"
                              disabled={siteSavingId === u._id}
                              onClick={() => saveSites(u._id)}
                              className="bg-[#0E7C86] hover:bg-[#0B6B74] text-white text-xs"
                              data-testid={`store-assignment-save-${u._id}`}
                            >
                              {siteSavingId === u._id ? "Saving..." : "Save Site Binding"}
                            </Button>
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
