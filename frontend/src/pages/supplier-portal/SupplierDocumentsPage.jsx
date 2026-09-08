import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Buildings, SignOut, ArrowLeft, FileText, UploadSimple, Clock, CheckCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { supplierApi } from "@/lib/supplierPortalApi";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

const DOC_TYPES = [
  { key: "msme", label: "MSME Certificate" },
  { key: "bank", label: "Bank Details / Cancelled Cheque" },
];

function formatDate(d) {
  if (!d) return "";
  return new Date(d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

export default function SupplierDocumentsPage() {
  const { account, logout } = useSupplierAuth();
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
    <div className="min-h-screen bg-[#F2F4F7] font-sans" data-testid="supplier-documents-page">
      <div className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] text-white px-6 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <Buildings size={20} weight="fill" className="text-white" />
          <span className="font-heading font-bold tracking-tight">Supplier Portal</span>
        </div>
        <div className="flex items-center gap-4">
          <Link to={`/supplier-portal/dashboard/${vendorCode}`} data-testid="supplier-nav-dashboard-link">
            <Button variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150">
              <ArrowLeft size={14} className="mr-1" /> Purchase Orders
            </Button>
          </Link>
          <div className="text-right hidden sm:block">
            <div className="text-sm font-semibold">{account?.company_name}</div>
            <div className="text-xs text-white/75 font-data">Vendor Code: {vendorCode}</div>
          </div>
          <Button onClick={logout} variant="outline" size="sm" className="rounded-sm border-white/40 text-white hover:bg-white/10 hover:text-white transition-colors duration-150" data-testid="supplier-dashboard-logout-button">
            <SignOut size={14} className="mr-1" /> Sign Out
          </Button>
        </div>
      </div>

      <div className="max-w-3xl mx-auto p-4 md:p-6">
        <h1 className="font-heading text-xl font-bold text-[#1D2939]">Additional Documents</h1>
        <p className="text-sm text-[#475467] mt-1">Upload anytime, and re-upload if something changes - each upload is kept as a new version, nothing is deleted.</p>

        {error && <div className="mt-4 bg-[#FEF3F2] border border-[#FECDCA] text-[#B42318] text-sm rounded-sm p-3" data-testid="supplier-documents-error">{error}</div>}

        {loading && <div className="mt-8 text-sm text-[#475467]">Loading your documents...</div>}

        {!loading && (
          <div className="mt-4 space-y-4">
            {DOC_TYPES.map(({ key, label }) => {
              const doc = docs[key] || { latest: null, history: [] };
              return (
                <div key={key} className="bg-white border border-[#D0D5DD] rounded-sm p-4 shadow-[0_1px_2px_0_rgba(16,24,40,0.05)]" data-testid={`supplier-document-card-${key}`}>
                  <div className="flex items-center justify-between flex-wrap gap-3">
                    <div className="flex items-center gap-2">
                      <FileText size={18} className="text-[#0E7C86]" />
                      <span className="font-heading font-semibold text-[#1D2939]">{label}</span>
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
                        className="flex items-center gap-1.5 text-[#0E7C86] hover:underline"
                        data-testid={`supplier-document-latest-link-${key}`}
                      >
                        <CheckCircle size={14} weight="fill" /> {doc.latest.filename} (v{doc.latest.version}) - uploaded {formatDate(doc.latest.uploaded_at)}
                      </a>
                      {doc.history.length > 1 && (
                        <button
                          onClick={() => setHistoryOpenFor(key)}
                          className="text-xs text-[#475467] hover:underline flex items-center gap-1"
                          data-testid={`supplier-document-history-btn-${key}`}
                        >
                          <Clock size={12} /> View history ({doc.history.length})
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="mt-3 text-sm text-[#98A2B3]" data-testid={`supplier-document-empty-${key}`}>Not uploaded yet.</div>
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
                className="flex items-center justify-between text-sm border border-[#D0D5DD] rounded-sm p-2 hover:bg-[#F9FAFB]"
                data-testid={`supplier-document-history-item-${rev.version}`}
              >
                <span>v{rev.version} - {rev.filename}</span>
                <span className="text-xs text-[#475467]">{formatDate(rev.uploaded_at)}</span>
              </a>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
