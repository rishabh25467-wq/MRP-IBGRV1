import { Link, useLocation } from "react-router-dom";

const TABS = [
  { to: "/", label: "BOM Explorer", testId: "nav-bom-explorer" },
  { to: "/purchasing-plan", label: "Purchasing Plan", testId: "nav-purchasing-plan" },
  { to: "/inventory", label: "Inventory", testId: "nav-inventory" },
  { to: "/admin", label: "Admin", testId: "nav-admin" },
];

export const NavTabs = () => {
  const { pathname } = useLocation();
  return (
    <div className="flex items-center gap-1">
      {TABS.map((tab) => {
        const active = pathname === tab.to;
        return (
          <Link
            key={tab.to}
            to={tab.to}
            className={`px-3 py-1 rounded-sm text-xs font-bold font-heading transition-colors ${
              active ? "bg-white/20 text-white" : "text-white/70 hover:bg-white/10 hover:text-white"
            }`}
            data-testid={tab.testId}
          >
            {tab.label}
          </Link>
        );
      })}
    </div>
  );
};
