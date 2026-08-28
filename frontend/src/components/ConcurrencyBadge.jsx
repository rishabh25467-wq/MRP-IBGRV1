import { useEffect, useState } from "react";
import axios from "axios";
import { CircleNotch } from "@phosphor-icons/react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

// Global "how busy is background processing right now" badge (user's
// explicit ask, Aug 2026) - shown in the bottom corner on every internal
// page. Wording stays generic on purpose ("Background tasks") - must
// never reveal SAP/Playwright/browser automation to the end user (same
// rule InboundReceiptsPage.js follows for its own progress copy).
export function ConcurrencyBadge() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      axios.get(`${API}/system/job-concurrency`).then(({ data }) => {
        if (!cancelled) setStatus(data);
      }).catch(() => {});
    };
    poll();
    const interval = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!status || (status.active === 0 && status.queued === 0)) return null;

  return (
    <div
      className="print:hidden fixed bottom-4 right-4 z-50 flex items-center gap-2 rounded-full bg-[#0B6B74] text-white text-xs font-medium px-3 py-1.5 shadow-lg"
      data-testid="concurrency-badge"
    >
      <CircleNotch size={13} className="animate-spin shrink-0" />
      <span data-testid="concurrency-badge-text">
        Background tasks: {status.active}/{status.max} running
        {status.queued > 0 ? `, ${status.queued} queued` : ""}
      </span>
    </div>
  );
}
