import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import axios from "axios";
import { Button } from "@/components/ui/button";
import { Printer, CircleNotch } from "@phosphor-icons/react";
import { Barcode128 } from "@/components/Barcode128";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

function formatDateDMY(isoDate) {
  if (!isoDate) return null;
  const [y, m, d] = isoDate.split("-");
  return d && m && y ? `${d}/${m}/${y}` : isoDate;
}

// Aug 27 2026, user's explicit ask (screenshot reference provided): a
// standalone Gate Pass slip for security at the Ship-from site's gate -
// reuses the same /delivery-note data (it already has everything: ERP
// Sale_No/Sale_Noc, vehicle, freight forwarder, Ship-to company).
export default function GatePassPage() {
  const { stoId } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    axios.get(`${API}/stock-transfer/${stoId}/delivery-note`)
      .then(({ data }) => setData(data))
      .catch((e) => setError(e?.response?.data?.detail || "Could not load this gate pass."));
  }, [stoId]);

  if (error) return <div className="p-10 text-center text-[#912018]" data-testid="gate-pass-error">{error}</div>;
  if (!data) return <div className="p-10 flex justify-center"><CircleNotch size={24} className="animate-spin text-[#667085]" /></div>;

  // User's explicit spec: "Sale_Noc:Comp_code:Sale_no:C" (C is fixed).
  const barcodeValue = data.erp_sale_noc != null
    ? `${data.erp_sale_noc}:${data.ship_from_site_id}:${data.erp_sale_no}:C`
    : null;

  return (
    <div className="min-h-screen bg-[#F2F4F7] py-6 print:bg-white print:py-0" data-testid="gate-pass-page">
      <div className="max-w-[520px] mx-auto mb-4 flex justify-end print:hidden">
        <Button onClick={() => window.print()} data-testid="gate-pass-print-btn" className="bg-[#175CD3] hover:bg-[#164FB0]">
          <Printer size={16} className="mr-2" /> Print / Save as PDF
        </Button>
      </div>

      <div className="max-w-[520px] mx-auto bg-white border border-[#D0D5DD] shadow-sm p-6 text-[13px] text-[#101828] print:border-0 print:shadow-none print:p-0" style={{ fontFamily: "'DM Sans', sans-serif" }} data-testid="gate-pass-document">
        <div className="text-center border-b-2 border-[#101828] pb-2">
          <h1 className="text-lg font-bold tracking-wide">{data.ship_from_company?.company_name || data.ship_from_site_id}</h1>
          <h2 className="text-sm font-bold uppercase mt-1">Gate Pass</h2>
        </div>

        <div className="grid grid-cols-2 gap-3 mt-4">
          <div><span className="font-bold">Serial No:</span> <span data-testid="gate-pass-serial-no">{data.erp_sale_noc || "—"}</span></div>
          <div><span className="font-bold">Date:</span> <span data-testid="gate-pass-date">{formatDateDMY(data.date_of_supply) || "—"}</span></div>
          <div className="col-span-2"><span className="font-bold">Gate Pass Type:</span> Delivery Challan</div>
          <div className="col-span-2"><span className="font-bold">Customer:</span> <span data-testid="gate-pass-customer">{data.ship_to_company?.company_name || data.ship_to_site_id}</span></div>
          <div><span className="font-bold">Vehicle No:</span> <span data-testid="gate-pass-vehicle-no">{data.vehicle_no || "—"}</span></div>
          <div><span className="font-bold">Transport No:</span> <span data-testid="gate-pass-transport-no">{data.freight_forwarder || "Self"}</span></div>
        </div>

        <div className="flex flex-col items-center mt-8 pt-4 border-t border-[#EAECF0]">
          <Barcode128 value={barcodeValue} testId="gate-pass-barcode" />
        </div>

        <div className="flex justify-between items-end mt-10 pt-6">
          <p className="text-xs text-[#667085]">Security Signature</p>
          <p className="text-xs text-[#667085]">Driver Signature</p>
        </div>
      </div>
    </div>
  );
}
