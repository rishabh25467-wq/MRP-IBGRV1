import { Toaster as Sonner, toast } from "sonner"

// Hardcoded to light theme (no next-themes ThemeProvider exists in this app,
// so theme="system" let Sonner's OS-dark-mode detection apply white text
// while bg stayed light => unreadable white-on-white toasts).
//
// Color-coding by type (success=green, error=red, warning=amber, info=blue)
// is done via `group-data-[type=x]:` selectors reading Sonner's own
// `data-type` attribute on the toast <li> (which also carries the "group"
// class from the `toast` key below) - NOT via Sonner's separate
// success/error/warning/info classNames keys. Those keys get merged
// alongside the base `toast` key onto the SAME element, so two competing
// `!important` utility classes end up fighting for the same CSS property
// (background-color, color) with identical specificity - Tailwind then
// breaks the tie by stylesheet generation order, which is unpredictable
// and empirically inconsistent per-property (confirmed: bg lost the tie,
// text color won it). A single group-data selector chain has no competing
// declaration for the same property, so it's deterministic.
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
            "group toast !border !shadow-lg !rounded-sm bg-white border-[#D0D5DD] " +
            "group-data-[type=success]:!bg-[#ECFDF3] group-data-[type=success]:!border-[#ABEFC6] " +
            "group-data-[type=error]:!bg-[#FEF3F2] group-data-[type=error]:!border-[#FECDCA] " +
            "group-data-[type=warning]:!bg-[#FFFAEB] group-data-[type=warning]:!border-[#FEDF89] " +
            "group-data-[type=info]:!bg-[#EFF4FF] group-data-[type=info]:!border-[#B8D4ED]",
          title:
            "!font-medium text-[#101828] " +
            "group-data-[type=success]:!text-[#027A48] group-data-[type=error]:!text-[#B42318] " +
            "group-data-[type=warning]:!text-[#B54708] group-data-[type=info]:!text-[#004B87]",
          description:
            "text-[#475467] " +
            "group-data-[type=success]:!text-[#027A48] group-data-[type=error]:!text-[#B42318] " +
            "group-data-[type=warning]:!text-[#B54708] group-data-[type=info]:!text-[#004B87]",
          actionButton: "!bg-[#004B87] !text-white",
          cancelButton: "!bg-[#F2F4F7] !text-[#475467]",
        },
      }}
      {...props} />
  );
}

export { Toaster, toast }
