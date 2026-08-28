import { useState, useEffect, useCallback, Fragment } from "react";
import "@/App.css";
import axios from "axios";
import { Truck, Shield, CircleNotch, CaretDown, CaretUp, CheckCircle, ArrowRight } from "@phosphor-icons/react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const formatQty = (v) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 }));
const formatDate = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");

const STATUS_STYLE = {
  pending: { label: "Pending Receipt", cls: "bg-[#FEF3C7] text-[#92400E]" },
  partial: { label: "Partially Received", cls: "bg-[#FEE4E2] text-[#B42318]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

// Inbound STO Receipt (Aug 28 2026) - lets any logged-in user receive a
// multi-line STO in one click instead of the SAP UI showing one row per
// line. See server.py's /api/inbound-receipts/* and
// inbound_receipt_service.py for the backend logic this drives.
export default function InboundReceiptsPage() {
  const { user } = useAuth();
  const [sites, setSites] = useState([]);
  const [siteFilter, setSiteFilter] = useState("all");
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);
  const [receiveTarget, setReceiveTarget] = useState(null);
  const [qtyEdits, setQtyEdits] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [progressStep, setProgressStep] = useState(null);

  useEffect(() => {
    axios.get(`${API}/inbound-receipts/sites`).then(({ data }) => {
      setSites(data.sites || []);
      const defaultSite = (user?.bound_sites || []).find((s) => (data.sites || []).includes(s));
      if (defaultSite) setSiteFilter(defaultSite);
    }).catch(() => toast.error("Could not load receiving sites."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const openReceive = (order) => {
    setReceiveTarget(order);
    const defaults = {};
    order.items.forEach((it) => { defaults[it.line_no] = it.requested_qty; });
    setQtyEdits(defaults);
  };

  const submitReceive = async () => {
    if (!receiveTarget) return;
    setSubmitting(true);
    setProgressStep("Starting...");
    try {
      const items = receiveTarget.items.map((it) => ({ line_no: it.line_no, received_qty: Number(qtyEdits[it.line_no] ?? it.requested_qty) }));
      const anyPartial = items.some((it) => {
        const line = receiveTarget.items.find((x) => x.line_no === it.line_no);
        return line && Math.abs(it.received_qty - line.requested_qty) > 1e-6;
      });
      const { data: startData } = await axios.post(`${API}/inbound-receipts/${receiveTarget.sto_id}/receive`, { items });
      let result;
      if (startData.already_received) {
        result = startData.result;
      } else {
        result = await pollReceiveJob(startData.job_id);
      }
      if (result.status === "received") {
        toast.success(anyPartial ? `${receiveTarget.sto_id} received as entered — closed in SAP.` : `${receiveTarget.sto_id} received in full — closed in SAP.`);
      } else if (result.status === "partial") {
        toast.warning(`${receiveTarget.sto_id}: some lines received, others failed — check the row for details.`);
      } else {
        toast.error(`${receiveTarget.sto_id}: receipt failed — check the row for details.`);
      }
      setReceiveTarget(null);
      loadOrders();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e?.message || "Could not post the Goods Receipt.");
    } finally {
      setSubmitting(false);
      setProgressStep(null);
    }
  };

  const pollReceiveJob = async (jobId) => {
    const deadline = Date.now() + 6 * 60 * 1000; // multi-line receipts take 40-90s per delivery
    while (Date.now() < deadline) {
      const { data: job } = await axios.get(`${API}/inbound-receipts/receive-status/${jobId}`);
      if (job.progress) setProgressStep(job.progress);
      if (job.status === "done") return job.result;
      if (job.status === "failed") throw new Error(job.error || "Receipt failed");
      await new Promise((r) => setTimeout(r, 2500));
    }
    throw new Error("This is taking longer than expected — check back shortly, the receipt may still complete in the background.");
  };

  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="inbound-receipts-page">
      <Toaster position="top-right" richColors />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
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
      </header>
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-6">
          <div>
            <h1 className="font-heading text-2xl sm:text-3xl font-bold text-[#101828] flex items-center gap-2">
              <Truck size={28} weight="fill" className="text-[#0B6B74]" />
              Inbound STO Receipt
            </h1>
            <p className="text-sm text-[#667085] mt-1">
              Receive a Stock Transfer Order in one click — closes every SAP delivery line for it automatically.
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

        {loading ? (
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
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {orders.map((order) => {
                  const expanded = expandedId === order.sto_id;
                  const status = STATUS_STYLE[order.receipt_status] || STATUS_STYLE.pending;
                  return (
                    <Fragment key={order.sto_id}>
                      <TableRow data-testid={`inbound-receipt-row-${order.sto_id}`}>
                        <TableCell>
                          <button
                            className="flex items-center gap-1 font-semibold text-[#101828] hover:text-[#0B6B74]"
                            onClick={() => setExpandedId(expanded ? null : order.sto_id)}
                            data-testid={`inbound-receipt-toggle-${order.sto_id}`}
                          >
                            {expanded ? <CaretUp size={14} /> : <CaretDown size={14} />}
                            {order.sto_id}
                          </button>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                          {order.outbound_delivery_ids.length > 0 && (
                            <div className="text-[11px] text-[#667085] font-mono mt-0.5" data-testid={`inbound-receipt-delivery-ids-${order.sto_id}`}>
                              SAP Delivery Notif: {order.outbound_delivery_ids.join(", ")}
                            </div>
                          )}
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
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs truncate" title={order.receipt_error}>{order.receipt_error}</div>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button size="sm" onClick={() => openReceive(order)} data-testid={`inbound-receipt-receive-btn-${order.sto_id}`}>
                            Receive
                          </Button>
                        </TableCell>
                      </TableRow>
                      {expanded && (
                        <TableRow key={`${order.sto_id}-detail`}>
                          <TableCell colSpan={7} className="bg-[#F9FAFB]">
                            <div className="py-2">
                              <table className="w-full text-sm">
                                <thead>
                                  <tr className="text-[#667085] text-xs">
                                    <th className="text-left py-1">Line</th>
                                    <th className="text-left py-1">Product</th>
                                    <th className="text-left py-1">Description</th>
                                    <th className="text-right py-1">Qty</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {order.items.map((it) => (
                                    <tr key={it.line_no} data-testid={`inbound-receipt-item-${order.sto_id}-${it.line_no}`}>
                                      <td className="py-1">{it.line_no}</td>
                                      <td className="py-1 font-mono text-xs">{it.product_id}</td>
                                      <td className="py-1">{it.description}</td>
                                      <td className="py-1 text-right">{formatQty(it.requested_qty)} {it.unit_of_measure}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      <Dialog open={!!receiveTarget} onOpenChange={(open) => !open && !submitting && setReceiveTarget(null)}>
        <DialogContent className="max-w-2xl" data-testid="inbound-receipt-dialog">
          <DialogHeader>
            <DialogTitle>Receive {receiveTarget?.sto_id}</DialogTitle>
            <DialogDescription>
              Received Qty defaults to the full shipped quantity — edit any line if the receiving site got a different amount. Confirming posts the Goods Receipt directly in SAP and closes this STO.
            </DialogDescription>
            {receiveTarget?.outbound_delivery_ids?.length > 0 && (
              <p className="text-xs text-[#98A2B3] font-mono" data-testid="inbound-receipt-dialog-delivery-ids">
                SAP created {receiveTarget.outbound_delivery_ids.length} separate Delivery Notification(s) for this STO: {receiveTarget.outbound_delivery_ids.join(", ")} — receiving here closes all of them in one click.
              </p>
            )}
          </DialogHeader>
          <div className="max-h-96 overflow-y-auto border border-[#EAECF0] rounded-lg">
            <table className="w-full text-sm">
              <thead className="bg-[#F9FAFB] sticky top-0">
                <tr className="text-[#667085] text-xs">
                  <th className="text-left py-2 px-3">Product</th>
                  <th className="text-right py-2 px-3">Shipped Qty</th>
                  <th className="text-right py-2 px-3">Received Qty</th>
                </tr>
              </thead>
              <tbody>
                {receiveTarget?.items.map((it) => (
                  <tr key={it.line_no} className="border-t border-[#EAECF0]">
                    <td className="py-2 px-3">
                      <div className="text-sm font-medium text-[#101828]">{it.product_id}</div>
                      <div className="text-xs text-[#667085]">{it.description}</div>
                    </td>
                    <td className="py-2 px-3 text-right text-[#344054]" data-testid={`inbound-receipt-shipped-qty-${it.line_no}`}>
                      {formatQty(it.requested_qty)} {it.unit_of_measure}
                    </td>
                    <td className="py-2 px-3">
                      <div className="flex items-center justify-end gap-2">
                        <Input
                          type="number"
                          min="0.01"
                          max={it.requested_qty}
                          step="0.01"
                          className="w-24 text-right"
                          value={qtyEdits[it.line_no] ?? it.requested_qty}
                          onChange={(e) => setQtyEdits((prev) => ({ ...prev, [it.line_no]: e.target.value }))}
                          disabled={submitting}
                          data-testid={`inbound-receipt-qty-input-${it.line_no}`}
                        />
                        <span className="text-xs text-[#667085] w-8">{it.unit_of_measure}</span>
                      </div>
                      {Number(qtyEdits[it.line_no] ?? it.requested_qty) > it.requested_qty && (
                        <div className="text-xs text-[#B42318] text-right mt-1" data-testid={`inbound-receipt-qty-error-${it.line_no}`}>
                          Can't exceed shipped qty ({formatQty(it.requested_qty)})
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {submitting && (
            <div className="bg-[#F0F9FA] border border-[#B4E4E8] rounded-lg px-4 py-3" data-testid="inbound-receipt-progress">
              <div className="flex items-center gap-2 text-sm font-medium text-[#0B6B74]">
                <CircleNotch size={16} className="animate-spin shrink-0" />
                <span data-testid="inbound-receipt-progress-step">{progressStep || "Working on it..."}</span>
              </div>
              <div className="w-full h-1.5 bg-[#D6EEF0] rounded-full mt-2 overflow-hidden">
                <div className="h-full w-1/3 bg-[#0B6B74] rounded-full animate-[pulse_1.5s_ease-in-out_infinite]" />
              </div>
              <p className="text-xs text-[#0B6B74]/70 mt-2">
                Posting this directly in SAP's own screen can take up to a couple of minutes — please keep this open.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setReceiveTarget(null)} disabled={submitting} data-testid="inbound-receipt-cancel-btn">
              Cancel
            </Button>
            <Button
              onClick={submitReceive}
              disabled={submitting || receiveTarget?.items.some((it) => {
                const qty = Number(qtyEdits[it.line_no] ?? it.requested_qty);
                return !(qty > 0) || qty > it.requested_qty + 1e-6;
              })}
              data-testid="inbound-receipt-confirm-btn"
            >
              {submitting ? <><CircleNotch size={16} className="animate-spin mr-2" /> Receiving…</> : <><CheckCircle size={16} className="mr-2" /> Confirm Receipt</>}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
