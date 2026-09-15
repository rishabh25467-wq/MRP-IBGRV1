import { ArrowRight, ListChecks, MagnifyingGlass, PencilSimple, FloppyDisk, Info, CheckCircle } from "@phosphor-icons/react";

const STEPS = [
  {
    number: 1,
    title: "Open List Prices",
    description: "Go to the Product Portfolio work center, then from the Overview menu select List Prices under the Products section.",
    image: "/list-price-assets/step1.png",
    icon: ListChecks,
  },
  {
    number: 2,
    title: "Search and edit",
    description: "On the List Prices screen, search for the item using the Product ID column, select the row, then click the Edit button.",
    image: "/list-price-assets/step2.png",
    icon: MagnifyingGlass,
  },
  {
    number: 3,
    title: "Update price and save",
    description: "In the edit screen, click the Price field and enter the new value, check/update the Valid From date, then click Save to apply the list price change.",
    image: "/list-price-assets/step3.png",
    icon: PencilSimple,
  },
];

const NEW_ITEM_STEPS = [
  {
    number: 1,
    title: "Define settings",
    description: "Click New List Price. On the Define Settings step, find and enter the Supplier, then check the Price Valid From/To dates are correct. Click Next.",
    image: "/list-price-assets/new-item-step1.png",
    icon: MagnifyingGlass,
  },
  {
    number: 2,
    title: "Select item, set price and release",
    description: "On the Enter Price step, select the item (Product ID) and set the Price. Check the Release checkbox is ticked if it should be active immediately, then click Finish.",
    image: "/list-price-assets/new-item-step2.png",
    icon: CheckCircle,
  },
];

