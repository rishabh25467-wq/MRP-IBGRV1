import { useState, useEffect, useCallback } from "react";
import "@/App.css";
import axios from "axios";
import { Truck, Shield, CircleNotch, ArrowRight } from "@phosphor-icons/react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";
import { ErpConnectionStatus } from "@/components/ErpConnectionStatus";
import { ReceiptDetailModal } from "@/components/ReceiptDetailModal";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const formatDate = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const formatDuration = (seconds) => {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
};

const COMPLETED_STATUS_STYLE = {
  received: { label: "Received", cls: "bg-[#ECFDF3] text-[#027A48]" },
  partial: { label: "Partially Received", cls: "bg-[#FEF3C7] text-[#92400E]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

const STATUS_STYLE = {
  pending: { label: "Pending Receipt", cls: "bg-[#FEF3C7] text-[#92400E]" },
  awaiting_relocation: { label: "Moving to Warehouse", cls: "bg-[#F0F9FA] text-[#0B6B74]" },
  partial: { label: "Partially Received", cls: "bg-[#FEE4E2] text-[#B42318]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

// Inbound STO Receipt (Aug 28 2026) - lets any logged-in user receive a
// multi-line STO in one click instead of the SAP UI showing one row per
// line. Sep 23 2026 redesign (user's explicit ask): dropped bulk
// selection and per-row expand/popover in favor of a single detail
// modal (see ReceiptDetailModal.jsx) that shows the shipment, fires the
// Goods Receipt the instant it opens, and only starts the slower
// warehouse move once the user confirms - the tables here now just
// show a compact status + error, all detail/progress/retry lives in
// the modal.
export default function InboundReceiptsPage() {
  const { user } = useAuth();
  const [sites, setSites] = useState([]);
  const [siteFilter, setSiteFilter] = useState("all");
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("pending"); // "pending" | "completed"
  const [completedOrders, setCompletedOrders] = useState([]);
  const [completedLoading, setCompletedLoading] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [modalState, setModalState] = useState(null); // { order, mode: "receive" | "view" }

  useEffect(() => {
    axios.get(`${API}/inbound-receipts/sites`).then(({ data }) => {
      setSites(data.sites || []);
      const defaultSite = (user?.bound_sites || []).find((s) => (data.sites || []).includes(s));
      if (defaultSite) setSiteFilter(defaultSite);
    }).catch(() => toast.error("Could not load receiving sites."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadCompletedOrders = useCallback(async () => {
    setCompletedLoading(true);
    try {
      const params = {};
      if (siteFilter !== "all") params.site_id = siteFilter;
      if (dateFrom) params.date_from = dateFrom;
      if (dateTo) params.date_to = dateTo;
      const { data } = await axios.get(`${API}/inbound-receipts/completed`, { params });
      setCompletedOrders(data.orders || []);
    } catch {
      toast.error("Could not load completed receipts.");
    } finally {
      setCompletedLoading(false);
    }
  }, [siteFilter, dateFrom, dateTo]);

  useEffect(() => {
    if (activeTab === "completed") loadCompletedOrders();
  }, [activeTab, loadCompletedOrders]);

  const loadOrders = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/inbound-receipts/pending`, {
        params: siteFilter === "all" ? {} : { site_id: siteFilter },
      });
      setOrders(data.orders || []);
    } catch {
      toast.error("Could not load pending receipts.");
    } finally {
      setLoading(false);
    }
  }, [siteFilter]);

  useEffect(() => { loadOrders(); }, [loadOrders]);

  const closeModal = () => setModalState(null);
  const onModalUpdated = () => { loadOrders(); if (activeTab === "completed") loadCompletedOrders(); };

  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="inbound-receipts-page">
      <Toaster position="top-right" richColors />
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Inbound STO Receipt</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0"><NavTabs /></div>
        <SapConnectionStatus />
        <ErpConnectionStatus />
      </header>
      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 py-8">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-6">
          <div>
            <h1 className="font-heading text-2xl sm:text-3xl font-bold text-[#101828] flex items-center gap-2">
              <Truck size={28} weight="fill" className="text-[#0B6B74]" />
              Inbound STO Receipt
            </h1>
            <p className="text-sm text-[#667085] mt-1">
              Receive a Stock Transfer Order — closes every SAP delivery line for it automatically.
            </p>
          </div>
          <div className="w-full sm:w-56">
            <Select value={siteFilter} onValueChange={setSiteFilter}>
              <SelectTrigger data-testid="inbound-receipts-site-filter">
                <SelectValue placeholder="Receiving site" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Receiving Sites</SelectItem>
                {sites.map((s) => (
                  <SelectItem key={s} value={s}>{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex items-center gap-1 mb-5 bg-[#EEF2F1] p-1 rounded-lg w-fit" data-testid="inbound-receipts-tabs">
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${activeTab === "pending" ? "bg-white text-[#0B6B74] shadow-sm" : "text-[#667085] hover:text-[#344054]"}`}
            onClick={() => setActiveTab("pending")}
            data-testid="inbound-receipts-tab-pending"
          >
            Pending
          </button>
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${activeTab === "completed" ? "bg-white text-[#0B6B74] shadow-sm" : "text-[#667085] hover:text-[#344054]"}`}
            onClick={() => setActiveTab("completed")}
            data-testid="inbound-receipts-tab-completed"
          >
            Completed
          </button>
        </div>

        {activeTab === "completed" && (
          <div className="flex items-end gap-3 flex-wrap mb-5" data-testid="inbound-receipts-completed-filters">
            <div>
              <label className="text-xs font-medium text-[#667085] block mb-1">From</label>
              <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-40" data-testid="inbound-receipts-date-from" />
            </div>
            <div>
              <label className="text-xs font-medium text-[#667085] block mb-1">To</label>
              <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-40" data-testid="inbound-receipts-date-to" />
            </div>
            {(dateFrom || dateTo) && (
              <Button variant="outline" size="sm" onClick={() => { setDateFrom(""); setDateTo(""); }} data-testid="inbound-receipts-date-clear-btn">
                Clear dates
              </Button>
            )}
          </div>
        )}

        {activeTab === "pending" && (
          loading ? (
            <div className="flex items-center justify-center py-24 text-[#667085]" data-testid="inbound-receipts-loading">
              <CircleNotch size={24} className="animate-spin mr-2" /> Loading pending receipts…
            </div>
          ) : orders.length === 0 ? (
            <div className="text-center py-24 text-[#667085] bg-white rounded-xl border border-[#EAECF0]" data-testid="inbound-receipts-empty">
              Nothing pending — every shipped STO for this site has been received.
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-[#EAECF0] overflow-x-auto" data-testid="inbound-receipts-list">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>STO</TableHead>
                    <TableHead>Route</TableHead>
                    <TableHead>Ship To</TableHead>
                    <TableHead>Shipped On</TableHead>
                    <TableHead>Lines</TableHead>
                    <TableHead className="min-w-[200px]">Status</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {orders.map((order) => {
                    const status = STATUS_STYLE[order.receipt_status] || STATUS_STYLE.pending;
                    const busyElsewhere = Boolean(order.active_job);
                    return (
                      <TableRow key={order.sto_id} data-testid={`inbound-receipt-row-${order.sto_id}`}>
                        <TableCell>
                          <span className="font-semibold text-[#101828]">{order.sto_id}</span>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-sm text-[#344054]">
                            {order.ship_from_site_id} <ArrowRight size={12} /> {order.ship_to_site_id}
                          </span>
                        </TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.ship_to_location_name || "—"}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{formatDate(order.created_at)}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.items.length}</TableCell>
                        <TableCell>
                          <span className={`text-xs font-medium px-2 py-1 rounded-full ${status.cls}`} data-testid={`inbound-receipt-status-${order.sto_id}`}>
                            {status.label}
                          </span>
                          {order.receipt_error && (
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs whitespace-normal break-words" data-testid={`inbound-receipt-error-${order.sto_id}`}>{order.receipt_error}</div>
                          )}
                          {busyElsewhere && (
                            <div className="flex items-center gap-1 text-xs font-medium mt-1 text-[#0B6B74]" data-testid={`inbound-receipt-busy-${order.sto_id}`}>
                              <CircleNotch size={12} className="animate-spin" /> Receiving…
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            size="sm" disabled={busyElsewhere}
                            onClick={() => setModalState({ order, mode: "receive" })}
                            data-testid={`inbound-receipt-receive-btn-${order.sto_id}`}
                          >
                            Receive
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )
        )}

        {activeTab === "completed" && (
          completedLoading ? (
            <div className="flex items-center justify-center py-24 text-[#667085]" data-testid="inbound-receipts-completed-loading">
              <CircleNotch size={24} className="animate-spin mr-2" /> Loading completed receipts…
            </div>
          ) : completedOrders.length === 0 ? (
            <div className="text-center py-24 text-[#667085] bg-white rounded-xl border border-[#EAECF0]" data-testid="inbound-receipts-completed-empty">
              No completed receipts found for this filter.
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-[#EAECF0] overflow-x-auto" data-testid="inbound-receipts-completed-list">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>STO</TableHead>
                    <TableHead>Route</TableHead>
                    <TableHead>Ship To</TableHead>
                    <TableHead>Received At</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Time Taken</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {completedOrders.map((order) => {
                    const status = COMPLETED_STATUS_STYLE[order.receipt_status] || COMPLETED_STATUS_STYLE.received;
                    return (
                      <TableRow key={order.sto_id} data-testid={`inbound-receipts-completed-row-${order.sto_id}`}>
                        <TableCell>
                          <span className="font-semibold text-[#101828]">{order.sto_id}</span>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-sm text-[#344054]">
                            {order.ship_from_site_id} <ArrowRight size={12} /> {order.ship_to_site_id}
                          </span>
                        </TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.ship_to_location_name || "—"}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{formatDate(order.received_at || order.receipt_completed_at)}</TableCell>
                        <TableCell>
                          <span className={`text-xs font-medium px-2 py-1 rounded-full ${status.cls}`} data-testid={`inbound-receipts-completed-status-${order.sto_id}`}>
                            {status.label}
                          </span>
                          {order.receipt_error && (
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs whitespace-normal break-words" data-testid={`inbound-receipts-completed-error-${order.sto_id}`}>{order.receipt_error}</div>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm text-[#344054]" data-testid={`inbound-receipts-completed-duration-${order.sto_id}`}>
                          {formatDuration(order.receipt_duration_seconds)}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            size="sm" variant="outline"
                            onClick={() => setModalState({ order, mode: "view" })}
                            data-testid={`inbound-receipts-completed-view-btn-${order.sto_id}`}
                          >
                            View
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )
        )}
      </div>

      {modalState && (
        <ReceiptDetailModal order={modalState.order} mode={modalState.mode} onClose={closeModal} onUpdated={onModalUpdated} />
      )}
    </div>
  );
}
