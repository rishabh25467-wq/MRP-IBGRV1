"""Probe: measure /api/admin/grn/sites availability + latency over 2 minutes
to characterise the intermittent 60s+ stalls seen during pytest runs."""
import time

import requests
from dotenv import dotenv_values

BASE = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/")
COOKIE = {"vms_session": "KGABHXazt4DGxMLppFGLi9bbLu25Z9m9"}

slow = 0
for i in range(24):
    t0 = time.time()
    try:
        r = requests.get(f"{BASE}/api/admin/grn/sites", cookies=COOKIE, timeout=65)
        dt = time.time() - t0
        ct = r.headers.get("content-type", "")
        print(f"{i:02d} status={r.status_code} {dt:6.2f}s ct={ct}")
        if dt > 5 or r.status_code != 200:
            slow += 1
    except Exception as e:
        print(f"{i:02d} EXC after {time.time()-t0:6.2f}s: {type(e).__name__}")
        slow += 1
    time.sleep(5)
print(f"slow_or_failed={slow}/24")
