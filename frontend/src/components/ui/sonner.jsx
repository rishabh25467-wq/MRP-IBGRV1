import { Toaster as Sonner, toast } from "sonner"

// Hardcoded to light theme (no next-themes ThemeProvider exists in this app,
// so theme="system" let Sonner's OS-dark-mode detection apply white text
// while bg stayed light => unreadable white-on-white toasts).
//
// Color-coding by type (success=green, error=red, warning=amber, info=blue)
// reads Sonner's own `data-type` attribute:
// - On the `toast` key (the <li> itself, which ALSO carries the "group"
//   class): use the SELF-referential `data-[type=x]:` variant (no "group-"
//   prefix) - `group-data-[type=x]:` compiles to a descendant selector
//   (`.group[data-type=x] &`) which requires "group" to be on an ANCESTOR,
//   so it can never match the very element that carries "group" itself.
// - On `title`/`description` (genuine descendants of that <li>): use
//   `group-data-[type=x]:`, which correctly resolves against the <li>
//   ancestor.
// Both are needed with `!important` since Sonner's own bundled CSS also
// sets these properties and would otherwise win depending on stylesheet
// generation order (confirmed non-deterministic per-property in testing).
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
            "data-[type=success]:!bg-[#ECFDF3] data-[type=success]:!border-[#ABEFC6] " +
            "data-[type=error]:!bg-[#FEF3F2] data-[type=error]:!border-[#FECDCA] " +
            "data-[type=warning]:!bg-[#FFFAEB] data-[type=warning]:!border-[#FEDF89] " +
            "data-[type=info]:!bg-[#EFF4FF] data-[type=info]:!border-[#B8D4ED]",
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
