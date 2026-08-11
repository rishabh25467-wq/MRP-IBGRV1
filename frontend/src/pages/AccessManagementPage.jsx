import { useState, useEffect } from "react";
import axios from "axios";
import { Shield, ShieldCheck } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Toaster, toast } from "@/components/ui/sonner";
import { NavTabs } from "@/components/NavTabs";
import { useAuth, PAGE_LABELS } from "@/contexts/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const PAGE_KEYS = Object.keys(PAGE_LABELS);

export default function AccessManagementPage() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState({});
  const [savingId, setSavingId] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/admin/users`);
      setUsers(data.users || []);
      const nextDrafts = {};
      (data.users || []).forEach((u) => {
        nextDrafts[u._id] = { role: u.role || "user", allowed_pages: u.allowed_pages || [] };
      });
      setDrafts(nextDrafts);
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

  const toggleRole = (userId) => {
    setDrafts((prev) => ({
      ...prev,
      [userId]: { ...prev[userId], role: prev[userId].role === "super_admin" ? "user" : "super_admin" },
    }));
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
      </header>

      <div className="p-4 sm:p-6">
        <div className="flex items-center gap-2 mb-4">
          <ShieldCheck size={20} weight="fill" className="text-[#0E7C86]" />
          <h1 className="font-heading text-lg font-bold text-[#101828]">IT Access Management</h1>
        </div>
        <p className="text-sm text-[#667085] mb-5">
          Grant or revoke page-level access for anyone who has signed in with Microsoft. New sign-ins appear here
          automatically with no access until you grant it.
        </p>

        {loading ? (
          <div className="text-sm text-[#667085]" data-testid="access-management-loading">Loading users...</div>
        ) : users.length === 0 ? (
          <div className="text-sm text-[#667085]" data-testid="access-management-empty">No one has signed in yet.</div>
        ) : (
          <div className="bg-white border border-[#E4E7EC] rounded-xl overflow-hidden">
            {users.map((u) => {
              const draft = drafts[u._id] || { role: "user", allowed_pages: [] };
              const isSuperAdmin = draft.role === "super_admin";
              const isSelf = currentUser && u.email === currentUser.email;
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
                      <Badge
                        className={isSuperAdmin ? "bg-[#0E7C86] text-white" : "bg-[#F2F4F7] text-[#344054]"}
                        data-testid={`access-management-role-badge-${u._id}`}
                      >
                        {isSuperAdmin ? "Super Admin" : "User"}
                      </Badge>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isSelf}
                        onClick={() => toggleRole(u._id)}
                        className="text-xs border-[#D0D5DD]"
                        data-testid={`access-management-toggle-role-${u._id}`}
                      >
                        {isSuperAdmin ? "Demote to User" : "Promote to Super Admin"}
                      </Button>
                    </div>
                  </div>

                  {!isSuperAdmin && (
                    <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
                      {PAGE_KEYS.map((pageKey) => (
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
                          {PAGE_LABELS[pageKey]}
                        </label>
                      ))}
                    </div>
                  )}
                  {isSuperAdmin && (
                    <div className="mt-2 text-xs text-[#667085]">Super Admins automatically have every page.</div>
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
      </div>
    </div>
  );
}
