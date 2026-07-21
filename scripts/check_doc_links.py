#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed consistency checks for every exported Markdown document.

In a source repository, the checker discovers the release TOML whose ``rules``
define the public namespace: destinations are the visible paths and
``copy``/``map`` sources provide their bytes. In an already-exported public
tree, where that release metadata is intentionally absent, every ``*.md``
outside ``.git`` is discovered directly.

The sweep validates relative targets, Markdown fragments, local GitHub Actions
workflow URLs, and safe repository paths used by Python/``--rules-file``
commands in fenced examples.  It deliberately uses only the Python standard
library so the same gate can run before project dependencies are installed.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import os
import posixpath
import re
import stat
import sys
import tomllib
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]

_LINK_RE = re.compile(
    r"(?<![\\!])\[(?P<text>[^\]\n]*)\]"
    r"\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)
_IMAGE_RE = re.compile(
    r"(?<!\\)!\[(?P<text>[^\]\n]*)\]"
    r"\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)
_LINKED_IMAGE_RE = re.compile(
    r"(?<!\\)\[!\[[^\]\n]*\]\([^)\n]+\)\]"
    r"\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)
_REF_RE = re.compile(r"^\s*\[(?P<id>[^\]]+)\]:\s*(?P<target>\S+)")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+|$)(?P<text>.*)$")
_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_EXPLICIT_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\b(?:id|name)\s*=\s*(?:\"(?P<double>[^\"]+)\"|"
    r"'(?P<single>[^']+)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_PYTHON_SCRIPT_RE = re.compile(
    r"(?<![\w./-])python(?:3(?:\.\d+)*)?\s+"
    r"(?P<path>(?:\./)?scripts/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py)"
    r"(?![A-Za-z0-9_.-])"
)
_RULES_FILE_RE = re.compile(
    r"(?<![\w-])--rules-file(?:=|\s+)"
    r"(?:\"(?P<double>[A-Za-z0-9_./-]+)\"|'(?P<single>[A-Za-z0-9_./-]+)'|"
    r"(?P<bare>[A-Za-z0-9_./-]+))"
)


@dataclass(frozen=True)
class PublicTree:
    """A destination-to-source view of the repository users receive."""

    root: Path
    files: dict[str, Path]
    available_files: frozenset[str]
    directories: frozenset[str]
    surfaces: tuple[str, ...]
    configuration_errors: tuple[str, ...]
    mode: str

    def is_file(self, destination: str) -> bool:
        return destination in self.available_files

    def exists(self, destination: str) -> bool:
        return destination in self.available_files or destination in self.directories

    def read_text(self, destination: str) -> str:
        return self.files[destination].read_text(encoding="utf-8")


@dataclass(frozen=True)
class CheckReport:
    tree: PublicTree
    rows: tuple[tuple[str, int], ...]
    errors: tuple[str, ...]


def _policy_rel_path(
    value: object, *, field: str, rule_number: int
) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, f"policy rule {rule_number}: {field} must be a non-empty string"
    if "\\" in value:
        return None, f"policy rule {rule_number}: {field} must use POSIX separators: {value!r}"
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None, f"policy rule {rule_number}: unsafe {field}: {value!r}"
    normalised = pure.as_posix()
    if normalised != value:
        return None, f"policy rule {rule_number}: non-canonical {field}: {value!r}"
    return normalised, None


def _directories_for(files: Iterable[str]) -> frozenset[str]:
    directories: set[str] = {""}
    for rel_path in files:
        parent = PurePosixPath(rel_path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return frozenset(directories)


def _build_policy_tree(root: Path, policy_path: Path) -> PublicTree:
    errors: list[str] = []
    policy_label = policy_path.relative_to(root).as_posix()
    try:
        policy = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return PublicTree(
            root=root,
            files={},
            available_files=frozenset(),
            directories=frozenset({""}),
            surfaces=(),
            configuration_errors=(f"{policy_label}: cannot load policy: {exc}",),
            mode="private-policy",
        )

    rules = policy.get("rules")
    if not isinstance(rules, list):
        return PublicTree(
            root=root,
            files={},
            available_files=frozenset(),
            directories=frozenset({""}),
            surfaces=(),
            configuration_errors=(f"{policy_label}: 'rules' must be an array",),
            mode="private-policy",
        )

    files: dict[str, Path] = {}
    available: set[str] = set()
    surfaces: set[str] = set()
    resolved_root = root.resolve()

    for rule_number, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            errors.append(f"policy rule {rule_number}: rule must be a table")
            continue
        action = rule.get("action")
        if action not in {"copy", "map"}:
            continue

        source, source_error = _policy_rel_path(
            rule.get("source"), field="source", rule_number=rule_number
        )
        if source_error:
            errors.append(source_error)
            continue
        assert source is not None

        destination_value = rule.get("destination", source)
        if action == "map" and "destination" not in rule:
            errors.append(f"policy rule {rule_number}: map action requires destination")
            continue
        destination, destination_error = _policy_rel_path(
            destination_value, field="destination", rule_number=rule_number
        )
        if destination_error:
            errors.append(destination_error)
            continue
        assert destination is not None

        if destination in files:
            errors.append(
                f"policy rule {rule_number}: duplicate public destination {destination!r}"
            )
            continue

        source_path = root / source
        files[destination] = source_path
        if destination.endswith(".md"):
            surfaces.add(destination)

        try:
            source_inside_root = source_path.resolve().is_relative_to(resolved_root)
        except OSError:
            source_inside_root = False
        if not source_inside_root or not source_path.is_file():
            errors.append(
                f"policy rule {rule_number}: configured source is absent or unsafe: {source!r}"
            )
            continue
        available.add(destination)

    return PublicTree(
        root=root,
        files=files,
        available_files=frozenset(available),
        directories=_directories_for(available),
        surfaces=tuple(sorted(surfaces)),
        configuration_errors=tuple(errors),
        mode="private-policy",
    )


def _build_public_tree(root: Path) -> PublicTree:
    files: dict[str, Path] = {}
    errors: list[str] = []
    if not root.is_dir():
        errors.append(f"public tree root is absent or not a directory: {root}")

    def record_walk_error(error: OSError) -> None:
        errors.append(f"cannot scan public tree: {error}")

    for current, directory_names, file_names in os.walk(root, onerror=record_walk_error):
        directory_names[:] = [name for name in directory_names if name != ".git"]
        current_path = Path(current)
        for file_name in file_names:
            path = current_path / file_name
            try:
                is_regular_file = stat.S_ISREG(path.stat().st_mode)
            except OSError as exc:
                errors.append(f"cannot inspect public-tree path {path}: {exc}")
                continue
            if is_regular_file:
                files[path.relative_to(root).as_posix()] = path
    available = frozenset(files)
    return PublicTree(
        root=root,
        files=files,
        available_files=available,
        directories=_directories_for(available),
        surfaces=tuple(sorted(path for path in available if path.endswith(".md"))),
        configuration_errors=tuple(errors),
        mode="public-scan",
    )


def _discover_release_policy(root: Path) -> tuple[Path | None, tuple[str, ...]]:
    """Find one release TOML with an export-style ``rules`` array.

    Discovery avoids coupling the public checker to private release filenames.
    Any unreadable TOML or ambiguous rule-bearing candidate fails closed.
    """

    release_dir = root / "release"
    if not release_dir.is_dir():
        return None, ()

    candidates: list[Path] = []
    errors: list[str] = []
    for candidate in sorted(release_dir.glob("*.toml")):
        label = candidate.relative_to(root).as_posix()
        try:
            candidate_is_safe = (
                not candidate.is_symlink()
                and candidate.resolve().is_relative_to(root)
                and candidate.is_file()
            )
        except OSError:
            candidate_is_safe = False
        if not candidate_is_safe:
            errors.append(f"release metadata path is absent or unsafe: {label}")
            continue
        try:
            document = tomllib.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"cannot inspect release metadata {label}: {exc}")
            continue
        if isinstance(document.get("rules"), list):
            candidates.append(candidate)

    if len(candidates) > 1:
        labels = ", ".join(path.relative_to(root).as_posix() for path in candidates)
        errors.append(f"multiple release metadata files define export rules: {labels}")
    if errors:
        return None, tuple(errors)
    return (candidates[0] if candidates else None), ()


def build_public_tree(root: Path = REPO_ROOT) -> PublicTree:
    """Build the exact public namespace used by all checks."""

    resolved_root = root.resolve()
    policy_path, discovery_errors = _discover_release_policy(resolved_root)
    if discovery_errors:
        return PublicTree(
            root=resolved_root,
            files={},
            available_files=frozenset(),
            directories=frozenset({""}),
            surfaces=(),
            configuration_errors=discovery_errors,
            mode="release-metadata-error",
        )
    if policy_path is not None:
        return _build_policy_tree(resolved_root, policy_path)
    return _build_public_tree(resolved_root)


def _load_allowlist(root: Path = REPO_ROOT) -> set[str]:
    allowed: set[str] = set()
    env_value = os.environ.get("DOC_LINK_ALLOWLIST", "")
    if env_value:
        allowed.update(entry.strip() for entry in env_value.split(":") if entry.strip())
    allowlist_file = root / "scripts" / "doc_link_allowlist.txt"
    if allowlist_file.is_file():
        for raw in allowlist_file.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                allowed.add(line)
    return allowed


def _fenced_line_states(text: str) -> Iterable[tuple[int, str, bool]]:
    fence_char: str | None = None
    fence_length = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if fence_char is not None:
            is_closing = bool(
                re.match(
                    rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*$",
                    line,
                )
            )
            yield line_no, line, True
            if is_closing:
                fence_char = None
                fence_length = 0
            continue

        opening = _FENCE_OPEN_RE.match(line)
        if opening:
            marker = opening.group("marker")
            fence_char = marker[0]
            fence_length = len(marker)
            yield line_no, line, True
        else:
            yield line_no, line, False


def _clean_target(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    return target


def _iter_links(text: str) -> Iterable[tuple[int, str]]:
    for line_no, line, in_fence in _fenced_line_states(text):
        if in_fence:
            continue
        targets: list[str] = []
        for pattern in (_LINK_RE, _IMAGE_RE, _LINKED_IMAGE_RE):
            targets.extend(match.group("target") for match in pattern.finditer(line))
        reference = _REF_RE.match(line)
        if reference:
            targets.append(reference.group("target"))
        seen: set[str] = set()
        for target in targets:
            cleaned = _clean_target(target)
            if cleaned not in seen:
                seen.add(cleaned)
                yield line_no, cleaned


def _heading_text_for_slug(text: str) -> str:
    value = html.unescape(text)
    value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("`", "").replace("*", "").replace("~", "")
    return value


def _github_slug(text: str) -> str:
    kept: list[str] = []
    for character in _heading_text_for_slug(text).strip().lower():
        category = unicodedata.category(character)
        codepoint = ord(character)
        if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
            continue
        if character in {"-", "_"} or category[0] in {"L", "M", "N"}:
            kept.append(character)
        elif character.isspace():
            kept.append(" ")
    # GitHub replaces each whitespace character independently.  Removing an
    # emoji or punctuation between two spaces therefore intentionally leaves
    # leading/doubled hyphens (for example ``📦 Prerequisites`` becomes
    # ``-prerequisites`` and ``Compatibility & deprecation`` becomes
    # ``compatibility--deprecation``).
    return re.sub(r"\s", "-", "".join(kept))


def _anchors_for_markdown(text: str) -> frozenset[str]:
    anchors: set[str] = set()
    used_heading_slugs: set[str] = set()
    previous_line: str | None = None

    def add_heading(raw_heading: str) -> None:
        base = _github_slug(raw_heading)
        if not base:
            return
        candidate = base
        suffix = 0
        while candidate in used_heading_slugs:
            suffix += 1
            candidate = f"{base}-{suffix}"
        used_heading_slugs.add(candidate)
        anchors.add(candidate)

    for _line_no, line, in_fence in _fenced_line_states(text):
        if in_fence:
            previous_line = None
            continue
        for match in _EXPLICIT_ANCHOR_RE.finditer(line):
            anchors.add(
                html.unescape(
                    match.group("double")
                    or match.group("single")
                    or match.group("bare")
                )
            )

        atx = _ATX_HEADING_RE.match(line)
        if atx:
            heading = re.sub(r"[ \t]+#+[ \t]*$", "", atx.group("text"))
            add_heading(heading)
            previous_line = None
            continue
        if previous_line is not None and _SETEXT_RE.match(line):
            add_heading(previous_line.strip())
            previous_line = None
            continue
        previous_line = line if line.strip() else None
    return frozenset(anchors)


def _workflow_destination(target: str) -> str | None:
    parsed = urlsplit(target)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "github.com":
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 5 or tuple(part.lower() for part in parts[:2]) != (
        "lesur-ai",
        "hivemind",
    ):
        return None
    if parts[2:4] != ["actions", "workflows"]:
        return None
    workflow = parts[4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow):
        return None
    return f".github/workflows/{workflow}"


def _resolve_relative(document: str, raw_path: str) -> str | None:
    decoded = unquote(raw_path)
    if not decoded:
        return document
    if decoded.startswith("/") or "\\" in decoded or "\x00" in decoded:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document), decoded))
    if resolved == ".." or resolved.startswith("../"):
        return None
    return resolved


