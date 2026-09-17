import { useEffect, useMemo, useState } from "react";
import axios from "axios";
import { MagnifyingGlass, Circuitry, CircleNotch, Link as LinkIcon } from "@phosphor-icons/react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const STATUS_STYLE = (status) => {
  if (status.startsWith("active")) return "bg-[#ECFDF3] text-[#027A48] border-[#ABEFC6]";
  if (status.includes("dormant") || status.includes("deprecated")) return "bg-[#FEF0C7] text-[#93370D] border-[#FEDF89]";
  return "bg-[#F2F4F7] text-[#344054] border-[#D0D5DD]";
};

const PROTOCOL_STYLE = (protocol) => {
  if (protocol.includes("SOAP")) return "bg-[#EFF8FF] text-[#175CD3] border-[#B2DDFF]";
  if (protocol.includes("OData")) return "bg-[#F4F3FF] text-[#5925DC] border-[#D9D6FE]";
  if (protocol.includes("Playwright")) return "bg-[#FEF3F2] text-[#B42318] border-[#FDA29B]";
  return "bg-[#F2F4F7] text-[#344054] border-[#D0D5DD]";
};

const IntegrationCard = ({ item }) => (
  <div className="border border-[#E4E7EC] rounded-md p-4 bg-white" data-testid={`sap-integration-card-${item.id}`}>
    <div className="flex items-start justify-between gap-3 flex-wrap">
      <h3 className="font-bold text-[#101828] text-sm">{item.name}</h3>
      <div className="flex gap-1.5 flex-wrap shrink-0">
        <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full border ${PROTOCOL_STYLE(item.protocol)}`}>{item.protocol}</span>
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${STATUS_STYLE(item.status)}`}>{item.status}</span>
      </div>
    </div>
    <p className="text-xs text-[#667085] mt-2 leading-relaxed">{item.purpose}</p>
    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
      <div>
        <span className="font-bold text-[#344054]">Service:</span> <span className="text-[#475467]">{item.service}</span>
      </div>
      <div>
        <span className="font-bold text-[#344054]">Technical user:</span> <span className="text-[#475467]">{item.technical_user}</span>
      </div>
      <div className="sm:col-span-2 flex items-start gap-1">
        <LinkIcon size={12} className="mt-0.5 shrink-0 text-[#475467]" />
        <span className="text-[#475467] break-all">{item.endpoint_url}</span>
      </div>
      <div className="sm:col-span-2">
        <span className="font-bold text-[#344054]">Operations:</span>{" "}
        <span className="text-[#475467]">{(item.operations || []).join(", ")}</span>
      </div>
      <div className="sm:col-span-2 text-[#98A2B3]">
        env: {item.endpoint_env} &middot; client file: {item.client_file}
      </div>
    </div>
  </div>
);

export default function SapIntegrationsDocsPage() {
  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  useEffect(() => {
    axios.get(`${API}/public/sap-integrations`).then(({ data }) => setDoc(data)).finally(() => setLoading(false));
  }, []);

  const filteredCategories = useMemo(() => {
    if (!doc) return [];
    const q = search.trim().toLowerCase();
    if (!q) return doc.categories;
    return doc.categories
      .map((c) => ({ ...c, integrations: c.integrations.filter((i) => `${i.name} ${i.service} ${i.purpose} ${i.category}`.toLowerCase().includes(q)) }))
      .filter((c) => c.integrations.length > 0);
  }, [doc, search]);

  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="sap-integrations-docs-page">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 py-8">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-md bg-[#101828] flex items-center justify-center shrink-0">
            <Circuitry size={20} className="text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-[#101828]">SAP Integration Reference</h1>
            <p className="text-xs text-[#667085]">Every SOAP/OData API this app talks to on tenant <span className="font-mono">{doc?.tenant_host || "..."}</span> - internal reference, public read-only page.</p>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-[#667085] mt-10" data-testid="sap-integrations-loading">
            <CircleNotch size={18} className="animate-spin" /> Loading...
          </div>
        ) : !doc ? (
          <p className="text-[#B42318] mt-10" data-testid="sap-integrations-error">Could not load integration reference. Try refreshing.</p>
        ) : (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-6 gap-3 mt-6">
              {[
                ["Total", doc.counts.total],
                ["Active", doc.counts.active],
                ["Dormant/Deprecated", doc.counts.dormant_or_deprecated],
                ["SOAP", doc.counts.soap],
                ["OData", doc.counts.odata],
                ["Playwright", doc.counts.playwright],
              ].map(([label, value]) => (
                <div key={label} className="border border-[#E4E7EC] rounded-md p-3 bg-white text-center">
                  <p className="text-xl font-bold text-[#101828]">{value}</p>
                  <p className="text-[10px] text-[#667085] uppercase font-bold">{label}</p>
                </div>
              ))}
            </div>

            <div className="relative mt-6">
              <MagnifyingGlass size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
              <input
                data-testid="sap-integrations-search-input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search by name, service, or purpose..."
                className="w-full border border-[#D0D5DD] rounded-md pl-9 pr-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#101828]/10"
              />
            </div>

            <div className="mt-8 space-y-10">
              {filteredCategories.map((c) => (
                <div key={c.category} data-testid={`sap-integration-category-${c.category.replace(/[^a-zA-Z0-9]+/g, "-").toLowerCase()}`}>
                  <h2 className="text-sm font-bold text-[#101828] uppercase tracking-wide border-b border-[#E4E7EC] pb-2 mb-3">{c.category}</h2>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {c.integrations.map((item) => <IntegrationCard key={item.id} item={item} />)}
                  </div>
                </div>
              ))}
              {filteredCategories.length === 0 && (
                <p className="text-sm text-[#667085]" data-testid="sap-integrations-no-results">No integrations match "{search}".</p>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
