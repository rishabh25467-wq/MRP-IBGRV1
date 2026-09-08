import { useState, useEffect, useRef } from "react";
import "@/App.css";
import axios from "axios";
import { Shield,
  MagnifyingGlass,
  Sparkle,
  Trash,
  ArrowRight,
  WarningCircle,
  CheckCircle,
  CircleNotch,
  ArrowsClockwise,
  Robot,
  Printer,
} from "@phosphor-icons/react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { NavTabs } from "@/components/NavTabs";
import { SapConnectionStatus } from "@/components/SapConnectionStatus";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const formatQty = (v) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 }));
const formatSapId = (id) => (id ? id.replace(/^0+(?=\d)/, "") : id);
const cleanSapMessage = (msg) => (msg ? msg.replace(/\s{2,}/g, " ").trim() : msg);
const formatDateTime = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : null);

// Per-line Ship Status (Aug 2026, user's explicit ask - header badge on
// the list view intentionally stays one combined status, this only backs
// the detail modal's "click to see detail" items table).
const SHIP_STATUS_STYLE = {
  shipped: { label: "Shipped", cls: "bg-[#ECFDF3] text-[#027A48]" },
  failed: { label: "Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
  insufficient_stock: { label: "Insufficient Stock", cls: "bg-[#FEF0C7] text-[#93370D]" },
  pending: { label: "Pending", cls: "bg-[#F2F4F7] text-[#475467]" },
};
const lineShipStatus = (order, item) => {
  const list = order.gi_line_status || [];
  const found = list.find((l) => l.line_no != null && l.line_no === item.line_no) || list.find((l) => l.product_id === item.product_id);
  if (found) return found;
  // Older orders posted before gi_line_status existed - fall back to
  // the order's own combined status rather than showing nothing.
  if (order.gi_status === "posted") return { status: "shipped", note: null };
  if (order.gi_status === "failed") return { status: "failed", note: null };
  if (order.gi_status === "insufficient_stock") return { status: "insufficient_stock", note: null };
  return { status: "pending", note: null };
};
const parseGiError = (raw) => {
  if (!raw) return null;
  const jsonMatch = raw.match(/\{.*\}/s);
  if (jsonMatch) {
    try {
      const value = JSON.parse(jsonMatch[0])?.error?.message?.value;
      if (value) return value.split("::").filter(Boolean).join(" — ");
    } catch { /* fall through to raw cleanup below */ }
  }
  return cleanSapMessage(raw);
};
const todayISO = () => new Date().toISOString().slice(0, 10);

// Real (not simulated) steps this app's live SAP write actually goes
// through today - drives the progress bar in the confirm dialog. The
// last step (Goods Issue) is genuinely async - SAP's own scheduling
// decides when the Customer Requirement becomes an Outbound Delivery
// Request, and our own retry loop live-checks real stock before every
// attempt (Aug 27 2026) - the dialog keeps polling it after "Create in
// SAP" finishes so the user can watch it happen, but can also close the
// dialog any time; the background job keeps running regardless.
const STO_STEPS = [
  { key: "validate", label: "Validate order" },
  { key: "check", label: "SAP availability & data check" },
  { key: "create", label: "Create in SAP" },
  { key: "erp_sync", label: "Sync to ERP Portal" },
  { key: "post_goods_issue", label: "Post Goods Issue (SAP delivery)" },
];

// One selected line item. Every field the SAP ByDesign reference screen
// shows per-line (Source Warehouse, Available Qty, Ship-from Site,
// Availability Status) lives here.
const emptyLine = (product) => ({
  key: `${product.product_id}-${Date.now()}`,
  product_id: product.product_id,
  description: product.description,
  unit_of_measure: product.unit_of_measure,
  hsn_code: product.hsn_code,
  locations: product.locations || [],
  source_warehouse_id: "",
  ship_from_site_id: "",
  available_qty: null,
  requested_qty: "",
  suggestion: null,
  error: null,
});
// Shared body for BOTH the create-flow confirm dialog (once the order
// exists in SAP) and the Recent Orders detail modal (user's explicit
// ask, Aug 2026: "the dialog that opens when I create the sto should be
// same as the detail dialog") - one single source of truth for what an
// order's live status looks like, so the two views can never drift
// apart again.
const DebugScreenshotsViewer = ({ stoId }) => {
  const [open, setOpen] = useState(false);
  const [shots, setShots] = useState(null);
  const toggle = async () => {
    if (!open && !shots) {
      try {
        const { data } = await axios.get(`${API}/stock-transfer/orders/${stoId}/debug-screenshots`);
        setShots(data);
      } catch {
        setShots([]);
      }
    }
    setOpen((o) => !o);
  };
  return (
    <div className="mt-2">
      <button type="button" onClick={toggle} className="text-xs text-[#175CD3] underline" data-testid="stock-transfer-debug-screenshots-toggle">
        {open ? "Hide" : "View"} debug screenshots (admin)
      </button>
      {open && (
        <div className="mt-2 grid grid-cols-3 gap-2" data-testid="stock-transfer-debug-screenshots-grid">
          {shots === null ? <p className="text-xs text-[#98A2B3]">Loading...</p>
            : shots.length === 0 ? <p className="text-xs text-[#98A2B3]">No debug screenshots yet for this order.</p>
            : shots.map((s) => (
              <a key={s.filename} href={`${API}/stock-transfer/debug-screenshots/${s.filename}`} target="_blank" rel="noreferrer" className="block border border-[#EAECF0] rounded-sm overflow-hidden hover:opacity-80">
                <img src={`${API}/stock-transfer/debug-screenshots/${s.filename}`} alt={s.step} className="w-full h-20 object-cover" />
                <p className="text-[10px] px-1 py-0.5 bg-[#F9FAFB] truncate">{s.step}</p>
              </a>
            ))}
        </div>
      )}
    </div>
  );
};

export const OrderDetailBody = ({ order, retryingStoId, onRetryOrder, retryingErpStoId, onRetryErpSync, retryingGiStoId, onRetryGoodsIssue, stoppingGiStoId, onForceStopGi, isAdmin, notifications, activateResults, activatingId, confirmActivateFor, setConfirmActivateFor, onActivate }) => {
  if (!order) return null;
  // User's explicit ask (Sep 3 2026): the "Activate this site" fix action
  // for a "No valid planning data..." rejection used to live ONLY in the
  // separate "Action Needed" banner above the orders list - easy to miss
  // when looking at this order's own detail modal instead. Surface the
  // exact same action here too, for this specific order, when it applies.
  const matchingNotification = (notifications || []).find((n) => n.sto_id === order.sto_id);
  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-[#667085]">Created {new Date(order.created_at).toLocaleString("en-IN")} by {order.created_by}</p>
        {order.gi_status === "posted" && (
          <Button
            size="sm" variant="outline" className="text-xs h-7 shrink-0"
            onClick={() => window.open(`/inventory/inter-plant-transfer/${order.sto_id}/delivery-note`, "_blank")}
            data-testid="stock-transfer-print-delivery-note-btn"
          >
            <Printer size={13} className="mr-1" /> Print Delivery Note
          </Button>
        )}
      </div>

      {order.error_message ? (
        <div className="bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-3 text-sm text-[#912018] flex items-start gap-2" data-testid="stock-transfer-detail-error">
          <WarningCircle size={16} className="mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="font-bold">SAP rejected this order:</p>
            <p className="mt-0.5">{cleanSapMessage(order.error_message)}</p>
            <Button
              size="sm" variant="outline" className="mt-2"
              onClick={() => onRetryOrder(order.sto_id)}
              disabled={retryingStoId === order.sto_id}
              data-testid="stock-transfer-detail-retry-button"
            >
              {retryingStoId === order.sto_id ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}
              Retry this order
            </Button>

            {matchingNotification && (
              <div className="mt-3 bg-[#FFFAEB] border border-[#FEC84B] rounded-sm p-2 space-y-2" data-testid="stock-transfer-detail-activate-panel">
                <p className="text-[#93370D]">
                  Product <span className="font-bold">{matchingNotification.product_id}</span> has no Planning/Valuation data set up at site <span className="font-bold">{matchingNotification.site_id}</span> - this is why SAP rejected it.
                </p>
                {(() => {
                  const result = activateResults?.[matchingNotification._id];
                  if (result) {
                    return (
                      <div className="space-y-1" data-testid={`stock-transfer-detail-activate-result-${matchingNotification._id}`}>
                        <p className={result.planning_logistics === "ok" ? "text-[#027A48] font-bold" : "text-[#B42318] font-bold"}>
                          Planning / Availability / Logistics: {result.planning_logistics === "ok" ? "Activated successfully." : cleanSapMessage(result.planning_logistics)}
                        </p>
                        {result.valuation && (
                          <p className={result.valuation === "ok" ? "text-[#027A48]" : "text-[#B42318]"}>
                            Valuation: {result.valuation === "ok" ? "Activated successfully." : cleanSapMessage(result.valuation)}
                          </p>
                        )}
                        {result.planning_logistics === "ok" && (
                          <Button size="sm" variant="outline" onClick={() => onRetryOrder(matchingNotification.sto_id)} disabled={retryingStoId === matchingNotification.sto_id} data-testid={`stock-transfer-detail-activate-retry-${matchingNotification.sto_id}`}>
                            {retryingStoId === matchingNotification.sto_id ? <CircleNotch size={14} className="animate-spin" /> : `Retry ${matchingNotification.sto_id}`}
                          </Button>
                        )}
                      </div>
                    );
                  }
                  if (confirmActivateFor === matchingNotification._id) {
                    return (
                      <div className="flex items-center gap-2 bg-white border border-[#FEC84B] rounded-sm p-2">
                        <span className="text-[#93370D]">This writes directly to live SAP master data - are you sure?</span>
                        <Button size="sm" onClick={() => onActivate(matchingNotification)} disabled={activatingId === matchingNotification._id} data-testid={`stock-transfer-detail-activate-confirm-${matchingNotification._id}`}>
                          {activatingId === matchingNotification._id ? <CircleNotch size={14} className="animate-spin" /> : "Yes, activate"}
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirmActivateFor(null)} data-testid={`stock-transfer-detail-activate-cancel-${matchingNotification._id}`}>Cancel</Button>
                      </div>
                    );
                  }
                  return (
                    <Button size="sm" variant="outline" onClick={() => setConfirmActivateFor(matchingNotification._id)} data-testid={`stock-transfer-detail-activate-button-${matchingNotification._id}`}>
                      Activate {matchingNotification.site_id} for {matchingNotification.product_id}
                    </Button>
                  );
                })()}
              </div>
            )}
          </div>
        </div>
      ) : order.status === "created_in_sap" ? (
        <div className="bg-[#ECFDF3] border border-[#ABEFC6] rounded-sm p-3 text-sm text-[#027A48] flex items-start gap-2" data-testid="stock-transfer-detail-success">
          <CheckCircle size={16} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-bold">Created in SAP.</p>
            <p className="mt-0.5">SAP Order ID: {formatSapId(order.sap_order_id) || "—"}{order.sap_order_uuid ? ` (UUID: ${order.sap_order_uuid})` : ""}</p>
          </div>
        </div>
      ) : (
        <div className="bg-[#FEF0C7] border border-[#FEDF89] rounded-sm p-3 text-sm text-[#93370D]" data-testid="stock-transfer-detail-no-error">
          Submitting to SAP now - refresh in a few seconds if this doesn't update.
        </div>
      )}

      {order.status === "created_in_sap" && (
        <div
          className={`rounded-sm p-3 text-sm flex items-start gap-2 ${
            order.erp_portal_status === "synced" ? "bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
            : order.erp_portal_status === "failed" ? "bg-[#FEF3F2] border border-[#FDA29B] text-[#912018]"
            : "bg-[#FEF0C7] border border-[#FEDF89] text-[#93370D]"
          }`}
          data-testid="stock-transfer-detail-erp-status"
        >
          {order.erp_portal_status === "synced" ? <CheckCircle size={16} className="mt-0.5 shrink-0" /> : order.erp_portal_status === "failed" ? <WarningCircle size={16} className="mt-0.5 shrink-0" /> : <CircleNotch size={16} className="mt-0.5 shrink-0 animate-spin" />}
          <div>
            <p className="font-bold">
              {order.erp_portal_status === "synced" ? "Synced to ERP Portal."
                : order.erp_portal_status === "failed" ? "ERP Portal sync failed:"
                : "ERP Portal: syncing..."}
            </p>
            {order.erp_portal_status === "failed" && <p className="mt-0.5">{order.erp_portal_error || "See logs."}</p>}
            {order.erp_portal_status !== "synced" && (
              <>
                <p className="mt-1 text-xs opacity-80">
                  {order.erp_portal_status === "failed"
                    ? "No legal Delivery Challan can be printed until this syncs - Serial Number comes from the ERP portal."
                    : "Looks stuck? Click Retry ERP Sync - safe to click anytime before this shows Synced."}
                </p>
                <Button
                  size="sm" variant="outline" className="mt-2"
                  onClick={() => onRetryErpSync(order.sto_id)}
                  disabled={retryingErpStoId === order.sto_id}
                  data-testid="stock-transfer-retry-erp-sync-btn"
                >
                  {retryingErpStoId === order.sto_id ? <CircleNotch size={14} className="animate-spin mr-1.5" /> : null}
                  Retry ERP Sync
                </Button>
              </>
            )}
            {order.erp_portal_status === "synced" && (
              <p className="mt-0.5 text-xs opacity-80">Portal Sale No: {order.erp_sale_no} / {order.erp_sale_noc}</p>
            )}
          </div>
        </div>
      )}

      {order.status === "created_in_sap" && (
        <div
          className={`rounded-sm p-3 text-sm flex items-start gap-2 ${
            order.gi_status === "posted" ? "bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
            : order.gi_status === "failed" || order.gi_status === "not_found_timeout" ? "bg-[#FEF3F2] border border-[#FDA29B] text-[#912018]"
            : "bg-[#FEF0C7] border border-[#FEDF89] text-[#93370D]"
          }`}
          data-testid="stock-transfer-detail-gi-status"
        >
          {order.gi_status === "posted" ? <CheckCircle size={16} className="mt-0.5 shrink-0" /> : <WarningCircle size={16} className="mt-0.5 shrink-0" />}
          <div className="flex-1">
            <p className="font-bold">
              {order.gi_status === "posted" ? "Goods Issue posted - delivery released."
                : order.gi_status === "failed" ? "Goods Issue failed:"
                : order.gi_status === "not_found_timeout" ? "Goods Issue still pending after 20 min - the order itself is unaffected."
                : order.gi_status === "insufficient_stock" ? "Goods Issue: insufficient live stock at the source warehouse."
                : order.gi_progress_phase === "opening_delivery" ? `Goods Issue: opening delivery ${order.gi_delivery_request_id || ""} in SAP...`
                : order.gi_progress_phase === "posting_goods_issue" ? `Goods Issue: posting delivery ${order.gi_delivery_request_id || ""} now...`
                : "Goods Issue: checking SAP for the delivery..."}
            </p>
            {!["posted", "failed", "not_found_timeout", "insufficient_stock"].includes(order.gi_status) && (
              <p className="mt-0.5 text-xs opacity-80" data-testid="stock-transfer-detail-gi-progress">Auto-checking every 20s - this can take a few minutes.</p>
            )}
            {order.gi_delivery_request_id && !["posted", "failed"].includes(order.gi_status) && (
              <p className="mt-0.5 text-xs opacity-80" data-testid="stock-transfer-detail-delivery-request-id">Delivery Request found in SAP: {order.gi_delivery_request_id}</p>
            )}
            {order.gi_status === "posted" && formatDateTime(order.gi_posted_at) && (
              <p className="mt-0.5 text-xs opacity-80" data-testid="stock-transfer-detail-gi-posted-at">Posted at: {formatDateTime(order.gi_posted_at)}</p>
            )}
            {(order.gi_status === "failed" || order.gi_status === "insufficient_stock") && <p className="mt-0.5">{parseGiError(order.gi_error) || "See logs."}</p>}
            {order.outbound_delivery_ids?.length > 0 && (order.gi_status === "posted" || order.gi_status === "failed") ? (
              <p className="mt-0.5 text-xs opacity-80" data-testid="stock-transfer-detail-delivery-ids">
                Outbound Delivery: {order.outbound_delivery_ids.join(", ")}
              </p>
            ) : order.outbound_delivery_object_id && (order.gi_status === "posted" || order.gi_status === "failed") && (
              <p className="mt-0.5 text-xs opacity-80" data-testid="stock-transfer-detail-legacy-object-id">Outbound Delivery Request: {order.outbound_delivery_object_id}</p>
            )}
            {(order.gi_status === "failed" || order.gi_status === "not_found_timeout") && (
              <Button
                size="sm" variant="outline" className="mt-2"
                onClick={() => onRetryGoodsIssue(order.sto_id)}
                disabled={retryingGiStoId === order.sto_id}
                data-testid="stock-transfer-detail-retry-gi-button"
              >
                {retryingGiStoId === order.sto_id ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}
                Retry Goods Issue
              </Button>
            )}
            {(order.gi_job_running || order.gi_status === "insufficient_stock") && (
              <Button
                size="sm" variant="outline" className="mt-2 text-[#912018] border-[#FDA29B] hover:bg-[#FEF3F2]"
                onClick={() => onForceStopGi(order.sto_id)}
                disabled={stoppingGiStoId === order.sto_id}
                data-testid="stock-transfer-detail-force-stop-gi-button"
              >
                {stoppingGiStoId === order.sto_id ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}
                Force Stop This Job Now
              </Button>
            )}
            {isAdmin && <DebugScreenshotsViewer stoId={order.sto_id} />}
          </div>
        </div>
      )}

      {order.gst_note_pushed && (
        <div
          className="rounded-sm p-3 text-sm flex items-start gap-2 bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
          data-testid="stock-transfer-detail-gst-status"
        >
          <CheckCircle size={16} className="mt-0.5 shrink-0" />
          <p className="font-bold">GST / Transport details recorded on the SAP Customer Requirement note.</p>
        </div>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
        <div><Label className="text-xs font-bold text-[#344054]">Ship-from Site</Label><p data-testid="stock-transfer-detail-ship-from">{order.ship_from_site_id}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Ship-to Site</Label><p data-testid="stock-transfer-detail-ship-to">{order.ship_to_site_id}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Ship-to Location</Label><p data-testid="stock-transfer-detail-ship-to-location">{order.ship_to_location_name || order.ship_to_location_id}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Delivery Priority</Label><p>{order.delivery_priority}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Requested Delivery Date</Label><p>{order.requested_delivery_date}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Transportation Mode</Label><p>{order.transportation_mode || "—"}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Vehicle No.</Label><p>{order.vehicle_no || "—"}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Place Of Supply</Label><p>{order.place_of_supply || "—"}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">G.R No.</Label><p>{order.gr_no || "—"}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Date Of Supply</Label><p>{order.date_of_supply || "—"}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Freight Forwarder</Label><p data-testid="stock-transfer-detail-freight-forwarder">{order.freight_forwarder || "—"}</p></div>
        <div><Label className="text-xs font-bold text-[#344054]">Remark</Label><p data-testid="stock-transfer-detail-remark">{order.remark || "—"}</p></div>
      </div>

      <div className="border border-[#EAECF0] rounded-sm overflow-auto">
        {order.gi_delivery_request_id && (
          <p className="px-2 py-1 text-[11px] text-[#475467] bg-[#F9FAFB] border-b border-[#EAECF0]" data-testid="stock-transfer-detail-items-table-delivery-request">
            Delivery Request in SAP: <span className="font-bold text-[#344054]">{order.gi_delivery_request_id}</span>
            {order.gi_playwright_user && <> &middot; SAP user: <span className="font-bold text-[#344054]">{order.gi_playwright_user}</span></>}
          </p>
        )}
        <table className="w-full text-xs border-collapse" data-testid="stock-transfer-detail-items-table">
          <thead>
            <tr>
              {["Line", "Product", "Description", "HSN Code", "Source Warehouse", "Available Qty", "Requested Qty", "Stock Status", "Ship Status"].map((h) => (
                <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-[11px] font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(order.items || []).map((it, idx) => {
              const ship = lineShipStatus(order, it);
              const shipStyle = SHIP_STATUS_STYLE[ship.status] || SHIP_STATUS_STYLE.pending;
              return (
              <tr key={it.line_no ?? idx} data-testid={`stock-transfer-detail-item-${it.product_id}`}>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.line_no}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{it.product_id}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.description || "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5" data-testid={`stock-transfer-detail-hsn-${it.product_id}`}>{it.hsn_code || "—"}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.source_warehouse_name || it.source_warehouse_id}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.available_qty)} {it.unit_of_measure}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(it.requested_qty)} {it.unit_of_measure}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">{it.availability_status}</td>
                <td className="border border-[#D0D5DD] px-2 py-1.5">
                  <span
                    className={`text-[11px] font-medium px-1.5 py-0.5 rounded-full ${shipStyle.cls}`}
                    title={ship.note || ""}
                    data-testid={`stock-transfer-detail-ship-status-${it.line_no ?? idx}`}
                  >
                    {shipStyle.label}
                  </span>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
};



export default function StockTransferPage() {
  const [items, setItems] = useState([]);
  const [productQuery, setProductQuery] = useState("");
  const [productSuggestions, setProductSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const searchWrapperRef = useRef(null);
  const suggestionDebounceRef = useRef(null);

  const [shipToSiteId, setShipToSiteId] = useState("");
  const [shipToSiteOptions, setShipToSiteOptions] = useState([]);
  const [shipToLocationId, setShipToLocationId] = useState("");
  const [shipToLocationOptions, setShipToLocationOptions] = useState([]);
  const [requestedDeliveryDate, setRequestedDeliveryDate] = useState(todayISO());
  // GST / E-way bill compliance fields (Aug 2026, user's explicit ask) -
  // mandatory; pushed live to SAP as a Note on the Customer Requirement
  // (see stock_transfer_service.py's _build_gst_note_text).
  const [transportationMode, setTransportationMode] = useState("By Road");
  const [vehicleNo, setVehicleNo] = useState("");
  const [placeOfSupply, setPlaceOfSupply] = useState("");
  const [grNo, setGrNo] = useState("");
  const [dateOfSupply, setDateOfSupply] = useState(todayISO());
  // Freight Forwarder / Transporter name (Aug 27 2026, user's explicit
  // ask - mandatory) - written to the SAP GST Note AND the legacy ERP
  // portal's own `Trans` field, printed on the Delivery Note + Gate Pass.
  const [freightForwarder, setFreightForwarder] = useState("");
  // Remark (Sep 2 2026, user's explicit ask) - optional free text, shown
  // right after Freight Forwarder everywhere it appears.
  const [remark, setRemark] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState(null);

  const [nlText, setNlText] = useState("");
  const [nlParsing, setNlParsing] = useState(false);
  const [nlPreview, setNlPreview] = useState(null);

  const [recentOrders, setRecentOrders] = useState([]);
  const [loadingRecent, setLoadingRecent] = useState(true);
  const [selectedOrder, setSelectedOrder] = useState(null);

  // Sep 7 2026, user's explicit ask: filter row above the Recent Stock
  // Transfer Orders table - all client-side over the already-loaded
  // (max 100, most recent) `recentOrders` list, no backend change needed.
  const [filterStoId, setFilterStoId] = useState("");
  const [filterDate, setFilterDate] = useState("");
  const [filterCreatedBy, setFilterCreatedBy] = useState("all");
  const [filterShipFrom, setFilterShipFrom] = useState("all");
  const [filterShipTo, setFilterShipTo] = useState("all");
  const [filterItem, setFilterItem] = useState("");

  const { user } = useAuth();
  const isAdmin = user?.role === "super_admin" || user?.role === "admin";

  // "Action Needed" admin panel (Aug 2026, user's explicit ask): when an
  // STO fails because a product was never set up (Planning/Availability/
  // Logistics/Valuation) at the destination site, super_admin/admin users
  // see it here with a one-click "Activate" - no toast, inline result
  // message only per user's ask, since there's no safe dry-run on SAP's
  // side for this write.
  const [notifications, setNotifications] = useState([]);
  const [activatingId, setActivatingId] = useState(null);
  const [activateResults, setActivateResults] = useState({}); // { [notificationId]: {planning_logistics, valuation} }
  const [confirmActivateFor, setConfirmActivateFor] = useState(null);
  const [retryingStoId, setRetryingStoId] = useState(null);

  const loadNotifications = async () => {
    try {
      const { data } = await axios.get(`${API}/admin/notifications`);
      setNotifications(data.notifications || []);
    } catch {
      setNotifications([]);
    }
  };

  // Sep 3 2026, user's explicit ask: any user who can create Stock
  // Transfer Orders should see + be able to fix a "site not activated"
  // blocker for their own orders, not just admins.
  useEffect(() => { loadNotifications(); }, []);

  const handleActivate = async (notification) => {
    setActivatingId(notification._id);
    try {
      const { data } = await axios.post(`${API}/admin/material-sites/activate`, {
        product_id: notification.product_id,
        site_id: notification.site_id,
        notification_id: notification._id,
      });
      setActivateResults((prev) => ({ ...prev, [notification._id]: data }));
      if (data.planning_logistics === "ok") loadNotifications();
    } catch (e) {
      setActivateResults((prev) => ({ ...prev, [notification._id]: { planning_logistics: e.response?.data?.detail || "Request failed" } }));
    } finally {
      setActivatingId(null);
      setConfirmActivateFor(null);
    }
  };

  const handleRetryOrder = async (stoId) => {
    setRetryingStoId(stoId);
    try {
      await axios.post(`${API}/stock-transfer/orders/${stoId}/retry`);
      setTimeout(() => { loadRecentOrders(); setRetryingStoId(null); }, 4000);
    } catch {
      setRetryingStoId(null);
    }
  };

  const [retryingGiStoId, setRetryingGiStoId] = useState(null);
  const handleRetryGoodsIssue = async (stoId) => {
    setRetryingGiStoId(stoId);
    try {
      await axios.post(`${API}/stock-transfer/orders/${stoId}/retry-goods-issue`);
      toast.message("Retrying Goods Issue - will keep checking live stock automatically for up to 20 minutes.");
      setTimeout(() => { loadRecentOrders(); setRetryingGiStoId(null); }, 3000);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not retry Goods Issue.");
      setRetryingGiStoId(null);
    }
  };

  const [stoppingGiStoId, setStoppingGiStoId] = useState(null);
  const handleForceStopGi = async (stoId) => {
    setStoppingGiStoId(stoId);
    try {
      await axios.post(`${API}/stock-transfer/orders/${stoId}/force-stop-gi`);
      toast.message("Stopped - a Delivery may already exist in SAP for this order, check before retrying.");
      setTimeout(() => { loadRecentOrders(); setStoppingGiStoId(null); }, 1500);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not stop this Goods Issue job.");
      setStoppingGiStoId(null);
    }
  };

  const [retryingErpStoId, setRetryingErpStoId] = useState(null);
  const handleRetryErpSync = async (stoId) => {
    setRetryingErpStoId(stoId);
    try {
      await axios.post(`${API}/stock-transfer/orders/${stoId}/retry-erp-sync`);
      toast.message("Retrying ERP Portal sync (tries primary server, then fallback automatically).");
      setTimeout(() => { loadRecentOrders(); setRetryingErpStoId(null); }, 3000);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not retry ERP Portal sync.");
      setRetryingErpStoId(null);
    }
  };

  // 2x-safety confirmation before the real, irreversible live SAP write
  // (user's explicit ask, Aug 2026): clicking "Create" first opens a
  // summary dialog - nothing is submitted until the user explicitly
  // confirms a SECOND time inside that dialog. The dialog then shows a
  // real (not simulated) step-by-step progress bar driven by the
  // background job's own `step` field.
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [sapSubmitPhase, setSapSubmitPhase] = useState(null); // null | "submitting" | "done" | "failed"
  const [sapSubmitMessage, setSapSubmitMessage] = useState(null);
  const [stepStatuses, setStepStatuses] = useState({}); // { [stepKey]: "pending" | "active" | "done" | "failed" }

  const shipFromSiteId = items.find((i) => i.ship_from_site_id)?.ship_from_site_id || "";

  const refreshItemStock = async (itemKey, productId) => {
    // Per-item "Refresh" (Sep 2026, user's explicit ask) - available as
    // soon as the item is picked, no site needed first. Scoped to just
    // this one product across every site/warehouse - a live SAP pull
    // this narrow takes ~11-17s end to end (verified), well under the
    // ~12s/site or 60s+/company-wide full refresh this replaces.
    setItems((prev) => prev.map((i) => (i.key === itemKey ? { ...i, refreshing: true } : i)));
    try {
      await axios.post(`${API}/stock-transfer/refresh-item-stock`, null, { params: { product_id: productId } });
      const { data: inv } = await axios.get(`${API}/stock-transfer/inventory`, { params: { product_id: productId, include_non_usable: true } });
      setItems((prev) => prev.map((i) => (i.key === itemKey ? { ...i, locations: inv.locations || [], hsn_code: inv.hsn_code } : i)));
      toast.success(`${productId} stock refreshed live from SAP.`);
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message || "Could not refresh item stock.");
    } finally {
      setItems((prev) => prev.map((i) => (i.key === itemKey ? { ...i, refreshing: false } : i)));
    }
  };

  const loadRecentOrders = async () => {
    setLoadingRecent(true);
    try {
      const { data } = await axios.get(`${API}/stock-transfer/orders`);
      setRecentOrders(data);
    } catch {
      setRecentOrders([]);
    } finally {
      setLoadingRecent(false);
    }
  };

  useEffect(() => { loadRecentOrders(); }, []);

  // Sep 7 2026, user's explicit ask: dropdown options + filtered list for
  // the Recent Stock Transfer Orders filter row - derived straight from
  // whatever's already loaded, no extra API call.
  const distinctCreatedBy = [...new Set(recentOrders.map((o) => o.created_by).filter(Boolean))].sort();
  const distinctShipFrom = [...new Set(recentOrders.map((o) => o.ship_from_site_id).filter(Boolean))].sort();
  const distinctShipTo = [...new Set(recentOrders.map((o) => o.ship_to_site_id).filter(Boolean))].sort();
  const filteredRecentOrders = recentOrders.filter((o) => {
    if (filterStoId && !o.sto_id.toLowerCase().includes(filterStoId.trim().toLowerCase())) return false;
    if (filterDate && (o.created_at || "").slice(0, 10) !== filterDate) return false;
    if (filterCreatedBy !== "all" && o.created_by !== filterCreatedBy) return false;
    if (filterShipFrom !== "all" && o.ship_from_site_id !== filterShipFrom) return false;
    if (filterShipTo !== "all" && o.ship_to_site_id !== filterShipTo) return false;
    if (filterItem) {
      const q = filterItem.trim().toLowerCase();
      const matches = (o.items || []).some((it) => (it.product_id || "").toLowerCase().includes(q) || (it.description || "").toLowerCase().includes(q));
      if (!matches) return false;
    }
    return true;
  });
  const hasActiveRecentFilters = !!(filterStoId || filterDate || filterCreatedBy !== "all" || filterShipFrom !== "all" || filterShipTo !== "all" || filterItem);
  const clearRecentFilters = () => {
    setFilterStoId(""); setFilterDate(""); setFilterCreatedBy("all"); setFilterShipFrom("all"); setFilterShipTo("all"); setFilterItem("");
  };

  // Companion to the sync-fix above - without this, the list itself
  // never refreshes on its own either (no other periodic poll exists),
  // so a long-running background job just sits stale until the user
  // manually reloads. Auto-refresh only while something is actually
  // in-flight.
  const hasRunningGiJob = recentOrders.some((o) => o.gi_job_running);
  useEffect(() => {
    if (!hasRunningGiJob) return;
    const id = setInterval(loadRecentOrders, 20000);
    return () => clearInterval(id);
  }, [hasRunningGiJob]);

  // Real bug (user's screenshot, Sep 2026): the detail modal is a static
  // snapshot from whatever row was clicked - it never refreshed on its
  // own, so if a background retry re-acquired a DIFFERENT pooled SAP
  // login (itadmin/STOREBOT1/STOREBOT2 - by design, not sticky across
  // retries) after the modal was opened, it kept showing the stale
  // username while the list table below (refreshed by loadRecentOrders)
  // already showed the current one. Keep it in sync with whatever the
  // list already has, every time the list refreshes.
  useEffect(() => {
    if (!selectedOrder) return;
    const updated = recentOrders.find((o) => o.sto_id === selectedOrder.sto_id);
    if (updated && updated !== selectedOrder) setSelectedOrder(updated);
  }, [recentOrders]);

  // Ship-to Site options depend on the (derived) Ship-from Site - refetch
  // whenever the first line item's warehouse pick resolves/changes it.
  // User's explicit ask: once Ship-to Site/Location are picked they should
  // stay valid for every item added afterwards, not need re-picking - so
  // only clear them if they've actually become INVALID for the (possibly
  // changed) Ship-from Site, never just because something else changed.
  useEffect(() => {
    if (!shipFromSiteId) {
      setShipToSiteOptions([]);
      setShipToSiteId("");
      return;
    }
    axios.get(`${API}/stock-transfer/ship-to-sites`, { params: { ship_from_site_id: shipFromSiteId } })
      .then(({ data }) => {
        const sites = data.sites || [];
        setShipToSiteOptions(sites);
        setShipToSiteId((prev) => (prev && !sites.includes(prev) ? "" : prev));
      })
      .catch(() => setShipToSiteOptions([]));
  }, [shipFromSiteId]);

  // Ship-to Location options depend on the chosen Ship-to Site.
  useEffect(() => {
    if (!shipToSiteId) { setShipToLocationOptions([]); setShipToLocationId(""); return; }
    axios.get(`${API}/stock-transfer/locations`, { params: { site_id: shipToSiteId } })
      .then(({ data }) => setShipToLocationOptions(data.warehouses || []))
      .catch(() => setShipToLocationOptions([]));
  }, [shipToSiteId]);

  useEffect(() => {
    if (suggestionDebounceRef.current) clearTimeout(suggestionDebounceRef.current);
    const q = productQuery.trim();
    if (q.length < 2) { setProductSuggestions([]); return; }
    suggestionDebounceRef.current = setTimeout(async () => {
      try {
        const { data } = await axios.get(`${API}/products/search`, { params: { q, limit: 10 } });
        setProductSuggestions(data);
      } catch {
        setProductSuggestions([]);
      }
    }, 250);
    return () => clearTimeout(suggestionDebounceRef.current);
  }, [productQuery]);

  useEffect(() => {
    const onClickOutside = (e) => {
      if (searchWrapperRef.current && !searchWrapperRef.current.contains(e.target)) setShowSuggestions(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const addItem = async (product_id, description, initialQty) => {
    setProductQuery("");
    setProductSuggestions([]);
    setShowSuggestions(false);
    if (items.some((i) => i.product_id === product_id)) {
      toast.error(`${product_id} is already in the line item list.`);
      return null;
    }
    try {
      const { data } = await axios.get(`${API}/stock-transfer/inventory`, { params: { product_id, include_non_usable: true } });
      const line = emptyLine({ ...data, description: data.description || description });
      if (initialQty != null) line.requested_qty = String(initialQty);
      setItems((prev) => [...prev, line]);
      fetchSuggestion(line.key, product_id);
      return line;
    } catch {
      toast.error(`Could not check inventory for ${product_id}.`);
      return null;
    }
  };

  const fetchSuggestion = async (key, product_id, shipTo = shipToSiteId) => {
    try {
      const { data } = await axios.get(`${API}/stock-transfer/suggest-source`, {
        params: { product_id, ship_to_site_id: shipTo || undefined },
      });
      setItems((prev) => prev.map((i) => (i.key === key ? { ...i, suggestion: data } : i)));
    } catch {
      // purely advisory - silently skip on failure
    }
  };

  // Ship-to Site changing can invalidate/refresh every line's suggestion
  // (a warehouse at the new Ship-to Site should never be suggested).
  useEffect(() => {
    items.forEach((i) => fetchSuggestion(i.key, i.product_id, shipToSiteId));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shipToSiteId]);

  const removeItem = (key) => setItems((prev) => prev.filter((i) => i.key !== key));

  const chooseWarehouse = (key, warehouseId) => {
    setItems((prev) => prev.map((i) => {
      if (i.key !== key) return i;
      const loc = i.locations.find((l) => l.warehouse_id === warehouseId);
      if (loc && loc.is_usable === false) return i; // Inspection/Restricted rows are shown but never selectable
      return {
        ...i,
        source_warehouse_id: warehouseId,
        ship_from_site_id: loc ? loc.site_id : "",
        available_qty: loc ? loc.qty : null,
        error: null,
      };
    }));
  };

  const applySuggestion = (key) => {
    setItems((prev) => prev.map((i) => {
      if (i.key !== key || !i.suggestion?.suggested) return i;
      const s = i.suggestion.suggested;
      return { ...i, source_warehouse_id: s.warehouse_id, ship_from_site_id: s.site_id, available_qty: s.qty, error: null };
    }));
  };

  const setRequestedQty = (key, value) => {
    setItems((prev) => prev.map((i) => {
      if (i.key !== key) return i;
      const qty = value === "" ? "" : Number(value);
      let error = null;
      if (value !== "" && (isNaN(qty) || qty <= 0)) error = "Requested Quantity must be greater than zero.";
      else if (i.available_qty != null && qty > i.available_qty) {
        error = "Insufficient Stock. Please enter a quantity equal to or less than the available inventory.";
      }
      return { ...i, requested_qty: value, error };
    }));
  };

  const runValidation = () => {
    if (items.length === 0) return "Select at least one item.";
    for (const i of items) {
      if (!i.source_warehouse_id) return `${i.product_id}: Source Warehouse is required.`;
      if (!i.requested_qty || Number(i.requested_qty) <= 0) return `${i.product_id}: Requested Quantity must be greater than zero.`;
      if (i.available_qty != null && Number(i.requested_qty) > i.available_qty) return `${i.product_id}: Insufficient Stock. Please enter a quantity equal to or less than the available inventory.`;
    }
    const sites = new Set(items.map((i) => i.ship_from_site_id));
    if (sites.size > 1) return "All line items must ship from the same Ship-from Site - remove the mismatched item(s) or create a separate order.";
    if (!shipToSiteId) return "Ship-to Site is required.";
    if (shipToSiteId === shipFromSiteId) return "Ship-to Site cannot be the same as Ship-from Site.";
    if (!shipToLocationId) return "Ship-to Location is required.";
    if (!requestedDeliveryDate) return "Requested Delivery Date is required.";
    if (requestedDeliveryDate < todayISO()) return "Requested Delivery Date cannot be earlier than today.";
    if (!transportationMode) return "Transportation Mode is required.";
    if (!vehicleNo.trim()) return "Vehicle No. is required.";
    if (!placeOfSupply.trim()) return "Place Of Supply is required.";
    if (!grNo.trim()) return "G.R No. is required.";
    if (!/^\d+$/.test(grNo.trim())) return "G.R No. must be numeric only.";
    if (!dateOfSupply) return "Date Of Supply is required.";
    if (!freightForwarder.trim()) return "Freight Forwarder is required.";
    return null;
  };

  const openConfirmDialog = () => {
    const validationError = runValidation();
    setFormError(validationError);
    if (validationError) { toast.error(validationError); return; }
    setShowConfirmDialog(true);
  };

  const applyJobStepStatuses = (job) => {
    setStepStatuses((prev) => {
      if (job.status === "done") return { ...prev, check: "done", create: "done", erp_sync: prev.erp_sync || "active", post_goods_issue: prev.post_goods_issue || "active" };
      if (job.status === "failed") {
        if (job.step === "creating") return { ...prev, check: "done", create: "failed" };
        return { ...prev, check: "failed", create: "pending" };
      }
      if (job.step === "creating") return { ...prev, check: "done", create: "active" };
      return { ...prev, check: "active", create: "pending" };
    });
  };

  // Goods Issue progress (Aug 27 2026) - polled by sto_id (not the job_id
  // above, which only covers the fast Validate/Check/Create steps) once
  // the order is created in SAP. Keeps updating the 4th step + a live
  // status line until posted/failed/timed-out, but never blocks the
  // dialog from being closed - the actual retry loop runs server-side
  // regardless of whether this tab is open.
  //
  // `createdOrderLive` holds the FULL order doc (not just gi_status/
  // erp_portal_status) so this dialog can render the exact same
  // <OrderDetailBody> the detail modal uses (user's explicit ask, Aug
  // 2026: "the dialog that opens when I create the sto should be same
  // as the detail dialog") - one shared source of truth for what an
  // order's live status looks like, instead of two hand-written copies
  // that can drift apart.
  const [createdOrderLive, setCreatedOrderLive] = useState(null);
  const giPollStopRef = useRef(false);
  const pollGiStatus = (stoId) => {
    giPollStopRef.current = false;
    const tick = async () => {
      if (giPollStopRef.current) return;
      try {
        const { data } = await axios.get(`${API}/stock-transfer/orders/${stoId}`);
        setCreatedOrderLive(data);
        setStepStatuses((prev) => ({
          ...prev,
          erp_sync: data.erp_portal_status === "synced" ? "done" : data.erp_portal_status === "failed" ? "failed" : "active",
        }));
        const done = data.gi_status === "posted";
        const failed = data.gi_status === "failed" || data.gi_status === "not_found_timeout";
        setStepStatuses((prev) => ({ ...prev, post_goods_issue: done ? "done" : failed ? "failed" : "active" }));
        if (done || failed) { loadRecentOrders(); return; }
      } catch { /* keep polling - a transient blip here shouldn't stop the loop */ }
      if (!giPollStopRef.current) setTimeout(tick, 4000);
    };
    tick();
  };

  const pollSapJob = (jobId) => {
    const start = Date.now();
    const tick = async () => {
      try {
        const { data } = await axios.get(`${API}/stock-transfer/orders/sap-status/${jobId}`);
        applyJobStepStatuses(data);
        if (data.status === "done") {
          setSapSubmitPhase("done");
          setSapSubmitMessage(`Created in SAP as ${formatSapId(data.result?.sap_order_id) || "—"}. Now posting Goods Issue...`);
          toast.success(`Stock Transfer Order created in SAP (${formatSapId(data.result?.sap_order_id) || "—"}).`);
          setItems([]);
          setShipToSiteId(""); setShipToLocationId(""); setRequestedDeliveryDate(todayISO()); setFormError(null);
          setVehicleNo(""); setPlaceOfSupply(""); setGrNo(""); setDateOfSupply(todayISO()); setTransportationMode("By Road"); setFreightForwarder(""); setRemark("");
          loadRecentOrders();
          if (data.sto_id) pollGiStatus(data.sto_id);
          return;
        }
        if (data.status === "failed") {
          setSapSubmitPhase("failed");
          setSapSubmitMessage(cleanSapMessage(data.error) || "SAP rejected this order.");
          toast.error(cleanSapMessage(data.error) || "SAP rejected this Stock Transfer Order.");
          loadRecentOrders();
          return;
        }
        if (Date.now() - start > 3 * 60 * 1000) {
          setSapSubmitPhase("failed");
          setSapSubmitMessage("Still waiting on SAP after 3 minutes - check the order's row/detail view shortly.");
          loadRecentOrders();
          return;
        }
        setTimeout(tick, 3000);
      } catch {
        setTimeout(tick, 3000);
      }
    };
    tick();
  };

  const confirmAndSubmit = async () => {
    setSubmitting(true);
    setSapSubmitPhase("submitting");
    setSapSubmitMessage(null);
    setStepStatuses({ validate: "active", check: "pending", create: "pending", erp_sync: "pending", post_goods_issue: "pending" });
    setCreatedOrderLive(null);
    try {
      const payload = {
        ship_to_site_id: shipToSiteId,
        ship_to_location_id: shipToLocationId,
        requested_delivery_date: requestedDeliveryDate,
        transportation_mode: transportationMode,
        vehicle_no: vehicleNo.trim(),
        place_of_supply: placeOfSupply.trim(),
        gr_no: grNo.trim(),
        date_of_supply: dateOfSupply,
        freight_forwarder: freightForwarder.trim(),
        remark: remark.trim(),
        items: items.map((i) => ({
          product_id: i.product_id,
          source_warehouse_id: i.source_warehouse_id,
          requested_qty: Number(i.requested_qty),
        })),
      };
      const { data } = await axios.post(`${API}/stock-transfer/orders`, payload);
      setStepStatuses({ validate: "done", check: "active", create: "pending", erp_sync: "pending", post_goods_issue: "pending" });
      toast.message(`Stock Transfer Order ${data.sto_id} saved - submitting live to SAP...`);
      loadRecentOrders();
      if (data.sap_job_id) pollSapJob(data.sap_job_id);
    } catch (e) {
      const msg = e?.response?.data?.detail || "Could not create the Stock Transfer Order.";
      setFormError(msg);
      toast.error(msg);
      setSapSubmitPhase(null);
      setStepStatuses({ validate: "failed", check: "pending", create: "pending", erp_sync: "pending", post_goods_issue: "pending" });
    } finally {
      setSubmitting(false);
    }
  };

  const parseNaturalLanguage = async () => {
    if (!nlText.trim()) return;
    setNlParsing(true);
    setNlPreview(null);
    try {
      const { data } = await axios.post(`${API}/stock-transfer/parse-nl`, { text: nlText.trim() });
      setNlPreview(data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not parse that request.");
    } finally {
      setNlParsing(false);
    }
  };

  const applyNlPreview = async () => {
    if (!nlPreview) return;
    let addedLine = null;
    if (nlPreview.product_id) {
      addedLine = await addItem(nlPreview.product_id, null, nlPreview.quantity);
    }
    if (nlPreview.ship_to_site_id) setShipToSiteId(nlPreview.ship_to_site_id);
    if (nlPreview.requested_delivery_date) { setRequestedDeliveryDate(nlPreview.requested_delivery_date); setDateOfSupply(nlPreview.requested_delivery_date); }
    // Auto-apply the AI's top-suggested Source Warehouse too (Aug 27
    // 2026, user's explicit ask) - without a resolved Source Warehouse,
    // Ship-from Site stays blank, which keeps Ship-to Site/Location
    // disabled - so the 2 lines above would have nowhere to take effect.
    // Only ever picks a USABLE location, same rule as the manual
    // "Suggested: ..." hint.
    if (addedLine) {
      try {
        const { data } = await axios.get(`${API}/stock-transfer/suggest-source`, {
          params: { product_id: addedLine.product_id, ship_to_site_id: nlPreview.ship_to_site_id || undefined },
        });
        if (data?.suggested) {
          chooseWarehouse(addedLine.key, data.suggested.warehouse_id);
          toast.message(`Auto-picked Source Warehouse: ${data.suggested.site_id} / ${data.suggested.warehouse_name || data.suggested.warehouse_id}.`);
        }
      } catch { /* purely advisory - the form is still fully usable without it */ }
    }
    setNlPreview(null);
    setNlText("");
    toast.message("Applied to the form below - review before creating.");
  };

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F2F4F7] text-[#1D2939]" data-testid="stock-transfer-page">
      <Toaster position="top-right" richColors />
      <header className="h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center justify-between px-3 sm:px-5 shrink-0 z-10 gap-2 sm:gap-4">
        <div className="flex items-center gap-3 shrink-0" data-testid="app-title">
          <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
            <Shield size={18} weight="fill" className="text-white" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub</span>
            <span className="font-sans text-[12px] text-white/70 hidden sm:inline">Inter-Plant Stock Transfer</span>
          </div>
        </div>
        <div className="w-px h-7 bg-white/25 shrink-0" />
        <div className="flex items-center gap-3 flex-1 justify-start min-w-0"><NavTabs /></div>
        <SapConnectionStatus />
      </header>

      <main className="flex-1 overflow-auto max-w-[1400px] w-full mx-auto px-6 py-6 space-y-4">
        <div>
          <h1 className="font-heading text-xl font-bold text-[#1D2939]">Inter-Plant Stock Transfer</h1>
          <p className="text-sm text-[#667085] mt-0.5">Create a Stock Transfer Order to move inventory between sites - submitted live to SAP immediately after you confirm.</p>
        </div>

        {/* AI natural-language entry (test feature) */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-2" data-testid="stock-transfer-nl-card">
          <div className="flex items-center gap-1.5">
            <Robot size={15} className="text-[#0E7C86]" />
            <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">AI Quick Entry (test)</h3>
          </div>
          <div className="flex gap-2">
            <Input
              value={nlText}
              onChange={(e) => setNlText(e.target.value)}
              placeholder='e.g. "transfer 500 of ITEM-001 to P2 by tomorrow"'
              className="flex-1"
              data-testid="stock-transfer-nl-input"
              onKeyDown={(e) => e.key === "Enter" && parseNaturalLanguage()}
            />
            <Button type="button" variant="outline" disabled={nlParsing || !nlText.trim()} onClick={parseNaturalLanguage} data-testid="stock-transfer-nl-parse-button">
              <Sparkle size={13} className="mr-1.5" /> {nlParsing ? "Parsing..." : "Parse"}
            </Button>
          </div>
          {nlPreview && (
            <div className="bg-[#F9FAFB] border border-[#EAECF0] rounded-sm p-3 text-xs space-y-1" data-testid="stock-transfer-nl-preview">
              <p className="text-[#344054]">Product: <b>{nlPreview.product_id || "—"}</b> · Qty: <b>{nlPreview.quantity ?? "—"}</b> · Ship-to: <b>{nlPreview.ship_to_site_id || "—"}</b> · Delivery: <b>{nlPreview.requested_delivery_date || "—"}</b></p>
              <p className="text-[#98A2B3]">Model: {nlPreview.model_used} - review carefully, nothing is created yet.</p>
              <Button type="button" size="sm" className="mt-1" onClick={applyNlPreview} data-testid="stock-transfer-nl-apply-button">Apply to Form</Button>
            </div>
          )}
        </div>

        {/* Item search */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3">
          <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">1. Select Item(s)</h3>
          <div className="relative max-w-md" ref={searchWrapperRef}>
            <MagnifyingGlass size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#98A2B3]" />
            <Input
              value={productQuery}
              onChange={(e) => { setProductQuery(e.target.value); setShowSuggestions(true); }}
              onFocus={() => setShowSuggestions(true)}
              placeholder="Search Product ID or description..."
              className="pl-8"
              data-testid="stock-transfer-product-search-input"
            />
            {showSuggestions && productSuggestions.length > 0 && (
              <div className="absolute z-20 mt-1 w-full bg-white border border-[#D0D5DD] rounded-sm shadow-lg max-h-64 overflow-y-auto" data-testid="stock-transfer-product-suggestions">
                {productSuggestions.map((s) => (
                  <button
                    key={s.product_id}
                    type="button"
                    className="w-full text-left px-3 py-2 text-xs hover:bg-[#F9FAFB] border-b border-[#EAECF0] last:border-0"
                    onClick={() => addItem(s.product_id, s.description)}
                    data-testid={`stock-transfer-product-suggestion-${s.product_id}`}
                  >
                    <span className="font-medium text-[#344054]">{s.product_id}</span>
                    {s.description && <span className="text-[#667085]"> - {s.description}</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Line item table */}
        {items.length > 0 && (
          <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="stock-transfer-line-items-card">
            <div className="px-3 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB]">
              <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">2-4. Inventory Check / Source Warehouse / Ship-from Site</h3>
            </div>
            <table className="w-full text-xs border-collapse min-w-[900px]" data-testid="stock-transfer-line-items-table">
              <thead>
                <tr>
                  {["Product", "Source Warehouse", "Ship-from Site", "Available Qty", "Requested Qty", "Status", ""].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-xs font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.key} data-testid={`stock-transfer-line-${i.product_id}`}>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <div className="flex items-center gap-1.5">
                        <span className="font-medium text-[#344054]">{i.product_id}</span>
                        <button
                          type="button"
                          title="Refresh live stock for this item from SAP (all warehouses)"
                          onClick={() => refreshItemStock(i.key, i.product_id)}
                          disabled={i.refreshing}
                          data-testid={`stock-transfer-refresh-item-${i.product_id}`}
                          className="text-[#0E7C86] hover:text-[#095b62] disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {i.refreshing ? <CircleNotch size={13} className="animate-spin" /> : <ArrowsClockwise size={13} />}
                        </button>
                      </div>
                      <div className="text-[#667085]">{i.description || "—"}</div>
                      <div className="text-[10px] mt-0.5" data-testid={`stock-transfer-hsn-${i.product_id}`}>
                        {i.hsn_code
                          ? <span className="text-[#0E7C86] font-medium">HSN: {i.hsn_code}</span>
                          : <span className="text-[#98A2B3]">HSN: Not maintained in SAP</span>}
                      </div>
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 min-w-[220px]">
                      <Select value={i.source_warehouse_id} onValueChange={(v) => chooseWarehouse(i.key, v)}>
                        <SelectTrigger className="h-8 text-xs" data-testid={`stock-transfer-warehouse-select-${i.product_id}`}>
                          <SelectValue placeholder={
                            i.locations.filter((l) => l.is_usable).length === 0
                              ? "No usable stock found"
                              : (shipFromSiteId && !i.locations.some((l) => l.site_id === shipFromSiteId) ? `No stock at ${shipFromSiteId}` : "Choose warehouse")
                          } />
                        </SelectTrigger>
                        <SelectContent>
                          {i.locations
                            .filter((l) => !shipFromSiteId || l.site_id === shipFromSiteId || l.warehouse_id === i.source_warehouse_id)
                            // Aug 27 2026 fix: with include_non_usable=true, the
                            // SAME warehouse_id can appear twice (a usable row +
                            // a non-usable Inspection/Restricted row) - dedupe by
                            // warehouse_id (backend already sorts usable-first)
                            // to avoid React's duplicate-key warning/option glitch.
                            .filter((l, idx, arr) => arr.findIndex((x) => x.warehouse_id === l.warehouse_id) === idx)
                            .map((l) => (
                              <SelectItem
                                key={l.warehouse_id}
                                value={l.warehouse_id}
                                disabled={l.is_usable === false}
                                className={l.is_usable === false ? "text-[#98A2B3]" : undefined}
                                data-testid={l.is_usable === false ? `stock-transfer-warehouse-nonusable-${i.product_id}-${l.warehouse_id}` : undefined}
                              >
                                {l.site_id} - {l.warehouse_name || l.warehouse_id} ({formatQty(l.qty)})
                                {l.is_usable === false ? ` - ${l.stock_status || "Restricted"}, not available` : ""}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                      {i.suggestion?.suggested && i.suggestion.suggested.warehouse_id !== i.source_warehouse_id
                        && (!shipFromSiteId || i.suggestion.suggested.site_id === shipFromSiteId) && (
                        <button
                          type="button"
                          onClick={() => applySuggestion(i.key)}
                          className="mt-1 flex items-center gap-1 text-[11px] text-[#0E7C86] hover:underline"
                          data-testid={`stock-transfer-suggestion-${i.product_id}`}
                        >
                          <Sparkle size={11} /> Suggested: {i.suggestion.suggested.site_id} / {i.suggestion.suggested.warehouse_name || i.suggestion.suggested.warehouse_id}
                        </button>
                      )}
                      {i.suggestion?.suggested && shipFromSiteId && i.suggestion.suggested.site_id !== shipFromSiteId
                        && !i.locations.some((l) => l.site_id === shipFromSiteId && l.is_usable) && (
                        <p className="mt-1 text-[11px] text-[#98A2B3]">No stock at {shipFromSiteId} for this item - it would need a separate order.</p>
                      )}
                      {i.suggestion && !i.suggestion.suggested && (
                        <p className="mt-1 text-[11px] text-[#98A2B3]">{i.suggestion.reason}</p>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-[#667085]">{i.ship_from_site_id || "—"}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(i.available_qty)} {i.unit_of_measure}</td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <Input
                        type="number"
                        value={i.requested_qty}
                        onChange={(e) => setRequestedQty(i.key, e.target.value)}
                        className="h-8 text-xs w-28"
                        data-testid={`stock-transfer-qty-input-${i.product_id}`}
                      />
                      {i.error && <p className="text-[11px] text-[#B42318] font-bold mt-1" data-testid={`stock-transfer-qty-error-${i.product_id}`}>{i.error}</p>}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      {i.locations.filter((l) => l.is_usable).length === 0 ? (
                        <span className="inline-flex items-center gap-1 text-[#B42318] font-bold"><WarningCircle size={12} /> No Stock</span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[#027A48] font-bold"><CheckCircle size={12} /> Available</span>
                      )}
                    </td>
                    <td className="border border-[#D0D5DD] px-2 py-1.5">
                      <button type="button" onClick={() => removeItem(i.key)} className="text-[#B42318] hover:text-[#912018]" data-testid={`stock-transfer-remove-${i.product_id}`}>
                        <Trash size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Header fields: Ship-to Site / Location / Priority / Date */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm p-4 space-y-3">
          <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">5-9. Ship-to Site, Location, Delivery Priority &amp; Date</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div>
              <Label className="text-xs font-bold text-[#344054]">Ship-from Site</Label>
              <Input value={shipFromSiteId || ""} readOnly disabled placeholder="Pick a source warehouse above" data-testid="stock-transfer-ship-from-site" />
              <p className="text-[11px] text-[#98A2B3] mt-0.5">Auto-determined from Source Warehouse.</p>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Ship-to Site*</Label>
              <Select value={shipToSiteId} onValueChange={setShipToSiteId} disabled={!shipFromSiteId}>
                <SelectTrigger data-testid="stock-transfer-ship-to-site-select"><SelectValue placeholder="Choose Ship-to Site" /></SelectTrigger>
                <SelectContent>
                  {shipToSiteOptions.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Ship-to Location*</Label>
              <Select value={shipToLocationId} onValueChange={setShipToLocationId} disabled={!shipToSiteId}>
                <SelectTrigger data-testid="stock-transfer-ship-to-location-select"><SelectValue placeholder="Choose Location" /></SelectTrigger>
                <SelectContent>
                  {shipToLocationOptions.map((w) => <SelectItem key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_name || w.warehouse_id}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-xs font-bold text-[#344054]">Delivery Priority</Label>
              <Input value="Immediate" readOnly disabled data-testid="stock-transfer-delivery-priority" />
            </div>
          </div>
          <div className="max-w-xs">
            <Label className="text-xs font-bold text-[#344054]">Requested Delivery Date*</Label>
            <Input
              type="date"
              min={todayISO()}
              value={requestedDeliveryDate}
              onChange={(e) => {
                // Aug 27 2026, user's explicit ask: Date Of Supply follows
                // whatever is entered here, so it doesn't have to be
                // re-typed - still independently editable afterward below.
                setRequestedDeliveryDate(e.target.value);
                setDateOfSupply(e.target.value);
              }}
              data-testid="stock-transfer-delivery-date-input"
            />
          </div>
        </div>

        {/* GST / E-way bill compliance fields (Aug 2026, user's explicit
            ask) - mandatory before the order can move forward, even
            though pushing these into SAP itself is pending Basis. */}
        <div className="grid grid-cols-1 sm:grid-cols-3 md:grid-cols-6 gap-3">
          <div>
            <Label className="text-xs font-bold text-[#344054]">Transportation Mode*</Label>
            <Select value={transportationMode} onValueChange={setTransportationMode}>
              <SelectTrigger data-testid="stock-transfer-transportation-mode-select"><SelectValue /></SelectTrigger>
              <SelectContent>
                {["By Road", "By Rail", "By Air", "By Self"].map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Vehicle No.*</Label>
            <Input value={vehicleNo} onChange={(e) => setVehicleNo(e.target.value.toUpperCase())} placeholder="e.g. UP85ET2398" data-testid="stock-transfer-vehicle-no-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Place Of Supply*</Label>
            <Input value={placeOfSupply} onChange={(e) => setPlaceOfSupply(e.target.value)} placeholder="e.g. Uttar Pradesh" data-testid="stock-transfer-place-of-supply-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">G.R No.*</Label>
            <Input value={grNo} onChange={(e) => setGrNo(e.target.value.replace(/\D/g, ""))} inputMode="numeric" placeholder="e.g. 6839" data-testid="stock-transfer-gr-no-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Date Of Supply*</Label>
            <Input type="date" value={dateOfSupply} onChange={(e) => setDateOfSupply(e.target.value)} data-testid="stock-transfer-date-of-supply-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Freight Forwarder*</Label>
            <Input value={freightForwarder} onChange={(e) => setFreightForwarder(e.target.value)} placeholder="e.g. Pooja Transport Company" data-testid="stock-transfer-freight-forwarder-input" />
          </div>
          <div>
            <Label className="text-xs font-bold text-[#344054]">Remark</Label>
            <Input value={remark} onChange={(e) => setRemark(e.target.value)} placeholder="Optional note" data-testid="stock-transfer-remark-input" />
          </div>
        </div>


        {formError && (
          <div className="bg-[#FEF3F2] border border-[#FDA29B] rounded-sm p-3 text-sm text-[#912018] flex items-center gap-2" data-testid="stock-transfer-form-error">
            <WarningCircle size={16} /> {formError}
          </div>
        )}

        <Button onClick={openConfirmDialog} disabled={submitting} className="w-full sm:w-auto" data-testid="stock-transfer-create-button">
          Review &amp; Create Stock Transfer Order <ArrowRight size={14} className="ml-1.5" />
        </Button>
        {/* Sep 7 2026, user's explicit ask: hide this aggregated "Action
            Needed" panel from the main page - the exact same Activate
            action already surfaces inline on each affected order's own
            detail view (OrderDetailBody above, via `notifications` prop),
            so this was redundant clutter. Notifications data/activation
            logic is kept (still feeds that per-order view) - only this
            panel's rendering is removed. */}

        {/* Recent orders */}
        <div className="bg-white border border-[#D0D5DD] rounded-sm overflow-x-auto" data-testid="stock-transfer-recent-card">
          <div className="px-3 py-2 border-b border-[#D0D5DD] bg-[#F9FAFB]">
            <h3 className="font-heading text-xs font-bold text-[#1D2939] uppercase tracking-wide">
              Recent Stock Transfer Orders ({hasActiveRecentFilters ? `${filteredRecentOrders.length} of ${recentOrders.length}` : recentOrders.length})
            </h3>
          </div>

          <div className="px-3 py-2.5 border-b border-[#D0D5DD] bg-white flex flex-wrap items-end gap-2" data-testid="stock-transfer-recent-filter-row">
            <div className="w-[120px]">
              <Label className="text-[10px] font-bold text-[#667085] uppercase">STO ID</Label>
              <Input value={filterStoId} onChange={(e) => setFilterStoId(e.target.value)} placeholder="e.g. 165" className="h-8 text-xs" data-testid="stock-transfer-filter-sto-id" />
            </div>
            <div className="w-[140px]">
              <Label className="text-[10px] font-bold text-[#667085] uppercase">Created Date</Label>
              <Input type="date" value={filterDate} onChange={(e) => setFilterDate(e.target.value)} className="h-8 text-xs" data-testid="stock-transfer-filter-date" />
            </div>
            <div className="w-[150px]">
              <Label className="text-[10px] font-bold text-[#667085] uppercase">Created By</Label>
              <Select value={filterCreatedBy} onValueChange={setFilterCreatedBy}>
                <SelectTrigger className="h-8 text-xs" data-testid="stock-transfer-filter-created-by">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Users</SelectItem>
                  {distinctCreatedBy.map((u) => <SelectItem key={u} value={u}>{u}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="w-[130px]">
              <Label className="text-[10px] font-bold text-[#667085] uppercase">Ship-from</Label>
              <Select value={filterShipFrom} onValueChange={setFilterShipFrom}>
                <SelectTrigger className="h-8 text-xs" data-testid="stock-transfer-filter-ship-from">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Sites</SelectItem>
                  {distinctShipFrom.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="w-[130px]">
              <Label className="text-[10px] font-bold text-[#667085] uppercase">Ship-to</Label>
              <Select value={filterShipTo} onValueChange={setFilterShipTo}>
                <SelectTrigger className="h-8 text-xs" data-testid="stock-transfer-filter-ship-to">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Sites</SelectItem>
                  {distinctShipTo.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="w-[170px]">
              <Label className="text-[10px] font-bold text-[#667085] uppercase">Item</Label>
              <Input value={filterItem} onChange={(e) => setFilterItem(e.target.value)} placeholder="Product ID or name" className="h-8 text-xs" data-testid="stock-transfer-filter-item" />
            </div>
            {hasActiveRecentFilters && (
              <Button variant="ghost" size="sm" onClick={clearRecentFilters} className="h-8 text-xs text-[#B42318]" data-testid="stock-transfer-filter-clear">
                Clear Filters
              </Button>
            )}
          </div>

          {loadingRecent ? (
            <p className="p-4 text-xs text-[#667085]">Loading...</p>
          ) : recentOrders.length === 0 ? (
            <p className="p-4 text-xs text-[#98A2B3]" data-testid="stock-transfer-recent-empty">No Stock Transfer Orders yet.</p>
          ) : filteredRecentOrders.length === 0 ? (
            <p className="p-4 text-xs text-[#98A2B3]" data-testid="stock-transfer-recent-filtered-empty">No orders match these filters.</p>
          ) : (
            <table className="w-full text-[12px] border-collapse min-w-[900px]" data-testid="stock-transfer-recent-table">
              <thead>
                <tr>
                  {["STO ID", "Created", "By", "Ship-from", "Ship-to", "Location", "Items", "Delivery Date", "SAP Order ID", "Status", "Goods Issue", "SAP User", "GST Push", "ERP Portal"].map((h) => (
                    <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-[11px] font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>

              <tbody>
                {filteredRecentOrders.map((o) => {
                  const badge = o.error_message
                    ? { label: "Issue Found", className: "bg-[#FEF3F2] text-[#B42318]" }
                    : o.status === "created_in_sap"
                    ? { label: "Created in SAP", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : { label: "Pending SAP", className: "bg-[#FEF0C7] text-[#93370D]" };
                  const giBadge = o.gi_status === "posted"
                    ? { label: "GI Posted", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : o.gi_status === "failed"
                    ? { label: "GI Failed", className: "bg-[#FEF3F2] text-[#B42318]" }
                    : o.gi_status === "not_found_timeout"
                    ? { label: "GI Pending (20min+)", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : o.gi_status === "insufficient_stock"
                    ? { label: "Insufficient Stock", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : o.gi_status === "awaiting_delivery"
                    ? { label: "Awaiting Delivery", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : null;
                  const gstBadge = o.gst_note_pushed
                    ? { label: "GST Recorded", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : null;
                  const erpBadge = o.erp_portal_status === "synced"
                    ? { label: "Synced", className: "bg-[#ECFDF3] text-[#027A48]" }
                    : o.erp_portal_status === "failed"
                    ? { label: "Sync Failed", className: "bg-[#FEF3F2] text-[#B42318]" }
                    : o.erp_portal_status === "retrying"
                    ? { label: "Retrying...", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : o.status === "created_in_sap"
                    ? { label: "Pending", className: "bg-[#FEF0C7] text-[#93370D]" }
                    : null;
                  return (
                    <tr
                      key={o.sto_id}
                      onClick={() => setSelectedOrder(o)}
                      className="cursor-pointer hover:bg-[#F9FAFB]"
                      data-testid={`stock-transfer-recent-row-${o.sto_id}`}
                    >
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium text-[#0E7C86] underline">{o.sto_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{new Date(o.created_at).toLocaleString("en-IN")}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.created_by}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.ship_from_site_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.ship_to_site_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.ship_to_location_name || o.ship_to_location_id}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.items?.length || 0}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">{o.requested_delivery_date}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono" data-testid={`stock-transfer-recent-sap-id-${o.sto_id}`}>
                        {formatSapId(o.sap_order_id) || "—"}
                        {o.gi_status === "posted" && o.outbound_delivery_ids?.length > 0 && (
                          <div className="mt-0.5" data-testid={`stock-transfer-recent-outbound-no-${o.sto_id}`}>
                            <span className="inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]">
                              Outbound: {o.outbound_delivery_ids.join(", ")}
                            </span>
                          </div>
                        )}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">
                        <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${badge.className}`}>{badge.label}</span>
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">
                        {giBadge ? <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${giBadge.className}`}>{giBadge.label}</span> : <span className="text-[#98A2B3]">—</span>}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5 font-mono" data-testid={`stock-transfer-recent-sap-user-${o.sto_id}`}>{o.gi_playwright_user || "—"}</td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5">
                        {gstBadge ? <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${gstBadge.className}`}>{gstBadge.label}</span> : <span className="text-[#98A2B3]">—</span>}
                      </td>
                      <td className="border border-[#D0D5DD] px-2 py-1.5" data-testid={`stock-transfer-recent-erp-status-${o.sto_id}`}>
                        {erpBadge ? <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-bold ${erpBadge.className}`}>{erpBadge.label}</span> : <span className="text-[#98A2B3]">—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </main>

      {/* 2x-safety confirmation before the real, irreversible live SAP
          write (user's explicit ask, Aug 2026) - clicking "Review &
          Create" above only opens this summary; nothing is submitted to
          SAP until "Confirm & Submit to SAP" is explicitly clicked here. */}
      <Dialog open={showConfirmDialog} onOpenChange={(open) => { if (!submitting) { setShowConfirmDialog(open); if (!open) { giPollStopRef.current = true; setSapSubmitPhase(null); setSapSubmitMessage(null); setStepStatuses({}); setCreatedOrderLive(null); } } }}>
        <DialogContent className="max-w-5xl max-h-[85vh] overflow-y-auto" data-testid="stock-transfer-confirm-dialog">
          <DialogHeader>
            <DialogTitle>Confirm Stock Transfer Order</DialogTitle>
            <DialogDescription>
              This will be written LIVE to SAP immediately - it cannot be undone from this app. Please review carefully.
            </DialogDescription>
          </DialogHeader>

          {sapSubmitPhase ? (
            <div className="space-y-3" data-testid="stock-transfer-sap-submit-status">
              <ol className="space-y-2.5">
                {STO_STEPS.map((step) => {
                  const state = step.key === "validate" ? (stepStatuses.validate || "pending") : (stepStatuses[step.key] || "pending");
                  return (
                    <li key={step.key} className="flex items-center gap-2.5 text-sm" data-testid={`stock-transfer-step-${step.key}`} data-step-state={state}>
                      {state === "done" && <CheckCircle size={18} weight="fill" className="text-[#12B76A] shrink-0" />}
                      {state === "active" && <CircleNotch size={18} className="text-[#7A5AF8] shrink-0 animate-spin" />}
                      {state === "failed" && <WarningCircle size={18} weight="fill" className="text-[#F04438] shrink-0" />}
                      {state === "pending" && <span className="w-[18px] h-[18px] rounded-full border-2 border-[#D0D5DD] shrink-0" />}
                      <span className={state === "pending" ? "text-[#98A2B3]" : state === "failed" ? "text-[#912018] font-medium" : "text-[#344054] font-medium"}>{step.label}</span>
                    </li>
                  );
                })}
              </ol>
              {(sapSubmitPhase === "failed" || (sapSubmitPhase === "done" && !createdOrderLive)) && (
                <div
                  className={`rounded-sm p-3 text-sm flex items-start gap-2 ${
                    sapSubmitPhase === "done" ? "bg-[#ECFDF3] border border-[#ABEFC6] text-[#027A48]"
                    : "bg-[#FEF3F2] border border-[#FDA29B] text-[#912018]"
                  }`}
                  data-testid="stock-transfer-sap-submit-result"
                >
                  {sapSubmitPhase === "done" ? <CheckCircle size={16} className="mt-0.5 shrink-0" /> : <WarningCircle size={16} className="mt-0.5 shrink-0" />}
                  <p>{sapSubmitMessage}</p>
                </div>
              )}
              {sapSubmitPhase === "done" && createdOrderLive && (
                <OrderDetailBody
                  order={createdOrderLive}
                  retryingStoId={retryingStoId} onRetryOrder={handleRetryOrder}
                  retryingErpStoId={retryingErpStoId} onRetryErpSync={handleRetryErpSync}
                  retryingGiStoId={retryingGiStoId} onRetryGoodsIssue={handleRetryGoodsIssue}
                  stoppingGiStoId={stoppingGiStoId} onForceStopGi={handleForceStopGi} isAdmin={isAdmin}
                  notifications={notifications} activateResults={activateResults} activatingId={activatingId}
                  confirmActivateFor={confirmActivateFor} setConfirmActivateFor={setConfirmActivateFor} onActivate={handleActivate}
                />
              )}
            </div>

          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
                <div><Label className="text-xs font-bold text-[#344054]">Ship-from Site</Label><p data-testid="stock-transfer-confirm-ship-from">{shipFromSiteId || "—"}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Ship-to Site</Label><p data-testid="stock-transfer-confirm-ship-to">{shipToSiteId}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Ship-to Location</Label><p>{shipToLocationOptions.find((w) => w.warehouse_id === shipToLocationId)?.warehouse_name || shipToLocationId}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Delivery Priority</Label><p>Immediate</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Requested Delivery Date</Label><p>{requestedDeliveryDate}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Transportation Mode</Label><p>{transportationMode}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Vehicle No.</Label><p>{vehicleNo}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Place Of Supply</Label><p>{placeOfSupply}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">G.R No.</Label><p>{grNo}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Date Of Supply</Label><p>{dateOfSupply}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Freight Forwarder</Label><p data-testid="stock-transfer-confirm-freight-forwarder">{freightForwarder}</p></div>
                <div><Label className="text-xs font-bold text-[#344054]">Remark</Label><p data-testid="stock-transfer-confirm-remark">{remark || "—"}</p></div>
              </div>
              <div className="border border-[#EAECF0] rounded-sm overflow-auto">
                <table className="w-full text-xs border-collapse" data-testid="stock-transfer-confirm-items-table">
                  <thead>
                    <tr>
                      {["Product", "Source Warehouse", "Requested Qty"].map((h) => (
                        <th key={h} className="bg-[#EAECF0] border border-[#D0D5DD] p-1.5 text-left text-[11px] font-bold text-[#344054] font-heading uppercase whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((i) => (
                      <tr key={i.key}>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 font-medium">{i.product_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5">{i.source_warehouse_id}</td>
                        <td className="border border-[#D0D5DD] px-2 py-1.5 text-right tabular-nums">{formatQty(Number(i.requested_qty))} {i.unit_of_measure}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex justify-end gap-2 pt-1">
                <Button type="button" variant="outline" onClick={() => setShowConfirmDialog(false)} data-testid="stock-transfer-confirm-cancel-button">Cancel, go back</Button>
                <Button type="button" onClick={confirmAndSubmit} disabled={submitting} data-testid="stock-transfer-confirm-submit-button">
                  {submitting ? "Submitting..." : "Confirm & Submit to SAP"}
                </Button>
              </div>
            </>
          )}

          {sapSubmitPhase && sapSubmitPhase !== "submitting" && (
            <div className="flex justify-end pt-1">
              <Button type="button" onClick={() => { giPollStopRef.current = true; setShowConfirmDialog(false); setSapSubmitPhase(null); setSapSubmitMessage(null); setStepStatuses({}); setCreatedOrderLive(null); }} data-testid="stock-transfer-confirm-close-button">Close</Button>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Detail modal (Aug 2026, user's explicit ask) - clicking any row
          above opens the full breakdown; any SAP-write failure's
          error_message is surfaced here verbatim, human-readable, never a
          raw stack trace. */}
      <Dialog open={!!selectedOrder} onOpenChange={(open) => !open && setSelectedOrder(null)}>
        <DialogContent className="max-w-5xl max-h-[85vh] overflow-y-auto" data-testid="stock-transfer-detail-modal">
          {selectedOrder && (
            <>
              <DialogHeader>
                <DialogTitle>{selectedOrder.sto_id}</DialogTitle>
                <DialogDescription>Stock Transfer Order details and live SAP status</DialogDescription>
              </DialogHeader>
              <OrderDetailBody
                order={selectedOrder}
                retryingStoId={retryingStoId} onRetryOrder={handleRetryOrder}
                retryingErpStoId={retryingErpStoId} onRetryErpSync={handleRetryErpSync}
                retryingGiStoId={retryingGiStoId} onRetryGoodsIssue={handleRetryGoodsIssue}
                stoppingGiStoId={stoppingGiStoId} onForceStopGi={handleForceStopGi} isAdmin={isAdmin}
                notifications={notifications} activateResults={activateResults} activatingId={activatingId}
                confirmActivateFor={confirmActivateFor} setConfirmActivateFor={setConfirmActivateFor} onActivate={handleActivate}
              />
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
