"""Delivery Challan Excel export (Sep 3 2026, user's explicit ask: "Downloaded
excel is not printable for A4"). Moved from client-side SheetJS (community
edition xlsx package, confirmed live it silently drops `!pageSetup`/
`!fitToPage` on write - Pro-only feature) to server-side openpyxl, which
fully supports page size/fit-to-page/margins/print area in the free
library - same content/field mapping as the browser-print view and the
old client-side export (see stock_transfer_service.get_delivery_note_data),
just laid out so Excel's own Print Preview already shows a single, clean
A4 portrait page with no manual setup needed.
"""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.page import PageMargins
from openpyxl.utils import get_column_letter

ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
        "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digit_words(n):
    if n < 20:
        return ONES[n]
    return TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else "")


def _three_digit_words(n):
    if n >= 100:
        rest = n % 100
        return ONES[n // 100] + " Hundred" + (" " + _two_digit_words(rest) if rest else "")
    return _two_digit_words(n)


def amount_in_words(amount) -> str:
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    if rupees == 0 and paise == 0:
        return "Zero Rupees Only"
    n = rupees
    parts = []
    crore, n = divmod(n, 10000000)
    lakh, n = divmod(n, 100000)
    thousand, hundred = divmod(n, 1000)
    if crore:
        parts.append(_three_digit_words(crore) + " Crore")
    if lakh:
        parts.append(_three_digit_words(lakh) + " Lakh")
    if thousand:
        parts.append(_three_digit_words(thousand) + " Thousand")
    if hundred:
        parts.append(_three_digit_words(hundred))
    words = (" ".join(parts) or "Zero") + " Indian Rupees"
    if paise:
        words += " and " + _two_digit_words(paise) + " Paise"
    return words + " Only"


def _format_date_dmy(iso_date):
    if not iso_date:
        return None
    parts = iso_date.split("-")
    if len(parts) == 3:
        y, m, d = parts
        return f"{d}-{m}-{y}"
    return iso_date


def _company_lines(company, fallback_site_id):
    if not company:
        return [[fallback_site_id]]
    address = ", ".join(v for v in [company.get("address_line1"), company.get("address_line2")] if v)
    return [
        [company.get("company_name") or ""],
        [address],
        [f"GSTIN: {company.get('gstin') or '—'}    PAN: {company.get('pan') or '—'}"],
        [f"State: {company.get('state') or '—'}    State Code: {company.get('state_code') or '—'}"],
    ]


def build_delivery_note_excel(data: dict) -> BytesIO:
    company_name = (data.get("ship_from_company") or {}).get("company_name") or f"RADISH TECHNOLOGIES-{data['ship_from_site_id']}"

    rows = [
        [company_name],
        *_company_lines(data.get("ship_from_company"), data["ship_from_site_id"]),
        ["DELIVERY CHALLAN (Stock Transfer / Bill of Supply)"],
        [],
        [f"Serial Number: {data.get('serial_number') or '—'}"],
        [f"Date of Issue: {_format_date_dmy(data.get('date_of_supply')) or '—'}"],
        [],
        ["Transport Details"],
        [f"Vehicle No: {data.get('vehicle_no') or '—'}"],
        [f"G.R. No: {data.get('gr_no') or '—'}"],
        [f"Mode: {data.get('transportation_mode') or '—'}"],
        [f"Place of Supply: {data.get('place_of_supply') or '—'}"],
        [f"Freight Forwarder: {data.get('freight_forwarder') or 'Self'}"],
        [f"Remark: {data.get('remark') or '—'}"],
        [],
        ["Details of Receiver | Billed to"],
        *_company_lines(data.get("ship_to_company"), data["ship_to_site_id"]),
        [],
        ["Details of Consignee | Shipped to"],
        *_company_lines(data.get("ship_to_company"), data["ship_to_site_id"]),
        [],
    ]
    header_row_idx = len(rows) + 1
    rows.append(["Sr.", "Part Code", "Description", "HSN", "Qty", "Unit", "Rate", "Amount"])
    for idx, item in enumerate(data.get("items") or [], start=1):
        rows.append([idx, item.get("product_id"), item.get("description") or "—", item.get("hsn_code") or "—",
                     item.get("qty"), item.get("unit"), item.get("rate"), item.get("amount")])
    last_item_row_idx = len(rows)
    rows.append(["", "", "", "", "", "", "Total (INR)", data.get("total_amount")])
    rows.append([])
    rows.append([f"Total Amount (in words): {amount_in_words(data.get('total_amount') or 0)}"])
    rows.append([])
    rows.append(["Terms & Condition"])
    rows.append(["1) This is a Stock Transfer document issued for e-way bill / GST purposes, not a Tax Invoice."])
    rows.append(["2) Goods must be packed and inspected in good condition upon receipt."])
    rows.append(["3) All disputes are subject to Aligarh Jurisdiction only."])

    wb = Workbook()
    ws = wb.active
    ws.title = "Delivery Challan"

    for row in rows:
        ws.append(row if row else [None])

    bold = Font(bold=True)
    ws["A1"].font = Font(bold=True, size=13)
    for label_row in (6, header_row_idx):
        ws.cell(row=label_row, column=1).font = bold

    thin = Side(style="thin", color="AAAAAA")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r in range(header_row_idx, last_item_row_idx + 2):
        for c in range(1, 9):
            cell = ws.cell(row=r, column=c)
            cell.border = box
            if r == header_row_idx or r == last_item_row_idx + 1:
                cell.font = bold

    col_widths = [6, 16, 30, 10, 8, 8, 12, 14]
    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    max_col = 8
    max_row = ws.max_row
    ws.print_area = f"A1:{get_column_letter(max_col)}{max_row}"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToPage = True
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5, header=0.2, footer=0.2)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