export default function ListPriceInstructionsPage() {
  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="list-price-instructions-page">
      <header className="min-h-16 bg-[#0E7C86] shadow-[0_1px_3px_0_rgba(16,24,40,0.15)] flex items-center px-4 sm:px-6 shrink-0 z-10">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-md bg-white/15 flex items-center justify-center">
            <FloppyDisk size={18} className="text-white" weight="fill" />
          </div>
          <span className="font-heading text-[16px] font-bold text-white tracking-tight">Materials Hub - Work Instruction</span>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 py-8 sm:py-12">
        <div className="mb-10">
          <p className="font-data text-xs font-bold text-[#004B87] uppercase tracking-widest mb-2" data-testid="wi-doc-code">SOP-LP-01 &middot; Product Portfolio</p>
          <h1 className="font-heading text-3xl sm:text-4xl font-bold text-[#101828] tracking-tight" data-testid="wi-page-title">
            How to Update an Item's List Price in SAP
          </h1>
          <p className="text-base text-[#475569] mt-3 max-w-2xl leading-relaxed">
            Follow these 3 steps to change a supplier's list price for a product. This is a step-by-step guide for staff working in the Product Portfolio work center.
          </p>
        </div>

        <div className="flex items-start gap-3 bg-[#EFF6FF] border border-[#BFDBFE] rounded-lg p-4 mb-10" data-testid="wi-info-banner">
          <Info size={20} className="text-[#1E40AF] shrink-0 mt-0.5" weight="fill" />
          <p className="text-sm text-[#1E40AF] leading-relaxed">
            Only users with access to the Product Portfolio work center in SAP Business ByDesign can perform this task. Always double-check the <span className="font-semibold">Valid From</span> date before saving - list prices apply from that date onward.
          </p>
        </div>

        <ol className="space-y-8">
          {STEPS.map((step, idx) => {
            const Icon = step.icon;
            return (
              <li key={step.number} className="relative" data-testid={`wi-step-${step.number}`}>
                <div className="bg-white border border-[#E2E8F0] rounded-xl overflow-hidden shadow-sm">
                  <div className="flex items-center gap-3 px-5 sm:px-6 py-4 border-b border-[#EAECF0] bg-[#F8FAFC]">
                    <div className="w-9 h-9 rounded-full bg-[#004B87] text-white font-heading font-bold text-sm flex items-center justify-center shrink-0" data-testid={`wi-step-${step.number}-number`}>
                      {step.number}
                    </div>
                    <Icon size={18} className="text-[#004B87] shrink-0" />
                    <h2 className="font-heading text-lg font-semibold text-[#101828] tracking-tight" data-testid={`wi-step-${step.number}-title`}>
                      {step.title}
                    </h2>
                  </div>
                  <div className="p-5 sm:p-6">
                    <p className="text-sm text-[#475569] leading-relaxed mb-5" data-testid={`wi-step-${step.number}-description`}>
                      {step.description}
                    </p>
                    <div className="rounded-lg border border-[#D0D5DD] overflow-hidden bg-[#F2F4F7]">
                      <img
                        src={step.image}
                        alt={`Step ${step.number}: ${step.title}`}
                        className="w-full h-auto block"
                        data-testid={`wi-step-${step.number}-screenshot`}
                      />
                    </div>
                  </div>
                </div>
                {idx < STEPS.length - 1 && (
                  <div className="flex justify-center py-2">
                    <ArrowRight size={20} className="text-[#94A3B8]" />
                  </div>
                )}
              </li>
            );
          })}
        </ol>

        <div className="mt-16 mb-10" data-testid="wi-new-item-section">
          <p className="font-data text-xs font-bold text-[#004B87] uppercase tracking-widest mb-2" data-testid="wi-new-item-doc-code">SOP-LP-02 &middot; Product Portfolio</p>
          <h1 className="font-heading text-3xl sm:text-4xl font-bold text-[#101828] tracking-tight" data-testid="wi-new-item-title">
            How to Create a New List Price for a Supplier
          </h1>
          <p className="text-base text-[#475569] mt-3 max-w-2xl leading-relaxed">
            Follow these 2 steps to create a brand-new list price entry for an item and supplier combination that doesn't have one yet.
          </p>
        </div>

        <div className="flex items-start gap-3 bg-[#EFF6FF] border border-[#BFDBFE] rounded-lg p-4 mb-10" data-testid="wi-new-item-info-banner">
          <Info size={20} className="text-[#1E40AF] shrink-0 mt-0.5" weight="fill" />
          <p className="text-sm text-[#1E40AF] leading-relaxed">
            Make sure the <span className="font-semibold">Release</span> checkbox is ticked before clicking Finish if the price should go live immediately - unchecked entries stay in draft until released separately.
          </p>
        </div>

        <ol className="space-y-8">
          {NEW_ITEM_STEPS.map((step, idx) => {
            const Icon = step.icon;
            return (
              <li key={step.number} className="relative" data-testid={`wi-new-item-step-${step.number}`}>
                <div className="bg-white border border-[#E2E8F0] rounded-xl overflow-hidden shadow-sm">
                  <div className="flex items-center gap-3 px-5 sm:px-6 py-4 border-b border-[#EAECF0] bg-[#F8FAFC]">
                    <div className="w-9 h-9 rounded-full bg-[#004B87] text-white font-heading font-bold text-sm flex items-center justify-center shrink-0" data-testid={`wi-new-item-step-${step.number}-number`}>
                      {step.number}
                    </div>
                    <Icon size={18} className="text-[#004B87] shrink-0" />
                    <h2 className="font-heading text-lg font-semibold text-[#101828] tracking-tight" data-testid={`wi-new-item-step-${step.number}-title`}>
                      {step.title}
                    </h2>
                  </div>
                  <div className="p-5 sm:p-6">
                    <p className="text-sm text-[#475569] leading-relaxed mb-5" data-testid={`wi-new-item-step-${step.number}-description`}>
                      {step.description}
                    </p>
                    <div className="rounded-lg border border-[#D0D5DD] overflow-hidden bg-[#F2F4F7]">
                      <img
                        src={step.image}
                        alt={`Step ${step.number}: ${step.title}`}
                        className="w-full h-auto block"
                        data-testid={`wi-new-item-step-${step.number}-screenshot`}
                      />
                    </div>
                  </div>
                </div>
                {idx < NEW_ITEM_STEPS.length - 1 && (
                  <div className="flex justify-center py-2">
                    <ArrowRight size={20} className="text-[#94A3B8]" />
                  </div>
                )}
              </li>
            );
          })}
        </ol>

        <div className="mt-12 border-t border-[#EAECF0] pt-6 text-center">
          <p className="text-xs text-[#94A3B8] font-data">Materials Hub Work Instruction &middot; List Price Update</p>
        </div>
      </main>
    </div>
  );
}
