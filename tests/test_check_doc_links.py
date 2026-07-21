from __future__ import annotations

import ast
from pathlib import Path
import sys
import textwrap

import pytest

from scripts import check_doc_links


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def _assert_clean(root: Path) -> check_doc_links.CheckReport:
    report = check_doc_links.audit_repository(root, allowlist=set())
    assert not report.errors, "\n".join(report.errors)
    return report


def test_dynamic_public_inventory_catches_document_omitted_by_old_static_list(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "README.md", "# Home\n")
    _write(tmp_path, ".git/hidden.md", "[Never scanned](missing.md)\n")
    late_document = _write(
        tmp_path,
        "docs/late-addition.md",
        """
        # Late addition

        [Home](../README.md#home)
        """,
    )
    baseline = _assert_clean(tmp_path)
    assert "docs/late-addition.md" in baseline.tree.surfaces
    assert ".git/hidden.md" not in baseline.tree.surfaces

    late_document.write_text(
        "# Late addition\n\n[Missing](not-exported.md)\n", encoding="utf-8"
    )
    mutated = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert any(
        "docs/late-addition.md" in error and "not-exported.md" in error
        for error in mutated.errors
    )


@pytest.mark.parametrize(
    ("baseline_link", "mutated_link"),
    [
        ("[Local](#home)", "[Local](#absent)"),
        ("[Cross-file](guide.md#install)", "[Cross-file](guide.md#absent)"),
        ("[Explicit](guide.md#manual-step)", "[Explicit](guide.md#missing-id)"),
    ],
)
def test_markdown_anchor_mutations_fail_closed(
    tmp_path: Path,
    baseline_link: str,
    mutated_link: str,
) -> None:
    readme = _write(tmp_path, "README.md", f"# Home\n\n{baseline_link}\n")
    _write(
        tmp_path,
        "guide.md",
        """
        # Install

        <a id="manual-step"></a>
        Manual details.
        """,
    )
    _assert_clean(tmp_path)

    readme.write_text(f"# Home\n\n{mutated_link}\n", encoding="utf-8")
    mutated = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert any("missing Markdown anchor" in error for error in mutated.errors)


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("📦 Prerequisites", "-prerequisites"),
        ("Compatibility & deprecation", "compatibility--deprecation"),
        ("👥 Multi-agent: Cline + Claude + Others", "-multi-agent-cline--claude--others"),
    ],
)
def test_github_slug_examples_match_rendered_anchor_contract(
    heading: str, slug: str
) -> None:
    assert check_doc_links._github_slug(heading) == slug


def test_github_actions_badge_and_action_url_require_exported_workflow(
    tmp_path: Path,
) -> None:
    readme = _write(
        tmp_path,
        "README.md",
        "# Home\n\n"
        "[![CI](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml/"
        "badge.svg)](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml)\n",
    )
    _write(tmp_path, ".github/workflows/ci.yml", "name: CI\n")
    _assert_clean(tmp_path)

    readme.write_text(
        readme.read_text(encoding="utf-8").replace("ci.yml", "missing.yml"),
        encoding="utf-8",
    )
    mutated = check_doc_links.audit_repository(tmp_path, allowlist=set())
    workflow_errors = [
        error for error in mutated.errors if "absent public workflow" in error
    ]
    assert len(workflow_errors) == 2
    assert all(".github/workflows/missing.yml" in error for error in workflow_errors)


def test_external_repository_workflow_url_is_not_treated_as_local(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        """
        # Home

        [Upstream CI](https://github.com/example/upstream/actions/workflows/ci.yml)
        """,
    )
    _assert_clean(tmp_path)


