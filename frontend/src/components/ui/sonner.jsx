import { Toaster as Sonner, toast } from "sonner"

// Hardcoded to light theme with explicit hex colors (not CSS-variable-based
// utility classes) - this app has no next-themes ThemeProvider, so
// theme="system" was letting Sonner's own OS-dark-mode detection apply its
// internal white text while our bg stayed light, making toasts unreadable
// (white text on white background). `!` (important) modifiers guarantee
// these win over Sonner's bundled base styles regardless of cascade order.
const Toaster = ({
  ...props
}) => {
  return (
    <Sonner
      theme="light"
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            "group toast !bg-white !text-[#101828] !border !border-[#D0D5DD] !shadow-lg !rounded-sm",
          title: "!text-[#101828] !font-medium",
          description: "!text-[#475467]",
          actionButton: "!bg-[#004B87] !text-white",
          cancelButton: "!bg-[#F2F4F7] !text-[#475467]",
          success: "!bg-[#ECFDF3] !text-[#027A48] !border-[#ABEFC6]",
          error: "!bg-[#FEF3F2] !text-[#B42318] !border-[#FECDCA]",
          warning: "!bg-[#FFFAEB] !text-[#B54708] !border-[#FEDF89]",
          info: "!bg-[#EFF4FF] !text-[#004B87] !border-[#B8D4ED]",
        },
      }}
      {...props} />
  );
}

export { Toaster, toast }
