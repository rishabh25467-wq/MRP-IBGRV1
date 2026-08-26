import { Buildings, ClockCountdown, XCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

export default function SupplierPendingPage() {
  const { account, logout } = useSupplierAuth();
  const rejected = account?.status === "rejected";

  return (
    <div className="min-h-screen bg-[#0F172A] flex items-center justify-center p-4" data-testid="supplier-pending-page">
      <div className="max-w-md w-full bg-white rounded-2xl p-8 text-center shadow-2xl">
        <div className="flex items-center justify-center gap-2 mb-4">
          <Buildings size={22} weight="fill" className="text-[#F59E0B]" />
          <span className="font-heading font-bold text-[#0F172A] tracking-tight">Supplier Portal</span>
        </div>
        {rejected ? (
          <>
            <XCircle size={40} weight="fill" className="text-[#B42318] mx-auto mb-3" data-testid="supplier-pending-rejected-icon" />
            <h1 className="font-heading text-lg font-bold text-[#0F172A]">Onboarding Rejected</h1>
            <p className="text-sm text-[#475467] mt-2">
              {account?.rejection_reason || "Your onboarding request was not approved."} Please contact the purchasing team for details.
            </p>
          </>
        ) : (
          <>
            <ClockCountdown size={40} weight="fill" className="text-[#F59E0B] mx-auto mb-3" data-testid="supplier-pending-icon" />
            <h1 className="font-heading text-lg font-bold text-[#0F172A]">Approval Pending</h1>
            <p className="text-sm text-[#475467] mt-2">
              Your onboarding request for <span className="font-semibold">{account?.company_name}</span> is awaiting review by our purchasing team.
              You'll be able to view your Purchase Orders as soon as it's approved.
            </p>
          </>
        )}
        <Button onClick={logout} className="mt-6 w-full bg-[#0F172A] hover:bg-[#1E293B]" data-testid="supplier-pending-logout-button">Sign Out</Button>
      </div>
    </div>
  );
}
