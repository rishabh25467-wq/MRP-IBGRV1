import { useState, useEffect, useCallback } from "react";
import axios from "axios";
import { Circle } from "@phosphor-icons/react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

// Aug 2026, user's explicit ask: the SAP live/broken indicator that only
// existed on the BOM Explorer (home) page header should show on every
// page so a user always knows SAP's connection status, not just on one
// screen. Same endpoint/logic as BomExplorerPage's original indicator.
export const SapConnectionStatus = () => {
  const [connection, setConnection] = useState({ connected: null, message: "Checking connection..." });

  const checkConnection = useCallback(async () => {
    try {
      const { data } = await axios.get(`${API}/bom/connection-status`);
      setConnection(data);
    } catch {
      setConnection({ connected: false, message: "Unable to reach backend" });
    }
  }, []);

  useEffect(() => {
    checkConnection();
    const interval = setInterval(checkConnection, 60000);
    return () => clearInterval(interval);
  }, [checkConnection]);

  return (
    <div
      className="flex items-center gap-1.5 bg-white/10 border border-white/20 px-2.5 py-1 rounded-full shrink-0"
      data-testid="connection-status-indicator"
      title={connection.message}
    >
      {connection.connected === null ? (
        <Circle size={8} weight="fill" className="text-[#F59E0B] animate-pulse" />
      ) : connection.connected ? (
        <Circle size={8} weight="fill" className="text-[#10B981] animate-pulse" />
      ) : (
        <Circle size={8} weight="fill" className="text-[#EF4444]" />
      )}
      <span className="font-sans text-[11px] text-white whitespace-nowrap hidden md:inline">
        {connection.connected === null
          ? "Checking SAP..."
          : connection.connected
          ? "SAP PRD Connected"
          : "SAP Disconnected"}
      </span>
    </div>
  );
};