def _root_reference(raw_path: str) -> str | None:
    path = raw_path[2:] if raw_path.startswith("./") else raw_path
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    pure = PurePosixPath(path)
    if (
        any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        return None
    return pure.as_posix()


def _iter_fenced_paths(text: str) -> Iterable[tuple[int, str, str]]:
    for line_no, line, in_fence in _fenced_line_states(text):
        if not in_fence or _FENCE_OPEN_RE.match(line):
            continue
        for match in _PYTHON_SCRIPT_RE.finditer(line):
            yield line_no, "python script", match.group("path")
        for match in _RULES_FILE_RE.finditer(line):
            yield line_no, "--rules-file", (
                match.group("double") or match.group("single") or match.group("bare")
            )


def check_file(
    rel_path: str,
    allowlist: set[str],
    *,
    tree: PublicTree | None = None,
    texts: dict[str, str] | None = None,
    anchors: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    """Return actionable public-tree errors for one Markdown destination."""

    public_tree = tree or build_public_tree()
    loaded_texts = texts if texts is not None else {}
    loaded_anchors = anchors if anchors is not None else {}
    if not public_tree.is_file(rel_path):
        return [f"{rel_path}: configured Markdown source is absent from the public tree"]

    try:
        text = loaded_texts.setdefault(rel_path, public_tree.read_text(rel_path))
    except (OSError, UnicodeError) as exc:
        return [f"{rel_path}: cannot read exported Markdown as UTF-8: {exc}"]

    errors: list[str] = []
    for line_no, target in _iter_links(text):
        workflow_destination = _workflow_destination(target)
        if workflow_destination is not None:
            if not public_tree.is_file(workflow_destination):
                errors.append(
                    f"{rel_path}:{line_no}: GitHub Actions URL references absent public workflow "
                    f"{workflow_destination!r}"
                )
            continue

        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or target.startswith("/"):
            continue
        resolved = _resolve_relative(rel_path, parsed.path)
        if resolved is None:
            errors.append(
                f"{rel_path}:{line_no}: relative link escapes or cannot resolve in public tree "
                f"-> {target!r}"
            )
            continue
        if parsed.path and resolved in allowlist:
            continue
        if parsed.path and not public_tree.exists(resolved):
            errors.append(
                f"{rel_path}:{line_no}: dangling public link target -> {target!r} "
                f"(resolved: {resolved})"
            )
            continue

        fragment = unquote(parsed.fragment)
        if not fragment or not resolved.endswith(".md") or not public_tree.is_file(resolved):
            continue
        if resolved not in loaded_anchors:
            try:
                target_text = loaded_texts.setdefault(resolved, public_tree.read_text(resolved))
            except (OSError, UnicodeError):
                continue
            loaded_anchors[resolved] = _anchors_for_markdown(target_text)
        accepted_fragments = loaded_anchors[resolved]
        canonical_fragment = (
            fragment.removeprefix("user-content-")
            if fragment.startswith("user-content-")
            else fragment
        )
        if fragment not in accepted_fragments and canonical_fragment not in accepted_fragments:
            errors.append(
                f"{rel_path}:{line_no}: missing Markdown anchor {fragment!r} in {resolved!r}"
            )

    seen_fenced_paths: set[tuple[int, str, str]] = set()
    for line_no, kind, raw_path in _iter_fenced_paths(text):
        resolved = _root_reference(raw_path)
        key = (line_no, kind, raw_path)
        if key in seen_fenced_paths:
            continue
        seen_fenced_paths.add(key)
        if resolved is None or not public_tree.is_file(resolved):
            errors.append(
                f"{rel_path}:{line_no}: fenced {kind} path is unsafe or absent from public tree "
                f"-> {raw_path!r}"
            )
    return errors


def audit_repository(root: Path = REPO_ROOT, allowlist: set[str] | None = None) -> CheckReport:
    tree = build_public_tree(root)
    allowed = _load_allowlist(tree.root) if allowlist is None else allowlist
    texts: dict[str, str] = {}
    anchors: dict[str, frozenset[str]] = {}
    errors = list(tree.configuration_errors)
    rows: list[tuple[str, int]] = []
    for rel_path in tree.surfaces:
        file_errors = check_file(
            rel_path,
            allowed,
            tree=tree,
            texts=texts,
            anchors=anchors,
        )
        rows.append((rel_path, len(file_errors)))
        errors.extend(file_errors)
    return CheckReport(tree=tree, rows=tuple(rows), errors=tuple(errors))


def main(argv: list[str]) -> int:
    del argv
    allowlist = _load_allowlist(REPO_ROOT)
    report = audit_repository(REPO_ROOT, allowlist)

    print("[check_doc_links] public documentation consistency check")
    print(f"[check_doc_links] repo root: {report.tree.root}")
    print(f"[check_doc_links] inventory mode: {report.tree.mode}")
    print(f"[check_doc_links] Markdown surfaces: {len(report.tree.surfaces)} file(s)")
    if allowlist:
        print(f"[check_doc_links] temporary allowlist entries: {len(allowlist)}")

    print()
    print(f"{'file':<70} {'issues':>8}")
    print("-" * 80)
    for rel_path, count in report.rows:
        print(f"{rel_path:<70} {count:>8}")
    print("-" * 80)
    surface_total = sum(count for _, count in report.rows)
    print(f"{'CONFIGURATION':<70} {len(report.tree.configuration_errors):>8}")
    print(f"{'TOTAL':<70} {surface_total + len(report.tree.configuration_errors):>8}")
    print()

    if report.errors:
        print("[check_doc_links] FAIL — public documentation inconsistencies:")
        for error in report.errors:
            print(f"  {error}")
        return 1

    print("[check_doc_links] OK — every public documentation reference resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
