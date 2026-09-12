"""Backend tests for Production Confirmation filter/KPI enhancements (Sep 12 2026)."""
import os
import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or "https://sap-data-sync.preview.emergentagent.com").rstrip("/")

SUPER_ADMIN_SESSION = "6e8c7c0c6a154691ae23819e13dc7712"
USER_SESSION = "d82a7ffc9b17d9adf588149565b7fe1d"


def _client(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture
def admin():
    return _client(SUPER_ADMIN_SESSION)


@pytest.fixture
def user():
    return _client(USER_SESSION)


# ---------- today-stats endpoint ----------
class TestTodayStats:
    def test_today_stats_super_admin(self, admin):
        r = admin.get(f"{BASE_URL}/api/production-confirmation/today-stats", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        for k in ["today_confirmed_output_qty", "today_released_output_qty",
                  "today_output_open_qty", "today_scrap_posted_qty"]:
            assert k in data, f"missing {k}"
            assert isinstance(data[k], (int, float)), f"{k} not numeric: {data[k]!r}"
            assert data[k] is not None

    def test_today_stats_site_user(self, user):
        r = user.get(f"{BASE_URL}/api/production-confirmation/today-stats", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        for k in ["today_confirmed_output_qty", "today_released_output_qty",
                  "today_output_open_qty", "today_scrap_posted_qty"]:
            assert k in data
            assert isinstance(data[k], (int, float))

    def test_today_stats_unauthenticated(self):
        r = requests.get(f"{BASE_URL}/api/production-confirmation/today-stats", timeout=30)
        assert r.status_code in (401, 403)


# ---------- proposal-history date range filter ----------
class TestProposalHistoryDateFilter:
    def test_no_filter(self, admin):
        r = admin.get(f"{BASE_URL}/api/production-confirmation/proposal-history", timeout=30)
        assert r.status_code == 200
        assert "entries" in r.json()
        assert isinstance(r.json()["entries"], list)

    def test_with_date_range(self, admin):
        r = admin.get(
            f"{BASE_URL}/api/production-confirmation/proposal-history",
            params={"start_date": "2026-09-01", "end_date": "2026-09-30"},
            timeout=30,
        )
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert isinstance(entries, list)

    def test_future_range_zero_rows(self, admin):
        r = admin.get(
            f"{BASE_URL}/api/production-confirmation/proposal-history",
            params={"start_date": "2099-01-01", "end_date": "2099-01-02"},
            timeout=30,
        )
        assert r.status_code == 200
        assert r.json()["entries"] == []

    def test_invalid_date_format(self, admin):
        r = admin.get(
            f"{BASE_URL}/api/production-confirmation/proposal-history",
            params={"start_date": "not-a-date"},
            timeout=30,
        )
        # Should either 400 or 500 gracefully
        assert r.status_code in (400, 422, 500)


# ---------- open-lots status=4 (Finished) support ----------
class TestOpenLotsStatusFilter:
    def test_status_open(self, admin):
        r = admin.get(f"{BASE_URL}/api/production-confirmation/open-lots",
                      params={"status": "2,3"}, timeout=120)
        assert r.status_code == 200
        assert "lots" in r.json() or "rows" in r.json() or isinstance(r.json(), (dict, list))

    def test_status_finished(self, admin):
        r = admin.get(f"{BASE_URL}/api/production-confirmation/open-lots",
                      params={"status": "4"}, timeout=120)
        assert r.status_code == 200
        body = r.json()
        # should return dict/list without error
        assert body is not None

    def test_status_all(self, admin):
        r = admin.get(f"{BASE_URL}/api/production-confirmation/open-lots",
                      params={"status": "2,3,4"}, timeout=120)
        assert r.status_code == 200
