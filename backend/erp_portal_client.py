"""Direct MS SQL Server client for the Radish "Delivery Challan" legacy
ERP/portal (Aug 27 2026) - user's explicit ask: every Stock Transfer
Order created in SAP by this app should also be written here, via the
portal's OWN stored procedures (user's explicit instruction - never raw
INSERTs), so both systems stay in sync.

Primary/fallback failover (user's explicit ask) - 2 separate on-prem SQL
Server hosts, same DB/login on both; tries primary first, falls back to
the secondary host on any connection failure.

Table/proc shapes (confirmed live against this exact tenant, Aug 27 2026,
via sys.parameters + the user's own CREATE TABLE scripts):
  dbo.DeliveryChallan (header) <- Pro_DeliveryChallan_Insert /
    Pro_DeliveryChallan_Update. @Sale_No and @Sale_Noc are OUTPUT params
    on Insert (the portal auto-generates its own document numbers) - both
    values must be passed through to every line item insert.
  dbo.DeliveryChallani (line items) <- Pro_DeliveryChallani_Insert, one
    call per line, `@Sno` = 1-based line sequence number within this
    challan.
"""
import logging

import pytds
from pytds import output

logger = logging.getLogger(__name__)


class ERPPortalError(Exception):
    pass


class ERPPortalClient:
    def __init__(self, primary_host: str, fallback_host: str, port: int, database: str,
                 username: str, password: str, timeout: int = 20):
        self.hosts = [h for h in (primary_host, fallback_host) if h]
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.timeout = timeout

    def _connect(self):
        last_err = None
        for i, host in enumerate(self.hosts):
            try:
                return pytds.connect(
                    server=host, port=self.port, database=self.database,
                    user=self.username, password=self.password,
                    timeout=self.timeout, login_timeout=self.timeout, autocommit=False,
                )
            except Exception as e:
                logger.warning(f"ERP Portal: could not reach {host}:{self.port}"
                                f"{' - trying fallback host' if i < len(self.hosts) - 1 else ' - no more hosts to try'}: {e}")
                last_err = e
        raise ERPPortalError(f"ERP Portal unreachable on all configured host(s) ({', '.join(self.hosts)}): {last_err}")

    def create_delivery_challan(self, header: dict, items: list) -> dict:
        """header keys: comp_code, elec_ref_no, sale_date (datetime),
        pcode, padd_code1, padd_code2, trans, veh_no, gr_no, gr_date
        ('YYYY-MM-DD' string), marks, amount, tdis_amt, ttaxable_amt,
        term1, term2, term3, emp_no.
        items: list of dicts with product_id, description, hsn_no, qty,
        unit, rate, amt, dis_amt, taxable_amt, remark.
        Returns {"sale_no": int, "sale_noc": int}."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            result = cur.callproc("Pro_DeliveryChallan_Insert", (
                output(value=0), header["comp_code"], output(value=0),
                header.get("elec_ref_no") or "", header["sale_date"], header["pcode"],
                header.get("padd_code1") or "", header.get("padd_code2") or "",
                header.get("trans") or "", header.get("veh_no") or "",
                header.get("gr_no") or "", header.get("gr_date") or "",
                header.get("marks") or "", header["amount"], header.get("tdis_amt") or 0,
                header["ttaxable_amt"], header.get("term1") or "", header.get("term2") or "",
                header.get("term3") or "", header.get("emp_no") or "",
            ))
            # pytds resolves OUTPUT params directly in-place on callproc's
            # own return value (confirmed live, Aug 27 2026) when the proc
            # returns no result set - cursor.get_proc_outputs() is a
            # DIFFERENT mechanism only for procs that ALSO return a result
            # set (raises IndexError here otherwise - this proc has none).
            if not result or not result[0]:
                raise ERPPortalError(f"Pro_DeliveryChallan_Insert did not return a Sale_No output value (got {result})")
            sale_no, sale_noc = int(result[0]), int(result[2])

            for idx, item in enumerate(items, start=1):
                cur.callproc("Pro_DeliveryChallani_Insert", (
                    sale_no, sale_noc, idx, header["comp_code"], header["sale_date"],
                    header["pcode"], item["product_id"], (item.get("description") or "")[:60],
                    item.get("hsn_no") or "", item["qty"], item["unit"], item["rate"],
                    item["amt"], item.get("dis_amt") or 0, item.get("taxable_amt") if item.get("taxable_amt") is not None else item["amt"],
                    (item.get("remark") or "")[:200],
                ))
            conn.commit()
            return {"sale_no": sale_no, "sale_noc": sale_noc}
        except ERPPortalError:
            raise
        except Exception as e:
            raise ERPPortalError(f"ERP Portal write failed: {e}")
        finally:
            conn.close()
