import { Hourglass, SignOut } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";

export default function PendingAccessPage() {
  const { user, logout } = useAuth();

  return (
    <div className="min-h-screen bg-[#F9FAFB] flex items-center justify-center p-4" data-testid="pending-access-page">
      <div className="w-full max-w-md bg-white rounded-2xl border border-[#E4E7EC] shadow-sm p-8 flex flex-col items-center gap-5 text-center">
        <div className="w-14 h-14 rounded-xl bg-[#FEF3C7] flex items-center justify-center">
          <Hourglass size={26} weight="fill" className="text-[#D97706]" />
        </div>
        <div>
          <h1 className="font-heading text-lg font-bold text-[#101828]">Access Pending Approval</h1>
          <p className="text-sm text-[#667085] mt-2">
            You're signed in as <span className="font-semibold text-[#344054]">{user?.email}</span>, but an IT
            administrator hasn't granted you access to any pages yet.
          </p>
          <p className="text-sm text-[#667085] mt-2">
            Ask a Super Admin to grant you access on the Access Management page, then refresh this page.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={logout}
          className="gap-2 border-[#D0D5DD] text-[#344054]"
          data-testid="pending-access-sign-out-button"
        >
          <SignOut size={16} weight="bold" />
          Sign Out
        </Button>
      </div>
    </div>
  );
}
