# Bug Report: Open-PO Demand feed shows closed/cancelled POs as still "open"

**Endpoint:** `GET /api/integration/open-po-demand`
**Reported by:** SAP BOM Explorer / Production Plan integration team
**Date:** Feb 2026

## Summary
Several PO lines that OMS itself shows as **CLOSED** are still being returned by this
feed with `qty_open > 0` (i.e. treated as open demand). Confirmed on 3 lines directly
(screenshot from OMS UI showing "Closed" status provided separately), all sharing the
same pattern: `qty_shipped = 0` and `qty_open = qty_ordered` - meaning nothing ever
shipped against these lines, yet they were closed in OMS through some other mechanism
(manual close / cancellation / write-off) that this feed doesn't appear to check.

## Confirmed bad rows (qty_shipped=0, closed in OMS per your UI, but feed says open)

| Customer | Customer PO | Internal PO # (`internal_pono`) | Item Code | Qty Ordered | Qty Shipped | Qty Open (per feed) |
|---|---|---|---|---|---|---|
| WALMART INC | 9528550591 | **9003458** | 100010102-A | 3,024 | 0 | 3,024 |
| WALMART INC | 1529115718 | **9004384** | 100010103-A | 6,000 | 0 | 6,000 |
| WALMART INC | 0884026160 | **9005961** | 100139235 | 208 | 0 | 208 |

## Root cause hypothesis
`qty_open` appears to be computed purely as `qty_ordered - qty_shipped`, without also
checking whether the PO/line has been separately marked Closed/Cancelled in OMS. This
would explain why fully-unshipped lines that were closed through a different workflow
(rather than by shipping) still pass the `qty_open > 0` filter and get included in this
feed.

## Additional rows matching the same suspicious pattern (for your team to spot-check)
Not all confirmed bad yet, but same shape (qty_shipped=0, target_ship_date far in the
past relative to today) - worth checking whether these are also incorrectly-open in
OMS:

| Target Ship Date | Customer | Customer PO | Internal PO # | Item Code | Qty Open |
|---|---|---|---|---|---|
| 2023-08-22 | WALMART INC | 1529115718 | 9004384 | 100010103-A | 6,000 |
| 2025-01-21 | WALMART INC | 0884026160 | 9005961 | 100139235 | 208 |
| 2025-06-27 | MACLEAN POWER -YORK L.L.C. | OP54466 Rev 1 | 9000763 | P27828 | 10 |
| 2025-07-14 | MACLEAN POWER SYSTEMS | OP83920 | 9000476 | F102199 | 210 |
| 2025-07-21 | MACLEAN POWER SYSTEMS | OP84461 REV 1 | 9000438 | F615047 | 240 |
| 2025-08-12 | WALMART INC | 0930144492 | 9003984 | 100010102-A | 3,840 |
| 2025-08-29 | MACLEAN POWER SYSTEMS | OP121691 | 9000477 / 9000439 | F102199 / F615047 | 210 / 288 |
| 2025-09-08 | MACLEAN POWER SYSTEMS | OP83283 | 9000475 | F102199 | 210 |
| 2025-09-13 | MACLEAN POWER SYSTEMS | OP118826 | 9000462 | F614794 | 1,980 |
| 2025-09-26 | VALMONT SITE PRO 1 | 0101093 | 9002406 / 9002405 | 248-24-DC / 248-12-DC | 150 / 600 |
| 2025-09-26 | MACLEAN POWER SYSTEMS | OP121692 | 9000478 / 9000440 | F102199 / F615047 | 210 / 288 |

(24 total rows currently in the feed have `qty_shipped = 0` with a `target_ship_date`
already more than a month in the past as of today - the table above is the first 15,
sorted oldest first. Full list available on request.)

## Requested fix
Please have the feed's `qty_open` calculation also exclude lines where the PO/line
status in OMS is Closed/Cancelled (not just "fully shipped"), so consuming systems
don't plan procurement against demand that no longer actually exists.