def test_release_policy_resolves_overlay_from_public_destination_and_hides_private_files(
    tmp_path: Path,
) -> None:
    overlay_readme = _write(
        tmp_path,
        "release/public-overlay/README.md",
        "# Public home\n\n[Support](SUPPORT.md#support)\n",
    )
    _write(
        tmp_path,
        "release/public-overlay/SUPPORT.md",
        "# Support\n",
    )
    _write(tmp_path, "private-only.md", "# Private only\n")
    _write(
        tmp_path,
        "release/export.toml",
        """
        schema_version = 1

        [[rules]]
        source = "private-only.md"
        action = "exclude"

        [[rules]]
        source = "release/public-overlay/README.md"
        action = "map"
        destination = "README.md"

        [[rules]]
        source = "release/public-overlay/SUPPORT.md"
        action = "map"
        destination = "SUPPORT.md"
        """,
    )

    baseline = _assert_clean(tmp_path)
    assert baseline.tree.mode == "private-policy"
    assert baseline.tree.files["README.md"] == overlay_readme
    assert "release/public-overlay/README.md" not in baseline.tree.surfaces

    overlay_readme.write_text(
        "# Public home\n\n[Private](private-only.md)\n", encoding="utf-8"
    )
    mutated = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert any(
        "private-only.md" in error and "dangling public link target" in error
        for error in mutated.errors
    )


@pytest.mark.parametrize(
    ("command", "existing_path", "missing_path"),
    [
        (
            "python scripts/example.py --check",
            "scripts/example.py",
            "scripts/missing.py",
        ),
        (
            "tool space create demo --rules-file RULES/example.md",
            "RULES/example.md",
            "RULES/missing.md",
        ),
    ],
)
def test_safe_fenced_command_path_mutations_fail_closed(
    tmp_path: Path,
    command: str,
    existing_path: str,
    missing_path: str,
) -> None:
    readme = _write(
        tmp_path,
        "README.md",
        f"""
        # Home

        ```bash
        {command}
        ```
        """,
    )
    _write(tmp_path, existing_path, "# exported fixture\n")
    _assert_clean(tmp_path)

    readme.write_text(
        readme.read_text(encoding="utf-8").replace(existing_path, missing_path),
        encoding="utf-8",
    )
    mutated = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert any(
        "fenced" in error and missing_path in error for error in mutated.errors
    )


def test_fenced_command_path_cannot_traverse_an_exported_parent(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        """
        # Home

        ```bash
        python scripts/../private.py
        ```
        """,
    )
    _write(tmp_path, "private.py", "# present but not a safe scripts path\n")

    report = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert any("unsafe or absent" in error for error in report.errors)


@pytest.mark.parametrize("configured_source", ["README.md", "scripts/tool.py"])
def test_missing_configured_copy_source_is_an_error(
    tmp_path: Path, configured_source: str
) -> None:
    _write(
        tmp_path,
        "release/export.toml",
        f"""
        schema_version = 1

        [[rules]]
        source = "{configured_source}"
        action = "copy"
        """,
    )

    report = check_doc_links.audit_repository(tmp_path, allowlist=set())
    if configured_source.endswith(".md"):
        assert configured_source in report.tree.surfaces
    assert any("configured source is absent" in error for error in report.errors)


def test_release_policy_discovery_fails_closed_on_ambiguous_rule_files(
    tmp_path: Path,
) -> None:
    for name in ("first.toml", "second.toml"):
        _write(
            tmp_path,
            f"release/{name}",
            """
            schema_version = 1
            rules = []
            """,
        )

    report = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert report.tree.mode == "release-metadata-error"
    assert any("multiple release metadata files" in error for error in report.errors)


def test_release_policy_discovery_fails_closed_on_invalid_toml(tmp_path: Path) -> None:
    _write(tmp_path, "release/broken.toml", "rules = [\n")

    report = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert report.tree.mode == "release-metadata-error"
    assert any("cannot inspect release metadata" in error for error in report.errors)


def test_absent_public_tree_root_fails_closed(tmp_path: Path) -> None:
    report = check_doc_links.audit_repository(
        tmp_path / "absent-public-tree", allowlist=set()
    )
    assert any("public tree root is absent" in error for error in report.errors)


def test_temporary_allowlist_still_applies_to_resolved_public_destinations(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "README.md", "# Home\n\n[Draft](docs/draft.md)\n")

    blocked = check_doc_links.audit_repository(tmp_path, allowlist=set())
    assert any("docs/draft.md" in error for error in blocked.errors)
    allowed = check_doc_links.audit_repository(
        tmp_path, allowlist={"docs/draft.md"}
    )
    assert not allowed.errors


def test_checker_imports_only_python_standard_library() -> None:
    source_path = Path(check_doc_links.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= set(sys.stdlib_module_names) | {"__future__"}
