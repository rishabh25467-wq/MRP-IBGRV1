// DEMO/PLACEHOLDER DATA ONLY - Audits & QC page (Sep 2026). A real QMS
// system will eventually push live Q-Notifications and Audit
// Non-Conformances via a future API feed (Phase 5 in PRD.md); until then
// this deterministically generates realistic-looking placeholder records
// per vendor_code so the same vendor always sees the same demo data.
function seedFromString(str) {
  let h = 0;
  for (let i = 0; i < (str || "").length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
  return h;
}

const DEFECT_TYPES = [
  "Dimensional Deviation", "Surface Finish Defect", "Material Hardness Out of Spec",
  "Coating Thickness Variance", "Packaging Damage", "Incorrect Marking/Labeling",
];
const SEVERITIES = ["Critical", "Major", "Minor"];
const Q_STATUSES = ["Open", "Under Investigation", "Closed"];
const ISO_CLAUSES = [
  "ISO 9001:2015 Cl. 8.5.1", "ISO 9001:2015 Cl. 8.4.2", "ISO 9001:2015 Cl. 9.1.3",
  "IATF 16949 Cl. 8.7.1", "ISO 9001:2015 Cl. 7.1.5",
];
const FINDINGS = [
  "Incoming inspection records incomplete for last 3 lots",
  "Calibration certificate for gauge GC-014 expired",
  "Process control chart not maintained for CNC line 2",
  "Non-conforming material disposition not documented",
  "Corrective action from previous audit not verified for effectiveness",
];
const RISK_LEVELS = ["High", "Medium", "Low"];
const NC_STATUSES = ["Open", "Closed"];

function daysAgoIso(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

export function getMockQmsData(vendorCode) {
  const seed = seedFromString(vendorCode || "DEMO");

  const qNotifications = Array.from({ length: 6 }, (_, i) => {
    const s = seed + i * 17;
    return {
      id: `QN-${(seed % 900) + 100}-${i + 1}`,
      defect_type: DEFECT_TYPES[s % DEFECT_TYPES.length],
      material_code: `P${(26000 + (s % 900)).toString()}`,
      affected_qty: 5 + (s % 45),
      inspection_lot: `INSP-${(s % 90000) + 10000}`,
      severity: SEVERITIES[s % SEVERITIES.length],
      status: Q_STATUSES[(s + i) % Q_STATUSES.length],
      raised_on: daysAgoIso(5 + (s % 60)),
    };
  });

  const auditNCs = Array.from({ length: 5 }, (_, i) => {
    const s = seed + i * 23 + 3;
    return {
      id: `NC-${(seed % 700) + 100}-${i + 1}`,
      audit_date: daysAgoIso(10 + (s % 90)),
      finding: FINDINGS[s % FINDINGS.length],
      iso_clause: ISO_CLAUSES[s % ISO_CLAUSES.length],
      risk_level: RISK_LEVELS[s % RISK_LEVELS.length],
      status: NC_STATUSES[(s + i) % NC_STATUSES.length],
    };
  });

  const closedQ = qNotifications.filter((q) => q.status === "Closed").length;
  const closedNc = auditNCs.filter((nc) => nc.status === "Closed").length;
  const totalRecords = qNotifications.length + auditNCs.length;
  const passRate = Math.round(((closedQ + closedNc) / totalRecords) * 100);

  return {
    qNotifications,
    auditNCs,
    metrics: {
      totalQNotifications: qNotifications.length,
      openAuditNCs: auditNCs.filter((nc) => nc.status !== "Closed").length,
      passRate,
    },
  };
}
