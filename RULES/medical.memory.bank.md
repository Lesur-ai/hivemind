# Non-Clinical Health Notes Rules — Hivemind Demonstration Template

## Safety Boundary

This template demonstrates how Hivemind can organize **synthetic or
appropriately de-identified notes** for product evaluation. It is not a clinical
record, medical device, monitoring service, diagnostic tool, triage system, or
treatment decision-support system. Do not use it to provide or coordinate
patient care.

The external clinical record maintained by qualified healthcare providers is
authoritative. Hivemind mid and long memory are derived convenience layers that
may omit, summarize, duplicate, or misstate information. A qualified human must
verify every retained fact against the external record before any downstream
use. An unverified Hivemind summary must never be copied into a clinical record
or used to make a care decision.

Hivemind and its LLM must never:

- diagnose, rule out, rank, or predict a condition;
- interpret symptoms, measurements, images, laboratory values, or trends;
- recommend, select, change, stop, or dose a medication or treatment;
- perform risk scoring, triage, clinical monitoring, or alerting;
- define normal ranges, personalized thresholds, or emergency actions;
- replace a clinician, pharmacist, emergency service, or authoritative record.

Medical emergencies are outside Hivemind. Contact the appropriate local
emergency services or a qualified healthcare professional immediately; never
wait for a Hivemind or LLM response.

## Sensitive-Data and LLM Guard

Use synthetic data by default. Before processing any real health information,
the operator must establish lawful authority, consent where required, data
minimization, access control, encryption, retention/deletion rules, incident
response, and approval for every storage and LLM provider involved.

Rules, short notes, and existing mid-memory content can be included in requests
to the configured LLM during consolidation. Do not submit direct identifiers,
contact details, credentials, record numbers, free-text clinical documents, or
other sensitive data unless that exact end-to-end processing path is approved.
Prefer references to records in an authorized external system over copied
clinical payloads.

## Demonstration File Structure

```text
scopeAndSources.md      # demo purpose, data classification, authorities
sourceIndex.md          # references to external records, not a clinical copy
conversationNotes.md    # neutral, attributed, explicitly unverified notes
questionsForReview.md   # questions for a qualified human; no generated answers
verificationLog.md      # provenance, reviewer, status, corrections
activeContext.md        # current demo tasks and unresolved data-quality issues
progress.md             # demonstration workflow history
```

### `scopeAndSources.md`

- State that the workspace is non-clinical and identify the demonstration goal.
- Name the authorized external system of record and qualified reviewers.
- Record whether data is synthetic, de-identified, or approved real data.
- Document allowed data classes, retention, access, and deletion requirements.
- Never copy credentials, access tokens, or emergency instructions.

### `sourceIndex.md`

- Store only the minimum reference needed to locate an authorized source.
- Record source type, external identifier, document date, and owner when allowed.
- Do not infer contents from a filename, code, or missing record.
- Mark inaccessible or ambiguous sources for human resolution.

### `conversationNotes.md`

- Attribute statements to the speaker or source and preserve uncertainty.
- Label every entry `UNVERIFIED` until a qualified reviewer validates it.
- Record neutral wording only; do not add interpretation or advice.
- Avoid direct identifiers and unnecessary sensitive detail.

### `questionsForReview.md`

- Capture user-authored questions for a qualified healthcare professional.
- Do not answer, prioritize, or reframe questions as clinical recommendations.
- Mark who is expected to review the question and its non-urgent workflow status.
- Urgent concerns must be redirected outside Hivemind to local emergency or
  professional care channels.

### `verificationLog.md`

- For each reviewed item, record source reference, reviewer identity/role,
  review date, and status: `UNVERIFIED`, `VERIFIED`, `REJECTED`, or `CORRECTED`.
- A correction must retain the prior claim as rejected and link the authority.
- LLM output can never set its own status to `VERIFIED`.

### `activeContext.md`

- Track only demonstration tasks, permissions, missing sources, and verification
  work.
- Do not contain a health assessment, care plan, risk status, alert, or timeline
  intended for clinical use.
- Keep unresolved provenance conflicts visible until a human resolves them.

### `progress.md`

- Record changes to the demonstration workflow and completed human reviews.
- Do not present completeness percentages for a person's health record.
- Keep canonical audit or clinical history in the approved external system.

## Safe Note-Category Mapping

- `observation` → neutral source receipt or attributed statement in
  `conversationNotes.md`, always `UNVERIFIED` initially.
- `decision` → product, privacy, access, or retention decision only; never a
  clinical decision.
- `progress` → demonstration or human-verification progress only.
- `issue` → provenance, permission, redaction, duplication, or data-quality issue.
- `todo` → non-urgent administrative demonstration task or human review.
- `insight` → product/workflow learning only; never a health correlation.
- `question` → `questionsForReview.md` without an answer or priority inference.

## Consolidator Rules

1. Treat the external clinical record as authoritative and Hivemind as derived.
2. Never invent, complete, interpret, normalize, or clinically classify content.
3. Preserve attribution, source reference, date, units, and uncertainty exactly
   when they are explicitly provided; otherwise mark the field missing.
4. Do not claim error-free transcription or completeness. Flag every item for human
   comparison with its source.
5. On conflict, keep both claims, label the conflict, and request qualified human
   review; never choose a winner from context or majority.
6. Redact or omit sensitive content that is outside the approved data scope.
7. Never generate thresholds, alerts, diagnoses, treatment suggestions,
   emergency procedures, or statements that a value is normal or abnormal.
8. Keep long/graph memory non-authoritative; it may locate a reference but never
   validate a health fact or verification status.
9. Remove obsolete or duplicate derived text only after preserving provenance
   and the human-reviewed correction in `verificationLog.md`.
10. If the request crosses the non-clinical boundary, stop and direct the user
    to the authoritative record and an appropriate qualified professional.

## Allowed Demonstration Example

> `UNVERIFIED — Source ref LAB-DEMO-004, dated 2026-01-15, contains a value
> recorded as 12 mg/L. No interpretation performed. Human comparison pending.`

## Prohibited Output Examples

- “This result is high/low/normal.”
- “These symptoms suggest …”
- “Change the dose or treatment.”
- “Wait and monitor; seek help only if …”
- “Hivemind has verified the medical record.”
