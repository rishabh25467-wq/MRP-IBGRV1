import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { FileText, UploadSimple, Clock, CheckCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";
import { SupplierPortalLayout } from "./SupplierPortalLayout";

const DOC_TYPES = [
  { key: "msme", label: "MSME Certificate" },
  { key: "bank", label: "Bank Details / Cancelled Cheque" },
];

function formatDate(d) {
  if (!d) return "";
  return new Date(d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

export default function SupplierDocumentsPage() {
  const { account } = useSupplierAuth();
  const { vendorCode } = useParams();

  const [docs, setDocs] = useState({ msme: { latest: null, history: [] }, bank: { latest: null, history: [] } });
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(null);
  const [historyOpenFor, setHistoryOpenFor] = useState(null);
  const [error, setError] = useState("");

  const load = async () => {
    try {
      const { data } = await supplierApi.get("/documents");
      setDocs(data);
    } catch {
      // non-fatal - sections just show "not uploaded yet"
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleUpload = async (docType, file) => {
    if (!file) return;
    setError("");
    setUploading(docType);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await supplierApi.post(`/documents/${docType}`, fd);
      await load();
    } catch (e) {
      setError(e.response?.data?.detail || "Could not upload this document. Please try again.");
    } finally {
      setUploading(null);
    }
  };

  const historyDoc = historyOpenFor ? docs[historyOpenFor] : null;

  return (
    <SupplierPortalLayout
      active="documents"
      vendorCode={vendorCode}
      pageTitle="Vendor Compliance & Statutory Documents"
      pageSubtitle={account?.company_name}
    >
      <div data-testid="supplier-documents-page">
        <p className="text-sm text-[#475569] mt-1">Upload anytime, and re-upload if something changes - each upload is kept as a new version, nothing is deleted.</p>

        {error && <div className="mt-4 bg-[#FEF2F2] border border-[#FECACA] text-[#991B1B] text-sm rounded-md p-3" data-testid="supplier-documents-error">{error}</div>}

        {loading && <div className="mt-8 text-sm text-[#475569]">Loading your documents...</div>}

        {!loading && (
          <div className="mt-4 space-y-4">
            {DOC_TYPES.map(({ key, label }) => {
              const doc = docs[key] || { latest: null, history: [] };
              return (
                <div key={key} className="bg-white border border-[#E2E8F0] rounded-xl p-4 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)]" data-testid={`supplier-document-card-${key}`}>
                  <div className="flex items-center justify-between flex-wrap gap-3">
                    <div className="flex items-center gap-2">
                      <FileText size={18} className="text-[#1E40AF]" />
                      <span className="font-heading font-semibold text-[#0F172A]">{label}</span>
                    </div>
                    <label>
                      <input
                        type="file" className="hidden"
                        onChange={(e) => handleUpload(key, e.target.files?.[0] || null)}
                        data-testid={`supplier-document-upload-input-${key}`}
                      />
                      <Button asChild variant="outline" size="sm" className="rounded-sm cursor-pointer" disabled={uploading === key}>
                        <span>
                          <UploadSimple size={14} className="mr-1.5" />
                          {uploading === key ? "Uploading..." : doc.latest ? "Upload New Version" : "Upload"}
                        </span>
                      </Button>
                    </label>
                  </div>

                  {doc.latest ? (
                    <div className="mt-3 flex items-center justify-between flex-wrap gap-2 text-sm">
                      <a
                        href={`${supplierApi.defaults.baseURL}/documents/${key}/${doc.latest.version}`}
                        target="_blank" rel="noreferrer"
                        className="flex items-center gap-1.5 text-[#1E40AF] hover:underline"
                        data-testid={`supplier-document-latest-link-${key}`}
                      >
                        <CheckCircle size={14} weight="fill" /> {doc.latest.filename} (v{doc.latest.version}) - uploaded {formatDate(doc.latest.uploaded_at)}
                      </a>
                      {doc.history.length > 1 && (
                        <button
                          onClick={() => setHistoryOpenFor(key)}
                          className="text-xs text-[#475569] hover:underline flex items-center gap-1"
                          data-testid={`supplier-document-history-btn-${key}`}
                        >
                          <Clock size={12} /> View history ({doc.history.length})
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="mt-3 text-sm text-[#94A3B8]" data-testid={`supplier-document-empty-${key}`}>Not uploaded yet.</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <Dialog open={!!historyOpenFor} onOpenChange={(open) => !open && setHistoryOpenFor(null)}>
        <DialogContent className="max-w-md" data-testid="supplier-document-history-dialog">
          <DialogHeader>
            <DialogTitle>Version History</DialogTitle>
            <DialogDescription>Older versions are kept for reference and can't be removed.</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {historyDoc?.history?.slice().reverse().map((rev) => (
              <a
                key={rev.version}
                href={`${supplierApi.defaults.baseURL}/documents/${historyOpenFor}/${rev.version}`}
                target="_blank" rel="noreferrer"
                className="flex items-center justify-between text-sm border border-[#E2E8F0] rounded-md p-2 hover:bg-[#F9FAFB]"
                data-testid={`supplier-document-history-item-${rev.version}`}
              >
                <span>v{rev.version} - {rev.filename}</span>
                <span className="text-xs text-[#475569]">{formatDate(rev.uploaded_at)}</span>
              </a>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </SupplierPortalLayout>
  );
}
