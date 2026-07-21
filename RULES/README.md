# RULES — Hivemind Mid-Memory Templates

This directory contains templates for creating a Hivemind space with
`space_create`. Rules define the desired mid-memory Markdown structure and guide
how the LLM consolidator organizes short notes. The same `space_id` also owns
the derived, non-authoritative long tier.

## Authority and Safety

The mid-memory bank is shared working context, not the only source of truth.
Repository files, signed records, source documents, approved business systems,
and domain-specific systems of record remain authoritative. Consolidation is an
LLM transformation: it may omit, summarize, misclassify, or misunderstand
content. Verify important facts against their canonical source before acting.

Rules are inserted into the configured LLM request during `mid_consolidate`.
Treat them as executable prompt instructions: do not place secrets, credentials,
personal data, or regulated data in a template unless the complete storage and
LLM-processing path has been approved for that data.

## Are Rules Mutable?

Rules are normally kept stable after creation so repeated consolidations follow
one structure. They are not technically immutable: a caller with target-space
access and `manage` permission can replace them with `space_update_rules` (CLI:
`space update-rules`). Treat this as an operator migration. Review the new
template, preserve a backup, and validate the next consolidation before relying
on the updated bank.

## Available Templates

| File | Domain | Purpose |
| --- | --- | --- |
| `live-mem.standard.memory.bank.md` | General | Six-file software/project workspace: `projectbrief.md`, `productContext.md`, `activeContext.md`, `systemPatterns.md`, `techContext.md`, and `progress.md`. |
| `book.memory.bank.md` | Writing | Book planning and editorial continuity with source-aware research, narrative design, active context, and progress. |
| `medical.memory.bank.md` | Non-clinical demonstration | Synthetic or appropriately de-identified health-note workflow for evaluating organization and human verification. It is not a clinical record or decision-support template and must not be used for patient care. |
| `presales.memory.bank.md` | Presales | Proposal analysis, personas, contradictions, reusable argument patterns, and progress tracking. |
| `product.management.memory.bank.md` | Product management | Product vision, portfolio, research, design, engineering context, discovery, stakeholder communication, features, and roadmap decisions. |

## Create a Space

Use the exact template path and the CLI's named description option:

```bash
uv run python scripts/mcp_cli.py space create my-project \
  --description "My project" \
  --rules-file RULES/live-mem.standard.memory.bank.md
```

The equivalent MCP call passes the file contents in `rules`:

```python
space_create(
    space_id="my-project",
    description="My project",
    rules=rules_markdown,
)
```

## Update Existing Rules

`space_update_rules` requires target-space access plus `manage` permission:

```bash
uv run python scripts/mcp_cli.py space update-rules my-project \
  --rules-file RULES/live-mem.standard.memory.bank.md
```

This replaces `_rules.md`; it does not automatically rewrite or validate
existing mid-memory files. Review the resulting bank after consolidation.

## Create a Template

Define:

- the desired files, their roles, and size/lifecycle expectations;
- how note categories map to those files;
- which external sources are authoritative and how conflicts are surfaced;
- what the consolidator may summarize, replace, or remove;
- human-verification and data-handling requirements for the domain.

A good template gives the consolidator precise structure without claiming
perfect recall or replacing the project's canonical evidence.
