"""Empty standard MID documents, with no inferred project facts."""

# Pin the rules content, not its operator-configurable path. The bootstrap test
# verifies this fingerprint against the shipped file whenever the rules change.
STANDARD_RULES_SHA256 = "3cf367527d68b6df1337e1bda0115332d7f4aa848e406e051fbfa841a0197013"

CANONICAL_BANK_TITLES = {
    "activeContext.md": "Active Context",
    "systemPatterns.md": "System Patterns",
    "techContext.md": "Tech Context",
    "productContext.md": "Product Context",
    "projectbrief.md": "Project Brief",
    "progress.md": "Progress",
}

STANDARD_BANK = {
    "projectbrief.md": "# Project Brief\n\n## Goals\n\n## Scope\n",
    "productContext.md": "# Product Context\n\n## Purpose\n\n## Workflow\n",
    "systemPatterns.md": "# System Patterns\n\n## Architecture\n\n## Decisions\n",
    "techContext.md": "# Tech Context\n\n## Stack\n\n## Constraints\n",
    "activeContext.md": "# Active Context\n\n## Current Focus\n\n## Next Steps\n",
    "progress.md": "# Progress\n\n## History\n",
}
