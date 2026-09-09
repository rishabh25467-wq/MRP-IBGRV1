import { Buildings, ClockCountdown, XCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { useSupplierAuth } from "@/contexts/SupplierAuthContext";

export default function SupplierPendingPage() {
  const { account, logout } = useSupplierAuth();
  const rejected = account?.status === "rejected";

  return (
    <div className="min-h-screen bg-[#F2F4F7] font-sans supplier-fiori-font flex items-center justify-center p-4" data-testid="supplier-pending-page">
      <div className="max-w-md w-full bg-white rounded-sm border border-[#D0D5DD] shadow-sm p-8 text-center">
        <div className="flex items-center justify-center gap-2 mb-4 pb-3 border-b border-[#D0D5DD]">
          <Buildings size={20} weight="fill" className="text-[#004B87]" />
          <span className="font-heading font-bold text-[#1D2939] tracking-tight">Supplier Portal</span>
        </div>
        {rejected ? (
          <>
            <XCircle size={38} weight="fill" className="text-[#E02424] mx-auto mb-3" data-testid="supplier-pending-rejected-icon" />
            <h1 className="font-heading text-lg font-bold text-[#1D2939]">Onboarding Rejected</h1>
            <p className="text-sm text-[#475467] mt-2 bg-[#F2F4F7] border border-[#D0D5DD] rounded-sm px-3 py-2">
              {account?.rejection_reason || "Your onboarding request was not approved."} Please contact the purchasing team for details.
            </p>
          </>
        ) : (
          <>
            <ClockCountdown size={38} weight="fill" className="text-[#E3A008] mx-auto mb-3" data-testid="supplier-pending-icon" />
            <h1 className="font-heading text-lg font-bold text-[#1D2939]">Approval Pending</h1>
            <p className="text-sm text-[#475467] mt-2">
              Your onboarding request for <span className="font-semibold text-[#1D2939]">{account?.company_name}</span> is awaiting review by our purchasing team.
              You'll be able to view your Purchase Orders as soon as it's approved.
            </p>
          </>
        )}
        <Button onClick={logout} className="mt-6 w-full rounded-sm bg-[#004B87] hover:bg-[#003A6A] transition-colors duration-150" data-testid="supplier-pending-logout-button">Sign Out</Button>
      </div>
    </div>
  );
}
