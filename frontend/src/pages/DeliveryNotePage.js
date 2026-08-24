import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import axios from "axios";
import { Button } from "@/components/ui/button";
import { Printer, CircleNotch } from "@phosphor-icons/react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

// Static placeholder legal/company info (user's explicit instruction, Aug
// 27 2026 - "use placeholder or copy from pdf") - copied from SAP's own
// printed Delivery Note (P3D1-2409.pdf) as the reference target. No Bank
// Details section per user's explicit ask ("no need. delete.").
const COMPANY = {
  name: "RADISH TECHNOLOGIES",
  address: "C-268 & 269, SEC-2, TALA NAGARI",
  gstin: "AAKFR1956L",
  pan: "AAKFR1956L",
  state: "Uttar Pradesh",
  state_code: "09",
};

const SITE_NAMES = {
  P1: "RAY INTERNATIONAL-P1", P8: "RAY INTERNATIONAL-P8",
};
const siteName = (siteId) => SITE_NAMES[siteId] || `RADISH TECHNOLOGIES-${siteId}`;

const ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
  "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"];
const TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"];

function twoDigitWords(n) {
  if (n < 20) return ONES[n];
  return TENS[Math.floor(n / 10)] + (n % 10 ? " " + ONES[n % 10] : "");
}

function threeDigitWords(n) {
  if (n >= 100) return ONES[Math.floor(n / 100)] + " Hundred" + (n % 100 ? " " + twoDigitWords(n % 100) : "");
  return twoDigitWords(n);
}

// Indian numbering (Lakh/Crore) - standard for GST documents.
function amountInWords(amount) {
  const rupees = Math.floor(amount);
  const paise = Math.round((amount - rupees) * 100);
  if (rupees === 0 && paise === 0) return "Zero Rupees Only";
  let n = rupees;
  const parts = [];
  const crore = Math.floor(n / 10000000); n %= 10000000;
  const lakh = Math.floor(n / 100000); n %= 100000;
  const thousand = Math.floor(n / 1000); n %= 1000;
  const hundred = n;
  if (crore) parts.push(threeDigitWords(crore) + " Crore");
  if (lakh) parts.push(threeDigitWords(lakh) + " Lakh");
  if (thousand) parts.push(threeDigitWords(thousand) + " Thousand");
  if (hundred) parts.push(threeDigitWords(hundred));
  let words = (parts.join(" ") || "Zero") + " Indian Rupees";
  if (paise) words += " and " + twoDigitWords(paise) + " Paise";
  return words + " Only";
}

function formatDateDMY(isoDate) {
  if (!isoDate) return null;
  const [y, m, d] = isoDate.split("-");
  return d && m && y ? `${d}-${m}-${y}` : isoDate;
}

