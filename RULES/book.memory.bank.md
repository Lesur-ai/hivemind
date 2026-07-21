# Book Memory Bank Rules — Hivemind Template

## Core Principle

The Memory Bank is shared editorial context that helps collaborators resume a
book project. The manuscript, source material, publisher requirements, and
human editorial decisions remain authoritative. Agents may also retain local
context; verify quotations, facts, rights, and consequential editorial choices
against their original source.

## File Structure and Hierarchy

Files build on each other in a clear hierarchy:

```
bookbrief.md (foundation)
├── bookContext.md (why this book exists, for whom)
├── narrativeDesign.md (narrative structure, voice, style)
└── writingContext.md (tools, editorial constraints, format)
    └── activeContext.md (current focus summary)
        └── progress.md (writing advancement journal)
```

- `bookbrief.md` is the foundational document that shapes all others
- `bookContext.md`, `narrativeDesign.md`, `writingContext.md` derive from it
- `activeContext.md` synthesizes the current focus from all other files
- `progress.md` tracks overall advancement and history

## Mandatory Files (6 files)

### bookbrief.md
**Book foundation — rarely modified.**
- Central thesis or premise of the book (one sentence, then expanded)
- Genre, register, and ambition (essay, novel, practical guide, narrative…)
- Scope: what the book covers AND what it does not cover
- Promise to the reader: what they will know/feel/be able to do after reading
- Structural constraints (target page count, collection, publisher, deadline)
- This file only changes if the project fundamentally pivots
- Every new agent must read this file first

### bookContext.md
**Why this book exists — the editorial context.**
- Problem or gap the book fills (why this book, why now?)
- Target readership: who the readers are, what they already know, what they expect
- Positioning: comparable or competing books, and what sets this one apart
- Expected tone for the readership (accessible popularization, provocation, dry expertise…)
- Domain vocabulary: key terms, jargon to use or avoid, living glossary
- Publication context (publisher, collection, format, target market)
- This file helps a new agent understand the "why" and the "for whom"

