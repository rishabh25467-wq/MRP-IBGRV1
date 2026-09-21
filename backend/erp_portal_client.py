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


# Official CBIC GST State Codes (Aug 27 2026, fix for Delivery Note print
# bug) - the ERP's own `comp` table has StateCode/Cstate blank or wrong
# for some sites (e.g. P3: StateCode NULL, Cstate literally "India" - not
# a real state name, unusable on a GST document), so this is used as a
# fallback derived from the GSTIN's own reliable first 2 digits.
GST_STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra", "28": "Andhra Pradesh",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman & Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh", "97": "Other Territory",
}


class ERPPortalClient:
    def __init__(self, primary_host: str, fallback_host: str, port: int, database: str,
                 username: str, password: str, timeout: int = 5):
        self.hosts = [h for h in (primary_host, fallback_host) if h]
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.timeout = timeout

    def check_connection(self) -> str:
        """Sep 9 2026, user's explicit ask: a lightweight "is the ERP
        server reachable right now" check (mirrors sap_soap_client's
        check_connection - NOT a per-STO/per-transaction sync status).
        Returns which host actually answered ("primary" or "fallback").
        Raises ERPPortalError (via _connect's own failover logic) if
        neither configured host is reachable."""
        for i, host in enumerate(self.hosts):
            try:
                conn = pytds.connect(
                    server=host, port=self.port, database=self.database,
                    user=self.username, password=self.password,
                    timeout=self.timeout, login_timeout=self.timeout, autocommit=False,
                )
                conn.close()
                return "primary" if i == 0 else "fallback"
            except Exception as e:
                logger.warning(f"ERP Portal connection check: could not reach {host}:{self.port}: {e}")
        raise ERPPortalError(f"ERP Portal unreachable on all configured host(s) ({', '.join(self.hosts)})")

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

    @staticmethod
    def _row_to_company_dict(row) -> dict:
        gstin = (row[8] or "").strip() or None
        state = (row[6] or "").strip() or None
        state_code = (row[7] or "").strip() or None
        # Fallback (Aug 27 2026 fix): the raw `comp` columns are
        # blank or wrong for some sites (e.g. P3: StateCode NULL,
        # Cstate literally "India" - not a real state name) - the
        # GSTIN's own first 2 digits are the official CBIC state
        # code and far more reliable, so prefer deriving from it
        # whenever the raw column is missing or bogus.
        gstin_prefix = gstin[:2] if gstin else None
        gstin_state = GST_STATE_CODES.get(gstin_prefix) if gstin_prefix else None
        if not state_code and gstin_prefix:
            state_code = gstin_prefix
        if (not state or state.strip().lower() == "india") and gstin_state:
            state = gstin_state
        return {
            "company_name": (row[1] or "").strip(),
            "address_line1": (row[2] or "").strip(),
            "address_line2": (row[3] or "").strip(),
            "city": (row[4] or "").strip(),
            "pin": (row[5] or "").strip() or None,
            "state": state,
            "state_code": state_code,
            "gstin": gstin,
            "pan": (row[9] or "").strip() or None,
            "session": (row[10] or "").strip() or None,
            # Aug 27 2026, user's explicit ask: the ERP's OWN internal
            # plant code for this site (confirmed live against real
            # DeliveryChallan history: the ship-to site's own `pcode` is
            # what actually belongs in that table's Pcode column, NOT
            # our app's raw Site ID) - None if never registered in `comp`
            # for this site (e.g. P6 today - caller falls back to the
            # raw site code in that case).
            "pcode": (row[11] or "").strip() or None,
        }

    def get_company_info(self, codes: list) -> dict:
        """Reads dbo.comp (Aug 2026, user's explicit ask - real Company
        Name/Address/GSTIN/PAN/current fiscal-year `session` code per
        site, for the Delivery Note/Gate Pass print views), keyed by
        `Ccode` - the SAME code as our own Site ID (e.g. "P2"). A
        read-only SELECT, not a stored-procedure write, so the user's
        "never raw INSERT, always use the portal's own procs" rule
        (which is about WRITES) doesn't apply here.

        Aug 27 2026 (this session): callers should prefer
        company_cache_service.get_cached_company_info instead of calling
        this directly on every print - this table "does not change"
        (user's own words) so it belongs in Mongo, refreshed
        periodically, not queried live on every page load."""
        if not codes:
            return {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            placeholders = ",".join("%s" for _ in codes)
            cur.execute(
                f"SELECT Ccode, C_name, Cadd1, Cadd2, Ccity, Cpin, Cstate, StateCode, GSTIN, pan_no, session, pcode "
                f"FROM comp WHERE Ccode IN ({placeholders})",
                tuple(codes),
            )
            return {(row[0] or "").strip(): self._row_to_company_dict(row) for row in cur.fetchall()}
        except Exception as e:
            raise ERPPortalError(f"ERP Portal read (comp) failed: {e}")
        finally:
            conn.close()

    def get_all_company_info(self) -> dict:
        """Every site/company row in dbo.comp, no WHERE clause - backs
        company_cache_service.refresh_company_cache's periodic full
        refresh (Aug 27 2026, user's explicit ask)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT Ccode, C_name, Cadd1, Cadd2, Ccity, Cpin, Cstate, StateCode, GSTIN, pan_no, session, pcode FROM comp")
            return {(row[0] or "").strip(): self._row_to_company_dict(row) for row in cur.fetchall()}
        except Exception as e:
            raise ERPPortalError(f"ERP Portal read (comp, full refresh) failed: {e}")
        finally:
            conn.close()

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
            # Aug 27 2026, user's explicit ask: InvStk_status is a real
            # column on DeliveryChallan, but Pro_DeliveryChallan_Insert
            # has no parameter for it at all (confirmed against the
            # proc's own signature) - it's left NULL by that proc. A
            # brand new row is marked "Open" (their own convention -
            # existing ERP values seen elsewhere are "GP Print"/"Close"/
            # "Transit", set later by other screens in their workflow).
            cur.execute(
                "UPDATE DeliveryChallan SET InvStk_status = 'Open' WHERE Sale_No = %s AND CompCode = %s",
                (sale_no, header["comp_code"]),
            )
            conn.commit()
            return {"sale_no": sale_no, "sale_noc": sale_noc}
        except ERPPortalError:
            raise
        except Exception as e:
            raise ERPPortalError(f"ERP Portal write failed: {e}")
        finally:
            conn.close()
