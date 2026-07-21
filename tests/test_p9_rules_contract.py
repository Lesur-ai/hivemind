"""Public P9 guards for Hivemind rules templates and the non-clinical demo."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = ROOT / "RULES"
TEMPLATES = (
    "book.memory.bank.md",
    "live-mem.standard.memory.bank.md",
    "medical.memory.bank.md",
    "presales.memory.bank.md",
    "product.management.memory.bank.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_templates_use_hivemind_identity_and_bounded_authority() -> None:
    forbidden = (
        "live memory v",
        "only source of truth",
        "single source of truth",
        "starts from zero",
        "depends entirely",
    )

    for filename in TEMPLATES:
        text = _read(RULES_DIR / filename)
        first_line = text.splitlines()[0]
        lowered = text.lower()
        assert "Hivemind" in first_line, filename
        for phrase in forbidden:
            assert phrase not in lowered, f"{filename}: forbidden absolute claim {phrase!r}"


def test_rules_readmes_match_real_mutability_cli_and_paths() -> None:
    for filename in ("README.md", "README.fr.md"):
        text = _read(RULES_DIR / filename)
        assert "space_update_rules" in text
        assert "`manage`" in text
        assert "space update-rules" in text
        assert "--description" in text
        assert "--rules-file RULES/live-mem.standard.memory.bank.md" in text
        assert "RULES/standard.memory.bank.md" not in text


def test_medical_template_is_strictly_non_clinical_and_human_verified() -> None:
    text = _read(RULES_DIR / "medical.memory.bank.md")
    flat_text = " ".join(text.split())

    required = (
        "Non-Clinical Health Notes",
        "synthetic or appropriately de-identified",
        "external clinical record",
        "qualified human must verify",
        "configured LLM",
        "Medical emergencies are outside Hivemind",
        "local emergency services",
        "never a clinical decision",
        "never a health correlation",
        "Never generate thresholds, alerts, diagnoses, treatment suggestions",
        "long/graph memory non-authoritative",
    )
    for phrase in required:
        assert phrase in flat_text

    forbidden = (
        "personalized alert thresholds",
        "flag alerts",
        "immediate actions to take",
        "therapeutic decisions currently",
        "perfect fidelity",
        "no data loss",
        "patientProfile.md",
        "emergencyProtocol.md",
    )
    lowered = flat_text.lower()
    for phrase in forbidden:
        assert phrase.lower() not in lowered


def test_agent_workflow_templates_follow_async_validated_consolidation() -> None:
    for filename in (
        "book.memory.bank.md",
        "live-mem.standard.memory.bank.md",
        "presales.memory.bank.md",
        "product.management.memory.bank.md",
    ):
        text = _read(RULES_DIR / filename)
        assert "meaningful new notes" in text, filename
        assert "user validation" in text, filename
        assert "Call `mid_consolidate` at most once" in text, filename
        assert "without polling or immediately re-reading the bank" in text, filename
        assert "At the end of every work session (always)" not in text, filename
        assert "Verify the bank reflects" not in text, filename


def test_rules_never_invent_metrics_or_offer_hidden_mid_write_to_agents() -> None:
    standard = _read(RULES_DIR / "live-mem.standard.memory.bank.md")
    book = _read(RULES_DIR / "book.memory.bank.md")
    product = _read(RULES_DIR / "product.management.memory.bank.md")

    assert "Never invent metrics" in standard
    assert "never estimate it" in standard
    assert "Track only measured word counts" in book
    assert "never estimate or invent a count" in book
    assert "calculated from an explicit target and measured word counts" in book
    assert "~50%" not in product
    assert "routine `read,write` agent cannot discover or invoke `mid_write`" in product
    assert "scripts/mcp_cli.py bank write" in product
    assert "via `mid_write` by an admin or agent" not in product
