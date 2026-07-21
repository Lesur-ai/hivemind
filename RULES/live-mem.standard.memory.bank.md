# Standard Memory Bank Rules — Hivemind Template

## Core Principle

The Memory Bank is shared working context for collaborating agents. It helps a
new session resume efficiently, but it is not the only authority: repository
files, approved project systems, and original evidence remain canonical. Agents
may also have local memory. Verify consequential facts against their canonical
source, and surface conflicts instead of silently resolving them.

## File Structure and Hierarchy

Files build on each other in a clear hierarchy:

```
projectbrief.md (foundation)
├── productContext.md (why the project exists)
├── systemPatterns.md (architecture and patterns)
└── techContext.md (tech stack and setup)
    └── activeContext.md (current focus summary)
        └── progress.md (advancement journal)
```

- `projectbrief.md` is the foundational document that shapes all others
- `productContext.md`, `systemPatterns.md`, `techContext.md` derive from it
- `activeContext.md` synthesizes the current focus from all other files
- `progress.md` tracks overall advancement and history

## Mandatory Files (6 files)

### projectbrief.md
**Project foundation — rarely modified.**
- Fundamental vision and objectives of the project
- Explicit scope and boundaries
- Key requirements and structural constraints
- Stakeholders and owner
- Shared summary of project scope; verify against canonical project documents
- This file only changes if the project fundamentally pivots
- Every new agent must read this file first

### productContext.md
**Why this project exists — the product context.**
- Concrete problems the project solves
- How the product works (main flow, key concepts)
- Domain terminology and vocabulary
- User experience goals (UX goals)
- Positioning relative to existing alternatives
- This file helps a new agent understand the "why" and the "how"

### activeContext.md
**The most dynamic file — the entry point of every session.**
- Current focus: what is being worked on right now
- Recently completed work (last few sessions, not the full history)
- Concrete next steps (prioritized todo list)
- Active decisions and ongoing considerations
- Important patterns and preferences recently discovered
- Learnings and insights from the session
- IMPORTANT: this file must reflect the CURRENT STATE, not the full history
- Completed items must be moved to progress.md
- This is the FIRST file an agent reads to resume work
- **Target size: < 8 KB** — beyond this, it signals inflation; move history to progress.md

### systemPatterns.md
**Architecture and technical patterns of the project.**
- Overall system architecture (with text diagrams if relevant)
- Key technical decisions and their justification (why this choice)
- Design patterns used and conventions
- Relationships and dependencies between components
- Critical implementation paths
- Code conventions, standards, and best practices
- This file captures STRUCTURAL DECISIONS, not implementation details
- **When a pattern evolves** (e.g., architecture migration), REPLACE the existing section — do not keep the old version

### techContext.md
**Tech stack and development environment.**
- Technologies used with versions and roles
- Development setup (step-by-step, commands)
- Known technical constraints and workarounds
- Dependencies and their management
- Source file structure (annotated tree)
- Tool usage patterns (CLI, Docker, tests)
- This file enables a new agent to set up their environment

### progress.md
**Advancement journal — grows over time.**
- What works (by version or milestone), with dates
- What remains to be built (roadmap, backlog) — **remove completed items**
- Overall project status (green/yellow/red)
- Known problems and documented workarounds
- Key metrics (lines of code, tests, coverage, MCP tools) only when a cited
  command, report, or note supplies the measurement; otherwise preserve the
  last sourced value as unverified or record `unknown` — never estimate it
- Chronological evolution of project decisions
- This file is the bank's designated chronological summary; canonical history may live elsewhere

## Additional Context

Beyond the 6 mandatory files, additional files may be created in the bank when they help organize:
- Complex feature documentation
- Integration specifications
- API documentation
- Test strategies
- Deployment procedures

## When to Update the Memory Bank

The following are candidate checkpoints for consolidation after meaningful
work, not a mandate to consolidate on every occurrence:
1. After discovering new project patterns or conventions
2. After implementing significant changes
3. When the context needs clarification
4. At the end of a meaningful work session, after user validation or an
   explicit active instruction authorizing immediate consolidation
5. Before a major topic change
6. When the user explicitly requests an update

## Recommended Agent Workflow

### At Session Start (every session)
1. Read ALL bank files (`mid_read_all`)
2. Verify that files are complete and consistent
3. Identify the current focus in `activeContext.md`
4. Develop a work strategy

### During Work
1. Write frequent, atomic notes via `short_note`:
   - `observation`: factual findings, command outputs
   - `decision`: technical choices and their justification
   - `todo`: identified tasks to do
   - `progress`: advancement, what is completed
   - `issue`: problems encountered, bugs
   - `insight`: learnings, patterns discovered
   - `question`: points to clarify, pending decisions
2. NEVER write directly to the bank — only the LLM consolidation does that
3. Check other agents' notes via `short_read` if working in a multi-agent setup

### At Session End
1. Confirm that meaningful new notes exist. Unless the active instruction
   explicitly requires immediate consolidation, ask for or confirm user
   validation first.
2. Call `mid_consolidate` at most once.
3. Return without polling or immediately re-reading the bank. Use
   `bank_consolidation_status` only for an explicit user-requested status check.

## Instructions for the LLM Consolidator

### Mapping Note Categories to Bank Files
- `observation` → `activeContext.md` (recent work) + relevant file depending on the topic
- `decision` → `activeContext.md` (active decisions) + `systemPatterns.md` if architectural
- `todo` → `activeContext.md` (next steps)
- `progress` → `progress.md` (what works) + `activeContext.md` (recent work)
- `issue` → `progress.md` (known problems) + `activeContext.md` if blocking
- `insight` → `activeContext.md` (learnings) + `systemPatterns.md` if it is a pattern
- `question` → `activeContext.md` (pending decisions)

### Consolidation Rules
1. **Preserve relevant durable information** — route useful facts into the bank, keep source references, and clean obsolete, replaced, or duplicated data explicitly.
2. **activeContext.md is the entry point** — it is the first file an agent reads at session start
3. **Never invent metrics** — update counts, coverage, versions, dates, or
   status figures only from an explicit source note, command, or report; retain
   the previous value as unverified or use `unknown` when no measurement exists
4. **Synthesize, don't copy** — group similar notes into coherent, readable paragraphs
5. **Maintain chronology in progress.md** — group by version/milestone with dates
6. **projectbrief.md is quasi-immutable** — only modify if a note fundamentally changes the project's vision
7. **Clean activeContext.md** — move completed items to progress.md to keep the current focus lightweight
8. **Update, don't duplicate** — if a section already exists on the same topic, REPLACE it with updated content. Never create duplicate sections.
9. **Respect the hierarchy** — information must live in the appropriate file per the defined hierarchy
10. **Clean up obsolete content** — remove completed items from backlogs ("What Remains to Be Built"), update metrics when they change, delete sections superseded by newer versions
11. **Keep files concise** — activeContext.md < 8 KB, other files < 15 KB. Beyond that, synthesize or archive to progress.md
