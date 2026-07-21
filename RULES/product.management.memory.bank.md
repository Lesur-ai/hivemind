# Product Management Memory Bank Rules — Hivemind Template

## Core Principle

The Product Management Memory Bank is shared working context for a product
team. Approved strategy, product analytics, research evidence, specifications,
roadmaps, source code, and release records remain authoritative. Agents may
also retain local context; verify consequential product claims and decisions
against their canonical evidence.

**Product decisions can have lasting impact. Preserve material insights,
feedback, rationale, and trade-offs with provenance, while keeping human owners
responsible for validation and decisions.**

## File Structure and Hierarchy

Files build on each other in a clear hierarchy:

```
productVision.md (foundation — mission, vision, positioning, values)
├── portfolio.md (product catalog: lines, features, state, dependencies)
├── marketIntelligence.md (competition, market, regulation, trends)
├── userKnowledge.md (cross-persona synthesis, JTBD, feedback patterns)
│   └── persona-[name].md (dynamic — created as insights emerge)
├── stakeholders.md (internal stakeholders, communication matrix, dynamics)
├── designSystem.md (UX principles, patterns, components, conventions)
├── communicationGuide.md (product voice, UX writing, PM communication styles)
├── engineeringContext.md (architecture, tech constraints, debt, capacity)
└── discoveryPlaybook.md (discovery pipeline, frameworks, templates)
    ├── framework-[name].md (dynamic — strategic reference frameworks)
    └── activeContext.md (current focus — session entry point)
        └── roadmapProgress.md (roadmap, delivered, backlog, decision log)
```

- `productVision.md` is the foundational document that shapes all others
- Domain files (`portfolio`, `marketIntelligence`, `userKnowledge`, `stakeholders`, `designSystem`, `communicationGuide`, `engineeringContext`) provide specialized context
- `discoveryPlaybook.md` defines how the team works, with optional `framework-*.md` reference files
- `activeContext.md` synthesizes the current focus from all other files
- `roadmapProgress.md` tracks advancement and decision history

## Mandatory Files (11 files)

### productVision.md
**Product foundation — rarely modified.**
- Mission and vision: why the product exists and where it's headed
- Product values: non-negotiable principles (e.g., security, privacy, openness)
- Strategic positioning: what differentiates the product in the market
- Target market segments and ideal customer profiles
- Anti-scope: what the product is NOT and will NOT become
- North-star metrics and current strategic OKRs
- Business model essentials (pricing tiers, revenue drivers)
- This file only changes if the product fundamentally pivots
- Every new agent must read this file first