export default function DeliveryNotePage() {
  const { stoId } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    axios.get(`${API}/stock-transfer/${stoId}/delivery-note`)
      .then(({ data }) => setData(data))
      .catch((e) => setError(e?.response?.data?.detail || "Could not load this delivery note."));
  }, [stoId]);

  if (error) return <div className="p-10 text-center text-[#912018]" data-testid="delivery-note-error">{error}</div>;
  if (!data) return <div className="p-10 flex justify-center"><CircleNotch size={24} className="animate-spin text-[#667085]" /></div>;

  return (
    <div className="min-h-screen bg-[#F2F4F7] py-6 print:bg-white print:py-0" data-testid="delivery-note-page">
      <div className="max-w-[820px] mx-auto mb-4 flex justify-end print:hidden">
        <Button onClick={() => window.print()} data-testid="delivery-note-print-btn" className="bg-[#175CD3] hover:bg-[#164FB0]">
          <Printer size={16} className="mr-2" /> Print / Save as PDF
        </Button>
      </div>

      <div className="max-w-[820px] mx-auto bg-white border border-[#D0D5DD] shadow-sm p-8 text-[13px] text-[#101828] print:border-0 print:shadow-none print:p-0" style={{ fontFamily: "'DM Sans', sans-serif" }} data-testid="delivery-note-document">
        <div className="flex justify-between items-start border-b-2 border-[#101828] pb-3">
          <div>
            <h1 className="text-xl font-bold tracking-wide">{COMPANY.name}</h1>
            <p className="text-[#475467]">{siteName(data.ship_from_site_id)}</p>
            <p className="text-[#475467]">{COMPANY.address}</p>
            <p className="text-[#475467]">GSTIN: {COMPANY.gstin} &nbsp; PAN: {COMPANY.pan}</p>
            <p className="text-[#475467]">State: {COMPANY.state} &nbsp; State Code: {COMPANY.state_code}</p>
          </div>
          <div className="text-right">
            <h2 className="text-lg font-bold uppercase">Delivery Challan</h2>
            <p className="text-[#667085] text-xs">(Stock Transfer / Bill of Supply)</p>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-6 mt-4">
          <div>
            <p><span className="font-bold">Serial Number:</span> {data.erp_sale_no || "—"}{data.erp_sale_noc ? ` / ${data.erp_sale_noc}` : ""}</p>
            <p><span className="font-bold">Date of Issue:</span> {formatDateDMY(data.date_of_supply) || "—"}</p>
          </div>
          <div className="border border-[#D0D5DD] rounded-sm p-2">
            <p className="font-bold uppercase text-xs mb-1 text-[#475467]">Transport</p>
            <p>Vehicle No: {data.vehicle_no || "—"}</p>
            <p>G.R. No: {data.gr_no || "—"}</p>
            <p>Mode: {data.transportation_mode || "—"}</p>
            <p>Place of Supply: {data.place_of_supply || "—"}</p>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-6 mt-4">
          <div className="border border-[#D0D5DD] rounded-sm p-2">
            <p className="font-bold uppercase text-xs mb-1 text-[#475467]">Details of Receiver | Billed to</p>
            <p className="font-bold">{siteName(data.ship_to_site_id)}</p>
          </div>
          <div className="border border-[#D0D5DD] rounded-sm p-2">
            <p className="font-bold uppercase text-xs mb-1 text-[#475467]">Details of Consignee | Shipped to</p>
            <p className="font-bold">{siteName(data.ship_to_site_id)}</p>
          </div>
        </div>

        <table className="w-full mt-4 border-collapse text-xs" data-testid="delivery-note-items-table">
          <thead>
            <tr>
              {["Sr.", "Part Code", "Description", "HSN", "Qty", "Unit", "Rate", "Amount"].map((h) => (
                <th key={h} className="border border-[#101828] bg-[#EAECF0] p-1.5 text-left font-bold">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.items.map((it, idx) => (
              <tr key={it.product_id}>
                <td className="border border-[#D0D5DD] p-1.5">{idx + 1}</td>
                <td className="border border-[#D0D5DD] p-1.5 font-medium">{it.product_id}</td>
                <td className="border border-[#D0D5DD] p-1.5">{it.description || "—"}</td>
                <td className="border border-[#D0D5DD] p-1.5" data-testid={`delivery-note-hsn-${it.product_id}`}>{it.hsn_code || "—"}</td>
                <td className="border border-[#D0D5DD] p-1.5 text-right">{it.qty}</td>
                <td className="border border-[#D0D5DD] p-1.5">{it.unit}</td>
                <td className="border border-[#D0D5DD] p-1.5 text-right" data-testid={`delivery-note-rate-${it.product_id}`}>₹{it.rate.toFixed(2)}</td>
                <td className="border border-[#D0D5DD] p-1.5 text-right font-medium">₹{it.amount.toFixed(2)}</td>
              </tr>
            ))}
            <tr>
              <td colSpan={7} className="border border-[#D0D5DD] p-1.5 text-right font-bold">Total (INR)</td>
              <td className="border border-[#D0D5DD] p-1.5 text-right font-bold" data-testid="delivery-note-total">₹{data.total_amount.toFixed(2)}</td>
            </tr>
          </tbody>
        </table>

        <p className="mt-3"><span className="font-bold">Total Amount (in words):</span> {amountInWords(data.total_amount)}</p>

        <div className="mt-5">
          <p className="font-bold uppercase text-xs mb-1 text-[#475467]">Terms & Condition</p>
          <p>1) This is a Stock Transfer document issued for e-way bill / GST purposes, not a Tax Invoice.</p>
          <p>2) Goods must be packed and inspected in good condition upon receipt.</p>
          <p>3) All disputes are subject to Aligarh Jurisdiction only.</p>
        </div>

        <div className="flex justify-between items-end mt-10 pt-6">
          <p className="text-xs text-[#667085]">Certified that the particulars given above are true and correct.</p>
          <div className="text-center">
            <p className="font-bold">For {COMPANY.name}</p>
            <p className="mt-8 text-xs text-[#667085]">(Authorized Signatory)</p>
          </div>
        </div>
      </div>
    </div>
  );
}
