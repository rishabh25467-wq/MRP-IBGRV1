import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { Warning, CheckCircle, UploadSimple } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";
import { getMockQmsData } from "@/lib/qmsMockData";
import { SupplierPortalLayout } from "./SupplierPortalLayout";
import { toast } from "@/components/ui/sonner";

const SEVERITY_BADGE = {
  Critical: "bg-[#FEF2F2] text-[#991B1B] border-[#FECACA]",
  Major: "bg-[#FFFBEB] text-[#92400E] border-[#FDE68A]",
  Minor: "bg-[#EFF6FF] text-[#1E40AF] border-[#BFDBFE]",
  High: "bg-[#FEF2F2] text-[#991B1B] border-[#FECACA]",
  Medium: "bg-[#FFFBEB] text-[#92400E] border-[#FDE68A]",
  Low: "bg-[#EFF6FF] text-[#1E40AF] border-[#BFDBFE]",
};

const STATUS_BADGE = {
  Open: "bg-[#FFEDD5] text-[#9A3412] border-[#FED7AA]",
  "Under Investigation": "bg-[#EFF6FF] text-[#1E40AF] border-[#BFDBFE]",
  Closed: "bg-[#ECFDF5] text-[#065F46] border-[#A7F3D0]",
};

function fmtDate(d) {
  return new Date(d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

function Badge({ text, map }) {
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-semibold border ${map[text] || "bg-slate-50 text-slate-700 border-slate-200"}`}>
      {text}
    </span>
  );
}

export default function SupplierAuditsPage() {
  const { account } = useSupplierAuth();
  const { vendorCode } = useParams();
  const isImpersonating = !!(account?.testing_mode && vendorCode && vendorCode !== account?.vendor_code);

  const qms = useMemo(() => getMockQmsData(vendorCode), [vendorCode]);

  const [caps, setCaps] = useState([]);
  const [capModal, setCapModal] = useState(null);
  const [form, setForm] = useState({ root_cause: "", containment_action: "", corrective_action: "", target_completion_date: "" });
  const [evidenceFile, setEvidenceFile] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const load = async () => {
    try {
      const params = isImpersonating ? { as_vendor: vendorCode } : {};
      const { data } = await supplierApi.get("/caps", { params });
      setCaps(data.caps || []);
    } catch {
      // non-fatal - action buttons just show "Submit CAP" instead of "Submitted"
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendorCode]);

  const capFor = (referenceId) => caps.find((c) => c.reference_id === referenceId);

  const pendingCapCount = qms.qNotifications.filter((q) => q.status !== "Closed" && !capFor(q.id)).length
    + qms.auditNCs.filter((nc) => nc.status !== "Closed" && !capFor(nc.id)).length;

  const openCapModal = (referenceType, referenceId, label) => {
    setCapModal({ referenceType, referenceId, label });
    setForm({ root_cause: "", containment_action: "", corrective_action: "", target_completion_date: "" });
    setEvidenceFile(null);
    setSubmitError("");
  };

  const submitCap = async () => {
    if (!(form.root_cause.trim() && form.containment_action.trim() && form.corrective_action.trim() && form.target_completion_date)) {
      setSubmitError("Root cause, containment action, corrective action, and target date are all required.");
      return;
    }
    setSubmitting(true);
    setSubmitError("");
    try {
      const fd = new FormData();
      fd.append("reference_type", capModal.referenceType);
      fd.append("reference_id", capModal.referenceId);
      fd.append("root_cause", form.root_cause);
      fd.append("containment_action", form.containment_action);
      fd.append("corrective_action", form.corrective_action);
      fd.append("target_completion_date", form.target_completion_date);
      if (evidenceFile) fd.append("evidence", evidenceFile);
      const params = isImpersonating ? { as_vendor: vendorCode } : {};
      await supplierApi.post("/caps", fd, { params });
      toast.success("Corrective Action Plan submitted", { description: `Recorded against ${capModal.label}.` });
      setCapModal(null);
      load();
    } catch (e) {
      setSubmitError(e.response?.data?.detail || "Could not submit this CAP. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <SupplierPortalLayout
      active="audits"
      vendorCode={vendorCode}
      isImpersonating={isImpersonating}
      pageTitle="Audits & Quality Control"
      pageSubtitle="QMS Integration (Preview)"
    >
      <div
        className="flex items-start gap-2.5 bg-[#FFFBEB] border border-[#FDE68A] text-[#92400E] text-sm rounded-lg px-4 py-3"
        data-testid="qms-demo-banner"
      >
        <Warning size={18} weight="fill" className="shrink-0 mt-0.5" />
        <span>
          <strong>Demo data disclosure:</strong> the QMS integration feed is currently operating in Preview/Demo mode.
          Data shown below illustrates real-time Q-Notifications and Audit Non-Conformances once the live QMS API sync is finalized.
        </span>
      </div>

      <div className="mt-4 grid grid-cols-2 lg:grid-cols-4 gap-3" data-testid="qms-metrics-row">
        <div className="bg-white border border-[#E2E8F0] rounded-xl p-4" data-testid="qms-metric-total-notifications">
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Total Q-Notifications</div>
          <div className="text-2xl md:text-3xl font-extrabold text-[#0F172A] mt-1">{qms.metrics.totalQNotifications}</div>
        </div>
        <div className="bg-white border border-[#E2E8F0] rounded-xl p-4" data-testid="qms-metric-open-ncs">
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Open Audit NCs</div>
          <div className="text-2xl md:text-3xl font-extrabold text-[#0F172A] mt-1">{qms.metrics.openAuditNCs}</div>
        </div>
        <div className="bg-white border border-[#E2E8F0] rounded-xl p-4" data-testid="qms-metric-cap-pending">
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider">CAP Pending Action</div>
          <div className="text-2xl md:text-3xl font-extrabold text-[#9A3412] mt-1">{pendingCapCount}</div>
        </div>
        <div className="bg-white border border-[#E2E8F0] rounded-xl p-4" data-testid="qms-metric-pass-rate">
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Pass Rate</div>
          <div className="text-2xl md:text-3xl font-extrabold text-[#065F46] mt-1">{qms.metrics.passRate}%</div>
        </div>
      </div>

      <Tabs defaultValue="qnotifications" className="mt-6">
        <TabsList data-testid="qms-tabs-list">
          <TabsTrigger value="qnotifications" data-testid="qms-tab-qnotifications">Q-Notifications</TabsTrigger>
          <TabsTrigger value="audit-ncs" data-testid="qms-tab-audit-ncs">Audit Non-Conformances</TabsTrigger>
        </TabsList>

        <TabsContent value="qnotifications">
          <div className="bg-white border border-[#E2E8F0] rounded-xl shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto" data-testid="qms-qnotifications-table">
            <table className="w-full text-[13px] border-collapse">
              <thead className="bg-[#F1F5F9] text-[#334155] text-xs font-bold font-heading uppercase tracking-wide">
                <tr>
                  <th className="border border-[#E2E8F0] p-2 text-left">Notification #</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Defect Type</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Material Code</th>
                  <th className="border border-[#E2E8F0] p-2 text-right">Affected Qty</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Inspection Lot #</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Severity</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Status</th>
                  <th className="border border-[#E2E8F0] p-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {qms.qNotifications.map((q) => {
                  const cap = capFor(q.id);
                  return (
                    <tr key={q.id} className="bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" data-testid={`qms-qnotification-row-${q.id}`}>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 font-data font-semibold text-[#1E40AF]">{q.id}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5">{q.defect_type}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 font-data">{q.material_code}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 text-right font-data">{q.affected_qty}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 font-data text-xs">{q.inspection_lot}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5"><Badge text={q.severity} map={SEVERITY_BADGE} /></td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5"><Badge text={q.status} map={STATUS_BADGE} /></td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 text-right">
                        {cap ? (
                          <span className="inline-flex items-center gap-1 text-xs font-semibold text-[#065F46]" data-testid={`qms-cap-submitted-${q.id}`}>
                            <CheckCircle size={13} weight="fill" /> CAP Submitted
                          </span>
                        ) : (
                          <Button size="sm" variant="outline" className="rounded-sm h-7 text-xs" onClick={() => openCapModal("q_notification", q.id, q.id)} data-testid={`qms-submit-cap-button-${q.id}`}>
                            Submit CAP
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </TabsContent>

        <TabsContent value="audit-ncs">
          <div className="bg-white border border-[#E2E8F0] rounded-xl shadow-[0_1px_2px_0_rgba(16,24,40,0.05)] overflow-x-auto" data-testid="qms-audit-ncs-table">
            <table className="w-full text-[13px] border-collapse">
              <thead className="bg-[#F1F5F9] text-[#334155] text-xs font-bold font-heading uppercase tracking-wide">
                <tr>
                  <th className="border border-[#E2E8F0] p-2 text-left">NC #</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Audit Date</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Finding</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">ISO Clause</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Risk Level</th>
                  <th className="border border-[#E2E8F0] p-2 text-left">Status</th>
                  <th className="border border-[#E2E8F0] p-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {qms.auditNCs.map((nc) => {
                  const cap = capFor(nc.id);
                  return (
                    <tr key={nc.id} className="bg-white odd:bg-[#F9FAFB] hover:bg-[#F0F4F8] transition-colors duration-150" data-testid={`qms-nc-row-${nc.id}`}>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 font-data font-semibold text-[#1E40AF]">{nc.id}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 font-data text-xs whitespace-nowrap">{fmtDate(nc.audit_date)}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 max-w-xs">{nc.finding}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 text-xs">{nc.iso_clause}</td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5"><Badge text={nc.risk_level} map={SEVERITY_BADGE} /></td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5"><Badge text={nc.status} map={STATUS_BADGE} /></td>
                      <td className="border border-[#E2E8F0] px-2 py-1.5 text-right">
                        {cap ? (
                          <span className="inline-flex items-center gap-1 text-xs font-semibold text-[#065F46]" data-testid={`qms-cap-submitted-${nc.id}`}>
                            <CheckCircle size={13} weight="fill" /> CAP Submitted
                          </span>
                        ) : (
                          <Button size="sm" variant="outline" className="rounded-sm h-7 text-xs" onClick={() => openCapModal("audit_nc", nc.id, nc.id)} data-testid={`qms-submit-cap-button-${nc.id}`}>
                            Submit CAP
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </TabsContent>
      </Tabs>

      <Dialog open={!!capModal} onOpenChange={(o) => !o && setCapModal(null)}>
        <DialogContent className="rounded-lg max-w-lg" data-testid="qms-cap-dialog">
          <DialogHeader>
            <DialogTitle className="font-heading">Corrective Action Plan - {capModal?.label}</DialogTitle>
            <DialogDescription>This will be recorded against {capModal?.label} and shared with Quality once the live QMS sync is finalized.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 max-h-[55vh] overflow-y-auto pr-1">
            <div>
              <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Root Cause Analysis</label>
              <Textarea
                value={form.root_cause}
                onChange={(e) => setForm((f) => ({ ...f, root_cause: e.target.value }))}
                placeholder="Describe the root cause identified..."
                className="mt-1 rounded-md"
                data-testid="qms-cap-root-cause-input"
              />
            </div>
            <div>
              <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Immediate Containment Action</label>
              <Textarea
                value={form.containment_action}
                onChange={(e) => setForm((f) => ({ ...f, containment_action: e.target.value }))}
                placeholder="What was done immediately to contain the issue..."
                className="mt-1 rounded-md"
                data-testid="qms-cap-containment-input"
              />
            </div>
            <div>
              <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Corrective Action Plan</label>
              <Textarea
                value={form.corrective_action}
                onChange={(e) => setForm((f) => ({ ...f, corrective_action: e.target.value }))}
                placeholder="Plan to prevent recurrence..."
                className="mt-1 rounded-md"
                data-testid="qms-cap-corrective-input"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Target Completion Date</label>
                <Input
                  type="date"
                  value={form.target_completion_date}
                  onChange={(e) => setForm((f) => ({ ...f, target_completion_date: e.target.value }))}
                  className="mt-1 rounded-md"
                  data-testid="qms-cap-target-date-input"
                />
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Evidence File (optional)</label>
                <label className="mt-1 flex items-center gap-1.5 text-xs border border-[#E2E8F0] rounded-md px-2.5 h-9 cursor-pointer text-slate-600 hover:bg-slate-50">
                  <UploadSimple size={14} />
                  <span className="truncate">{evidenceFile ? evidenceFile.name : "Attach file"}</span>
                  <input type="file" className="hidden" onChange={(e) => setEvidenceFile(e.target.files?.[0] || null)} data-testid="qms-cap-evidence-input" />
                </label>
              </div>
            </div>
          </div>
          {submitError && <div className="text-sm text-[#991B1B]" data-testid="qms-cap-submit-error">{submitError}</div>}
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" className="rounded-md" onClick={() => setCapModal(null)}>Cancel</Button>
            <Button onClick={submitCap} disabled={submitting} className="rounded-md bg-[#1E40AF] hover:bg-[#1E3A8A] transition-colors duration-150" data-testid="qms-cap-submit-button">
              {submitting ? "Submitting..." : "Submit CAP"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </SupplierPortalLayout>
  );
}