### portfolio.md
**Living product catalog — updated as features evolve.**
- Product suite overview organized by product line or subscription tier
- Per feature: status (GA / beta / alpha / planned / deprecated), supported platforms, known limitations
- Feature × segment matrix (what's available for each customer segment)
- Dependencies between features and components
- Product debt: broken UX, inconsistencies, incomplete features, legacy issues
- Existing integrations and extension points
- This file summarizes current capability claims; verify them against shipped code, release records, and approved product documentation

### marketIntelligence.md
**External awareness — competitive and market context.**
- Competitive landscape: key competitors with relative strengths/weaknesses
- Positioning axes: how the product compares on key dimensions (security, UX, price, compliance, etc.)
- Market trends: regulatory shifts, technology trends, customer behavior changes
- Regulatory constraints: compliance requirements relevant to the product (data protection, certifications, industry standards)
- Win/loss patterns: why deals are won or lost, recurring themes from sales
- Weak signals and market opportunities: emerging needs not yet addressed
- This file helps the agent make market-aware product recommendations

### userKnowledge.md
**Cross-persona user intelligence — the voice of the customer.**
- Jobs to Be Done by segment: what customers hire the product to accomplish
- Recurring pain points across all personas (top 10, ranked by frequency/severity)
- Feedback patterns: themes from support tickets, NPS surveys, feature requests, user interviews
- User research insights: synthesized findings (summaries, not raw data)
- Behavioral patterns: how users actually use the product vs. how it was designed
- Feature adoption metrics and usage patterns (if tracked)
- This file provides the transverse view; persona-specific details live in persona files

### stakeholders.md
**Internal stakeholder intelligence — how to navigate the organization.**
- Key stakeholders: name, role, scope of responsibility, decision authority
- Communication preferences per stakeholder: preferred channel (Slack, email, 1:1), frequency, level of detail expected
- Decision styles: how each stakeholder makes decisions (data-driven, consensus-seeking, gut-feel, etc.)
- Communication matrix: who gets what information, when, in what format
- Organizational dynamics: alliances, tensions, who influences whom, escalation paths
- Recurring feedback patterns: what Sales consistently asks for, what the CTO blocks, what the CEO cares about
- Champions and detractors per initiative: who supports what and why
- Pet peeves and green lights: what gets easy approval vs. what raises red flags
- This file helps the agent navigate organizational complexity and tailor communication
- Stakeholder management is a substantial part of product work, so preserve
  relevant context between sessions with appropriate privacy boundaries

### designSystem.md
**UX principles, patterns, and component state.**
- Design principles: the guiding philosophy for all design decisions
- Recurring UX patterns and conventions (navigation, layout, interaction patterns)
- Component library state: what exists, what's missing, what needs rework
- Platform-specific design considerations (mobile vs. desktop vs. web)
- Accessibility guidelines and current compliance state
- Design debt: known UX issues, inconsistencies between platforms, outdated patterns
- This file helps an agent produce design recommendations consistent with existing standards

### communicationGuide.md
**Product voice, UX writing, and PM communication standards.**

**Part 1 — Product Voice & UX Writing:**
- Brand personality: how the product should feel in words (professional, friendly, authoritative, etc.)
- Tone guidelines by context: errors, onboarding, empty states, success messages, marketing, notifications
- Terminology glossary: approved terms, forbidden terms, and why (multilingual if applicable)
- Do / Don't examples: concrete before/after rewrites for common patterns
- Naming conventions: how features, UI elements, and actions are named
- Localization notes: language-specific considerations, translation pitfalls

**Part 2 — PM Communication Standards:**
- **Internal register** (Slack, standups, team syncs): direct, action-oriented, bullet points preferred, use "we" not "I"
- **Technical register** (specs, tickets, engineering discussions): precise terminology, edge cases explicit, technical constraints upfront, include API/integration details where relevant
- **Executive register** (status updates, steering committees, board decks): lead with the "so what", numbers and impact first, strategic rationale clear, explicit ask (what decision is needed?)
- **Customer/user register** (in-app copy, support responses, marketing, release notes): simple language, benefits before features, concrete examples over abstractions, empathetic tone
- Style rules: vary sentence length, use contractions naturally, avoid corporate jargon ("leverage", "synergize", "cutting-edge"), lead with positive recommendations

This file ensures consistent voice across all product touchpoints AND PM communications.

### engineeringContext.md
**Technical reality that shapes product decisions.**
- High-level architecture: backend services, client platforms, infrastructure overview
- Key technical constraints impacting product decisions (protocols, encryption, federation, etc.)
- Team capacity by scope or domain: how many engineers, what bandwidth
- Known technical debt impacting product quality or velocity
- Platform-specific limitations: what's possible on each client vs. what's not
- Release process and cadence: how and when things ship
- This file bridges the gap between product ambition and engineering reality

### discoveryPlaybook.md
**How the team discovers and specifies — semi-stable.**
- **Discovery pipeline**: the stages an idea or feature request goes through, from intake to general availability. Typical stages: Intake → Triage → Accepted → Discovery → Scoping → Development → Early Access → GA (plus Rejected, Freezer)
- **Idea vs. feature request distinction**: internal ideas vs. external customer/prospect requests — both feed the same funnel but carry different context
- **Discovery frameworks** (reference, not prescription): F.O.C.U.S.E.D. (Frame, Observe, Claim, Unfold, Steal, Execute, Decide), Opportunity Solution Tree, Double Diamond, Continuous Discovery Habits, Design Sprint. The team picks what fits each situation.
- **Product Trio** principle: PM + Designer + Engineer collaborate from discovery onward, not through sequential handoffs
- **Prioritization framework**: team-specific scoring or ranking method (document here when formalized)
- **Spec/PRD template**: structure and expectations for product specifications (the `feature-*.md` template below serves as the standard)
- **Go/no-go criteria**: what conditions must be met before committing to build a feature
- **Strategic frameworks library**: list of reference frameworks the team uses
  (e.g., JTBD, PLG Iceberg, Hook-Retain-Expand, 7 Powers, Growth Loops,
  Counter-positioning, AI Product Strategy). Each framework lives as a separate
  `framework-[name].md` bank file. A trusted operator installs it outside the
  routine agent workflow with a `manage` credential, through the Admin Console
  or `uv run python scripts/mcp_cli.py bank write <space-id>
  framework-<name>.md --content-file <reviewed-file.md>`. A routine
  `read,write` agent cannot discover or call the hidden `mid_write` tool.
- This file defines the "how" of product work and changes only when the process evolves

### activeContext.md
**The most dynamic file — the entry point of every session.**
- Current focus: features in discovery, specs in progress, decisions pending
- Recently completed work (last few sessions, not full history)
- Active product decisions and trade-offs being evaluated
- Open questions and blockers
- Concrete next steps (prioritized action list)
- IMPORTANT: this file reflects the CURRENT STATE, not the full history
- Completed items must be moved to roadmapProgress.md
- This is the FIRST file an agent reads to resume work
- **Target size: < 8 KB** — beyond this, move history to roadmapProgress.md

### roadmapProgress.md
**Advancement journal and decision archive — grows over time.**
- Current quarter roadmap and planning horizon
- Delivered features with dates and retrospective: what was the impact, what did we learn
- Prioritized backlog: validated features not yet scheduled (remove items when shipped)
- **Decision log (ADR format)**:
  ```
  ### [DATE] Decision: [title]
  - **Context**: why the question arose
  - **Options considered**: A, B, C with pros/cons
  - **Decision**: what was chosen
  - **Rationale**: why
  - **Consequences**: what this implies going forward
  ```
- Key product metrics tracking (adoption, engagement, retention, NPS — whatever is monitored)
- Status indicators: ✅ Shipped | 🔄 In Progress | ⏱️ Planned | ❌ Rejected | 🧊 Frozen
- This file is the bank's designated product chronology; canonical decision and release records may live elsewhere

## Dynamic Files (created by the consolidator as needed)

### Persona Files (`persona-[name].md`)

Created when notes provide insights about a specific user persona. The consolidator must create the file on first mention and enrich it progressively over time.

**Template structure:**

```markdown
# Persona: [Role Name]

## Profile
Role, organizational context, type of organization, tech-savviness level

## Goals
Primary and secondary goals when using the product

## Pain Points
Key frustrations, challenges, and unmet needs

## Jobs to Be Done
What they hire the product to accomplish (functional, emotional, social)

## Decision Criteria
What matters when evaluating or choosing the product

## Behavioral Patterns
How they actually use (or would use) the product day-to-day

## Key Objections
Common pushback, concerns, or reasons for churn

## Effective Messages
Arguments, framings, and value propositions that resonate

## Notes
Specific observations, verbatim quotes, anecdotes from research or feedback
```

**Consolidator rules for personas:**
- Create the file as soon as a note provides persona-specific information
- Cross-persona insights belong in `userKnowledge.md`, not in individual persona files
- Never delete persona content — only enrich and refine over time
- If a note mentions conflicting behaviors between personas, document the conflict in both persona files

### Feature Files (`feature-[name].md`)

Created when a feature enters active discovery or specification. Archived to `roadmapProgress.md` when shipped, rejected, or frozen.

**Template structure:**

```markdown
# Feature: [Name]

## Current Phase
Problem Framing | Research | Ideation | Prototyping | Validation | Specification | Handed Off | Rollout | Impact Review

## Target Outcome
What business or product result we're seeking (tied to OKR or north-star metric)

## Opportunities Identified
User pain points, needs, and desires justifying this feature

## Solutions Explored
Options considered with pros/cons for each

## Assumptions to Test
Critical hypotheses and how to validate them (experiments, interviews, data analysis)

## Decisions Made
Choices and their rationale (mini-ADR: what, why, what was rejected)

## Spec / Deliverable
Link or summary of the current deliverable (PRD, prototype, wireframes)

## Success Metrics
- **Primary metric**: [name] — baseline: [current] → target: [goal] — timeline: [when]
- **Guardrail metrics** (must not harm): [metric]: [acceptable range]
- **Kill criteria**: if [condition], then [rollback / pause / iterate]

## Rollout Plan
| Phase | Audience | Duration | Pass Criteria |
|-------|----------|----------|---------------|
| Phase 1 | [who, % traffic] | [duration] | [metrics to hit] |
| Phase 2 | [expand to] | [duration] | [metrics to hit] |
| GA | [everyone] | Ongoing | [steady-state monitoring] |

## Risks & Recovery
| Risk | Detection Signal | Fallback | Kill Switch |
|------|-----------------|----------|-------------|
| [risk] | [signal, threshold] | [fallback action] | [who owns, where] |

## AI Behavior Contract (if applicable)
> Include only for features with AI/ML components.
- **Task(s)**: summarize / extract / classify / generate / route
- **Inputs**: [fields, context, tools, RAG sources]
- **Guardrails**: [brand voice, privacy rules, compliance constraints]
- **Disallowed**: [PII echo, policy violations, specific failure modes]
- **Latency budget**: P50: [X]ms / P95: [Y]ms
- **Eval plan**: offline eval ([metric, target]) → human review ([sample, quality target]) → online eval ([primary metric, duration])

## Constraints
Technical, regulatory, business, or timeline constraints shaping the solution

## Open Questions
What's still unresolved, who needs to answer, by when
```

**Consolidator rules for features:**
- Create the file when a note indicates a feature has entered discovery
- Update the "Current Phase" as the feature progresses through the extended lifecycle (Problem Framing → ... → Impact Review)
- When a feature ships or is rejected, summarize its outcome in `roadmapProgress.md` and remove the feature file (or mark it as archived)
- Maximum ~10 active feature files at a time — if more exist, consider whether some should be frozen or rejected
- Success Metrics, Rollout Plan, Risks & Recovery, and AI Behavior Contract sections can be left empty in early phases and filled as the feature matures

### Framework Files (`framework-[name].md`)

Static reference documents containing strategic frameworks used by the product
team. The consolidator never creates them. A trusted operator installs them
with a `manage` credential through the Admin Console or the CLI `bank write`
command; a routine `read,write` agent cannot discover or invoke `mid_write`.

**Purpose:** Provide persistent strategic context (e.g., JTBD canvas, PLG playbook, Growth Loops reference) that agents can read at session start to inform product decisions.

**Naming convention:** `framework-jtbd.md`, `framework-plg-iceberg.md`, `framework-hook-retain-expand.md`, `framework-7-powers.md`, etc.

**Consolidator rules for frameworks:**
- NEVER modify or delete framework files during consolidation
- Framework files are read-only reference material
- If a note references a framework, the insight goes into the appropriate domain file (e.g., `userKnowledge.md`, `marketIntelligence.md`, `discoveryPlaybook.md`) — not into the framework file itself
- The list of available frameworks should be maintained in `discoveryPlaybook.md` (Strategic Frameworks Library section)

## Note Categories and Their Product Team Usage

During work, the agent writes atomic notes via `short_note` with these categories:

- **`observation`** — Factual finding: data point, user feedback, test result, market signal, competitive move, usage metric, stakeholder comment or reaction
- **`decision`** — Product, design, or UX writing choice and its rationale: feature scope, design direction, terminology change, prioritization call, communication approach
- **`progress`** — Advancement: spec completed, prototype validated, feature shipped, research study finished, design review done
- **`issue`** — Problem identified: UX debt, inconsistency, negative user feedback, engineering blocker, competitive threat, missed target, stakeholder misalignment
- **`todo`** — Task to do: user interview to schedule, spec to write, prototype to test, design review to run, competitor to analyze, stakeholder to align
- **`insight`** — Learning: UX pattern discovered, market correlation identified, user behavior revelation, design principle validated, effective messaging found, stakeholder dynamic understood
- **`question`** — Point to clarify: trade-off to resolve, missing information, validation needed from stakeholder, open design question

## When to Update the Memory Bank

The following are candidate checkpoints for consolidation after meaningful
work, not a mandate to consolidate on every occurrence:
1. After a discovery phase milestone (e.g., problem validated, solution selected, spec approved)
2. After a product decision (roadmap change, feature scoped/rejected, pivot)
3. After user research sessions or significant feedback received
4. After a design review or UX writing review producing actionable changes
5. After a significant stakeholder interaction (alignment meeting, escalation, strategy shift)
6. At the end of a meaningful work session, after user validation or an
   explicit active instruction authorizing immediate consolidation
7. When the user explicitly requests an update

## Agent Workflow

### At Session Start (mandatory)
1. Read ALL bank files (`mid_read_all`)
2. Check completeness and consistency across files
3. Identify current focus in `activeContext.md`
4. Review active feature files to understand ongoing discovery/spec work
5. Review `stakeholders.md` to understand current organizational dynamics
6. Develop a work strategy before taking action

### During Work
1. Write frequent, atomic notes via `short_note` — one note = one fact, one decision, or one task
2. NEVER write directly to the bank — only the LLM consolidation does that.
   For a new `framework-*.md`, request the separate operator `manage` workflow;
   do not attempt to call hidden `mid_write` from a routine agent.
3. Check other agents' notes via `short_read` if working in a multi-agent setup (PM agent, Design agent, etc.)

### At Session End
1. Confirm that meaningful new notes exist. Unless the active instruction
   explicitly requires immediate consolidation, ask for or confirm user
   validation first.
2. Call `mid_consolidate` at most once.
3. Return without polling or immediately re-reading the bank. Use
   `bank_consolidation_status` only for an explicit user-requested status check.

## Instructions for the LLM Consolidator

### Mapping Note Categories to Bank Files

- `observation` (factual findings) → `activeContext.md` (recent work) + `userKnowledge.md` (if user-related) or `marketIntelligence.md` (if market-related) or `portfolio.md` (if product state) or `stakeholders.md` (if about a stakeholder's behavior, reaction, or preference) + `persona-*.md` (if persona-specific)
- `decision` (product/design choices) → `activeContext.md` (active decisions) + `feature-*.md` (if feature-related) + `roadmapProgress.md` (if strategic, add ADR entry) + `designSystem.md` or `communicationGuide.md` (if design/writing/communication standard) + `stakeholders.md` (if decision involves stakeholder alignment or is influenced by a stakeholder)
- `progress` (advancement) → `roadmapProgress.md` (journal + metrics) + `activeContext.md` (current state) + `feature-*.md` (update phase)
- `issue` (problems) → `activeContext.md` (if blocking) + `portfolio.md` (if product debt) + `engineeringContext.md` (if tech constraint) + `feature-*.md` (if feature-specific) + `stakeholders.md` (if stakeholder misalignment or organizational blocker)
- `todo` (tasks) → `activeContext.md` (next steps)
- `insight` (learnings) → `userKnowledge.md` (if user behavior) or `marketIntelligence.md` (if market) + `designSystem.md` (if UX pattern) + `communicationGuide.md` (if communication pattern or writing convention) + `persona-*.md` (if persona-specific) + `discoveryPlaybook.md` (if process learning) + `stakeholders.md` (if organizational dynamic or stakeholder pattern)
- `question` (open points) → `activeContext.md` (pending decisions) + `feature-*.md` (if feature-specific)

### Managing Dynamic Files

**Persona files:**
- If a note mentions a specific persona (by role, segment, or name), update the corresponding `persona-[name].md`
- If the persona file doesn't exist yet, create it using the template defined above
- Cross-persona information goes in `userKnowledge.md`

**Feature files:**
- If a note references a feature in active discovery/specification, update the corresponding `feature-[name].md`
- If the feature file doesn't exist yet, create it using the template defined above
- When a feature is marked as shipped, rejected, or frozen, summarize in `roadmapProgress.md` and remove the feature file
- Sections like Success Metrics, Rollout Plan, Risks & Recovery, and AI Behavior Contract may be empty in early phases — that's expected

**Framework files:**
- NEVER create, modify, or delete framework files during consolidation
- If a note references a framework, route the insight to the appropriate domain file
- Only update the frameworks list in `discoveryPlaybook.md` if a note explicitly adds or removes a framework from the team's toolkit

### Consolidation Rules

1. **Preserve material product context** — route useful insights, decisions, feedback, and market signals with provenance; explicitly clean obsolete, replaced, or duplicated data.
2. **activeContext.md is the entry point** — it's the first file read at every session start; keep it focused and current
3. **Synthesize, don't copy** — group related notes into coherent, readable paragraphs
4. **Maintain the decision log in roadmapProgress.md** — every strategic product decision gets an ADR entry with date, context, options, rationale, and consequences
5. **productVision.md is quasi-immutable** — only modify if a note fundamentally changes the product's mission, vision, or positioning
6. **Clean activeContext.md aggressively** — move completed items to roadmapProgress.md; target < 8 KB
7. **Maintain personas progressively** — incorporate verified research where relevant; correct or remove superseded, unsupported, or duplicate persona content with rationale
8. **Manage feature file lifecycle** — create on discovery entry, update throughout (including Success Metrics, Rollout Plan, Risks as the feature matures), archive on completion or rejection
9. **Respect the hierarchy** — information must live in the appropriate file per the structure defined above
10. **Keep files concise** — activeContext.md < 8 KB, other files < 15 KB. Beyond that, synthesize or archive to roadmapProgress.md
11. **Update, don't duplicate** — if a section already exists on the same topic, REPLACE it with updated content. Never create duplicate sections.
12. **Track design, writing, and communication standards** — when a note establishes a new UX pattern, add it to `designSystem.md`; when it establishes a writing convention or communication approach, add it to `communicationGuide.md`
13. **Maintain stakeholder intelligence** — when a note reveals stakeholder preferences, decision patterns, organizational dynamics, or communication feedback, update `stakeholders.md`. This file is critical for organizational navigation.
14. **Never touch framework files** — `framework-*.md` files are read-only reference material, managed outside the consolidation pipeline
