import { useEffect, useRef } from "react";
import JsBarcode from "jsbarcode";

// Aug 27 2026, user's explicit ask (Gate Pass): "Sale_Noc:Comp_code:Sale_no:C"
// rendered as a CODE128 barcode with the text printed underneath.
export function Barcode128({ value, testId }) {
  const ref = useRef(null);

  useEffect(() => {
    if (ref.current && value) {
      JsBarcode(ref.current, value, { format: "CODE128", displayValue: true, fontSize: 13, height: 45, margin: 6 });
    }
  }, [value]);

  if (!value) return null;
  return <svg ref={ref} data-testid={testId} />;
}