### activeContext.md
**The most dynamic file — the entry point of every session.**
- Current focus: which chapter or section is being written/revised
- Recently completed work (last few sessions, not the full history)
- Concrete next steps (upcoming chapters, revisions to do, pending research)
- Active editorial decisions (e.g., "deciding between two outlines for chapter 7")
- Open narrative threads to resolve, transitions to write
- Current writing problems (passage that doesn't work, detected redundancy…)
- Learnings and insights from the session (review feedback, tone adjustments)
- IMPORTANT: this file must reflect the CURRENT STATE, not the full history
- Completed items must be moved to progress.md
- This is the FIRST file an agent reads to resume work

### narrativeDesign.md
**Narrative architecture and style choices — the book's skeleton.**
- Detailed outline (structure by parts, chapters, sections) with a summary of each unit
- Overall narrative or argumentative arc: how the book progresses from point A to point Z
- Voice and tone: language register, level of familiarity, person used (first/third/we)
- Adopted style rules: sentence length, use of metaphors, level of technicality
- Recurring devices (sidebars, examples, anecdotes, quotes, exercises, illustrations)
- Key characters or figures (for narrative/fiction: character sheets; for essays: reference figures)
- Key narrative decisions and their justification (why this structure, this tone, this order)
- Planned transitions between chapters (common thread, recurring motifs)
- This file captures STRUCTURAL DECISIONS, not the written text itself

### writingContext.md
**Writing environment and practical constraints.**
- Tools used (word processor, Scrivener, Markdown, LaTeX…) and file conventions
- Typographic guidelines and editorial conventions (quotation marks, dashes, footnotes…)
- Collection or publisher standards (template, style sheet, delivery format)
- Source file organization (one file per chapter? directory structure?)
- References and sources: how they are managed (Zotero, manual bibliography, notes)
- Length constraints (words per chapter, total target) and current counter status
- Proofreading and validation process (who reviews, at what stage, what feedback to integrate)
- This file enables a new agent to produce text that meets formal expectations

### progress.md
**Advancement journal — grows over time.**
- Completed chapters (with dates and word counts)
- Chapters in progress and their status (first draft, revision 1, revision 2, validated)
- What remains to be written (chapter roadmap, section backlog)
- Overall manuscript status (green/yellow/red); include a completion percentage
  only when it is calculated from an explicit target and measured word counts
- Known problems (weak chapters, detected inconsistencies, passages to rewrite)
- Review feedback and its integration status
- Key metrics (total word count, per chapter, target vs. actual)
- Chronology of major editorial decisions (outline changes, cuts, additions)
- This file is the bank's designated editorial chronology; the manuscript and source archive remain authoritative

## Additional Context

Beyond the 6 mandatory files, additional files may be created in the bank when they help organize:
- Detailed character sheets or thematic briefs
- Timeline of events (for narrative or historical essays)
- Research and documentary notes by topic
- Excerpts or quotes to integrate (with sources)
- Correspondence and feedback from the editor or reviewers
- Book bible (universe, internal rules, factual consistency)

## When to Update the Memory Bank

The following are candidate checkpoints for consolidation after meaningful
work, not a mandate to consolidate on every occurrence:
1. After significant writing or revision of a chapter or section
2. After a structural editorial decision (outline change, tone shift, chapter cut)
3. After integrating review feedback
4. At the end of a meaningful work session, after user validation or an
   explicit active instruction authorizing immediate consolidation
5. When switching chapters or phases (drafting → revision → finalization)
6. When the user explicitly requests an update

## Recommended Agent Workflow

### At Session Start (every session)
1. Read ALL bank files (`mid_read_all`)
2. Check consistency: does the outline in `narrativeDesign.md` match the progress in `progress.md`?
3. Identify the current focus in `activeContext.md`
4. Re-read the last chapter(s) written to recapture the tone and thread

### During Work
1. Write frequent, atomic notes via `short_note`:
   - `observation`: factual findings (chapter length, source found, inconsistency spotted)
   - `decision`: editorial choices and their justification (cut a passage, reorder, change tone)
   - `todo`: identified tasks (rewrite a transition, verify a source, add an example)
   - `progress`: advancement (chapter finished, section revised, word count)
   - `issue`: writing problems (passage going in circles, redundancy with another chapter, inconsistent tone)
   - `insight`: learnings (what works well, enlightening review feedback, narrative pattern discovered)
   - `question`: points to clarify (verify a fact, choose between two approaches, request an opinion)
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
- `decision` → `activeContext.md` (active decisions) + `narrativeDesign.md` if it is a structural choice (outline, tone, style)
- `todo` → `activeContext.md` (next steps)
- `progress` → `progress.md` (completed chapters, counters) + `activeContext.md` (recent work)
- `issue` → `progress.md` (known problems) + `activeContext.md` if it is blocking
- `insight` → `activeContext.md` (learnings) + `narrativeDesign.md` if it is a style or structure pattern
- `question` → `activeContext.md` (pending decisions)

### Consolidation Rules
1. **Preserve relevant editorial information** — retain useful decisions and source references while reconciling obsolete or duplicate notes
2. **activeContext.md is the entry point** — it is the first file an agent reads at session start
3. **Synthesize, don't copy** — group similar notes into coherent, readable paragraphs
4. **Maintain chronology in progress.md** — group by chapter/part with dates and status
5. **bookbrief.md is quasi-immutable** — only modify if a note fundamentally changes the book's vision
6. **Clean activeContext.md** — move completed items to progress.md to keep the current focus lightweight
7. **Enrich, don't overwrite** — when updating, enrich existing content rather than replacing it
8. **Respect the hierarchy** — information must live in the appropriate file per the defined hierarchy
9. **Preserve the voice** — notes about tone, style, and narrative voice are precious; consolidate them carefully into `narrativeDesign.md`
10. **Track only measured word counts** — update per-chapter or total counts
    only when a cited manuscript command, report, or source note supplies the
    measurement. Otherwise retain the last sourced value as unverified or use
    `unknown`; never estimate or invent a count.
