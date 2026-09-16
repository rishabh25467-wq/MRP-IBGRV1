import { Shield, WarningCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";

export default function LoginPage() {
  const { login } = useAuth();
  const params = new URLSearchParams(window.location.search);
  const authError = params.get("auth_error");
  const authReason = params.get("auth_reason");

  return (
    <div className="min-h-screen bg-[#0E7C86] flex items-center justify-center p-4" data-testid="login-page">
      <div className="w-full max-w-sm bg-white rounded-2xl shadow-[0_20px_60px_-15px_rgba(0,0,0,0.4)] p-8 flex flex-col items-center gap-6">
        <div className="w-14 h-14 rounded-xl bg-[#0E7C86]/10 flex items-center justify-center">
          <Shield size={28} weight="fill" className="text-[#0E7C86]" />
        </div>
        <div className="text-center">
          <h1 className="font-heading text-xl font-bold text-[#101828]">Materials Hub</h1>
          <p className="text-sm text-[#667085] mt-1">Sign in with your organization account to continue</p>
        </div>
        <Button
          onClick={login}
          className="w-full h-11 bg-[#0E7C86] hover:bg-[#0B6B74] text-white font-bold rounded-lg gap-2.5"
          data-testid="microsoft-login-button"
        >
          <svg width="18" height="18" viewBox="0 0 21 21" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect x="1" y="1" width="9" height="9" fill="#F25022" />
            <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
            <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
            <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
          </svg>
          Continue with Microsoft
        </Button>
        <p className="text-xs text-[#98A2B3] text-center">
          Access is managed by your IT administrator. Contact them if you're unable to sign in.
        </p>
        {authError && (
          <div
            className="w-full flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg p-3 text-xs text-red-700"
            data-testid="login-error-banner"
          >
            <WarningCircle size={16} weight="fill" className="shrink-0 mt-0.5" />
            <span>{authReason ? decodeURIComponent(authReason) : "Sign-in failed. Please try again."}</span>
          </div>
        )}
      </div>
    </div>
  );
}
