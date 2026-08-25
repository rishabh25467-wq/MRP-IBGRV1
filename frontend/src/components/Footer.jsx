import { useEffect, useState } from "react";
import axios from "axios";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

// Aug 27 2026, user's explicit ask: "add build version on footer" - this
// repo has no separate semantic-version bump step, so the current git
// commit (fetched once from the backend) is the most accurate,
// zero-maintenance stand-in for "what build is actually running".
export function Footer() {
  const [version, setVersion] = useState(null);

  useEffect(() => {
    axios.get(`${API}/version`).then(({ data }) => setVersion(data)).catch(() => setVersion(null));
  }, []);

  return (
    <footer className="print:hidden shrink-0 border-t border-[#EAECF0] bg-white px-4 py-2 text-center text-[11px] text-[#98A2B3]" data-testid="app-footer">
      {version?.commit ? `Build ${version.commit} \u00b7 ${version.commit_date}` : "\u00a0"}
    </footer>
  );
}
