"""Regression checks for the English default product surfaces."""

import ast
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata

import yaml

from live_mem.core.consolidator import SYSTEM_PROMPT, SYSTEM_PROMPT_FRENCH


ROOT = Path(__file__).resolve().parents[1]

FRENCH_ACCENTS = re.compile(r"[àâçéèêëîïôùûüÿœ]", re.IGNORECASE)
WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿŒœ0-9]+")

# Broad language signals, not a list of known findings. Shared technical nouns
# intentionally appear in both sets; phrase order and surrounding stopwords
# break ties. Values are accent-normalized because _normalized_words() is.
FRENCH_WORDS = {
    "a", "acces", "adresse", "afin", "ainsi", "annule", "annulee",
    "attente", "au", "aucun", "aucune", "aux", "avec", "avances", "avant",
    "bloque", "bloquee", "categorie", "ce", "ces", "comme", "commence",
    "compaction", "confirme", "connexion", "consolidation", "contient",
    "corrompu", "corrompue", "corrompues", "cours", "dans", "de",
    "declenche", "declenchee", "depasse", "depassee", "des", "disponible",
    "disponibles", "document", "documents", "doit", "donnee", "donnees",
    "du", "echec", "ecrire", "ecriture", "effective", "elle", "en",
    "enregistre", "entree", "erreur", "espace", "est", "et", "etape",
    "etre", "expire", "expiree", "fichier", "fichiers", "forcee",
    "garantie", "il", "inconnu", "inconnue", "indisponible",
    "interdit", "invalide", "invalides", "jeton", "la", "le", "lecture",
    "les", "leur", "longueur", "mais", "manquant", "memoire", "mise",
    "modification", "modifier", "necessaire", "ne", "non", "notre", "nouveau",
    "nouveaux", "nouvelle", "nouvelles", "nous",
    "obligatoire", "operation", "outils", "ou", "par", "pas", "maximale",
    "permissions", "perdue", "pour", "quota", "rapport", "reessayer",
    "recherche", "reessayez", "refus", "refusee", "reglages", "repare",
    "reparation",
    "replication", "requise", "restauration", "resultat", "revoque",
    "revoquee", "sauvegarde", "sans", "scan", "seul", "seule", "suivante",
    "supportee", "synchronisation", "termine", "terminee", "tous", "toutes",
    "trop", "un", "une", "valide", "valides", "verrou", "verification",
    "via", "volumineuse", "volumineux", "vous",
}
ENGLISH_WORDS = {
    "a", "access", "admin", "all", "allow", "and", "archive", "automatic",
    "available", "backup", "before", "cannot", "checksum", "client",
    "compaction", "consolidation", "content", "create", "default", "delete",
    "description", "document", "documents", "download", "effective", "empty",
    "en", "error", "expire", "file", "files", "filter", "for", "format", "from",
    "identifier", "in", "invalid", "is", "memory", "message", "metadata",
    "modification", "must", "no", "non", "not", "of", "operation", "or",
    "permission", "permissions", "prefix", "public", "quota", "read",
    "replace", "replication", "required", "restore", "result", "scan",
    "source", "sources",
    "space", "status", "the", "this", "to", "token", "true", "update", "use",
    "valid", "verification", "via", "was", "with", "without", "write",
}
FRENCH_BIGRAMS = {
    ("documents", "sources"),
    ("du", "document"),
    ("sans", "modification"),
    ("trop", "long"),
}
ENGLISH_BIGRAMS = {
    ("source", "documents"),
    ("document", "identifier"),
    ("without", "modification"),
    ("too", "long"),
}

# The product uses this established English loanword in an otherwise-English
# sentence. Keep the exception narrow: other accented words remain evidence of
# French copy instead of becoming a broad accent allowlist.
ENGLISH_ACCENTED_LOANWORDS = {"facade"}

PYTHON_RUNTIME_ROOTS = (
    ROOT / "src/live_mem",
    ROOT / "src/hivemind_inference",
    ROOT / "services/graph-memory/src/mcp_memory",
)
PYTHON_RUNTIME_SURFACES = tuple(
    sorted(
        {
            path
            for runtime_root in PYTHON_RUNTIME_ROOTS
            for path in runtime_root.rglob("*.py")
        }
    )
)
STATIC_CLIENT_SURFACES = (
    *sorted((ROOT / "src/live_mem/static").rglob("*.html")),
    *sorted((ROOT / "src/live_mem/static").rglob("*.js")),
    *sorted((ROOT / "src/live_mem/static").rglob("*.svg")),
    *sorted((ROOT / "src/live_mem/static").rglob("*.css")),
    *sorted(
        (ROOT / "services/graph-memory/src/mcp_memory/static").rglob("*.html")
    ),
    *sorted(
        (ROOT / "services/graph-memory/src/mcp_memory/static").rglob("*.js")
    ),
    *sorted(
        (ROOT / "services/graph-memory/src/mcp_memory/static").rglob("*.svg")
    ),
    *sorted(
        (ROOT / "services/graph-memory/src/mcp_memory/static").rglob("*.css")
    ),
)
EMBEDDED_ONTOLOGY_SURFACES = tuple(
    sorted((ROOT / "services/graph-memory/ONTOLOGIES").glob("*.yaml"))
)
OPERATOR_CLI_SURFACES = (ROOT / "scripts/test_recette.py",)
CLI_RUNTIME_SURFACES = tuple(
    sorted(
        (
            *(ROOT / "scripts/cli").rglob("*.py"),
            ROOT / "scripts/mcp_cli.py",
        )
    )
)

# These values are compatibility inputs, never defaults or emitted UI/MCP copy.
INTENTIONAL_FRENCH_COMPATIBILITY = {
    "SYSTEM_PROMPT_FRENCH": SYSTEM_PROMPT_FRENCH,
    "historical_inferred_marker": "[inféré]",
}
PARSER_INPUT_VOCAB_ASSIGNMENTS = frozenset({"STOP_WORDS", "_STATUS_KEYWORDS"})


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _normalized_words(text: str) -> list[str]:
    return [
        "".join(
            char
            for char in unicodedata.normalize("NFKD", token.casefold())
            if not unicodedata.combining(char)
        )
        for token in WORD.findall(text)
    ]


def _looks_french(text: str) -> bool:
    """Detect ordinary French copy without classifying English loanwords."""

    if re.fullmatch(r"[a-z][a-z0-9+.-]*://\S+", text, re.IGNORECASE):
        return False

    raw_words = WORD.findall(text)
    words = _normalized_words(text)
    if not words:
        return False

    pairs = set(zip(words, words[1:]))
    french_only = {
        word for word in words if word in FRENCH_WORDS - ENGLISH_WORDS
    }
    french_score = sum(word in FRENCH_WORDS for word in words)
    english_score = sum(word in ENGLISH_WORDS for word in words)
    french_score += 3 * len(pairs & FRENCH_BIGRAMS)
    english_score += 3 * len(pairs & ENGLISH_BIGRAMS)
    accented_nonloanword_words = {
        normalized
        for raw, normalized in zip(raw_words, words)
        if FRENCH_ACCENTS.search(raw)
        and normalized not in ENGLISH_ACCENTED_LOANWORDS
    }

    if len(words) == 1:
        return bool(french_only or accented_nonloanword_words)
    if accented_nonloanword_words:
        return True
    if words[:2] == ["service", "cloud"]:
        return True
    if pairs & FRENCH_BIGRAMS:
        return True
    # A user-visible English sentence can carry a French fragment in an
    # example, option value, or appended note. Do not let surrounding English
    # stopwords hide a French-only token such as "nouvelle".
    return bool(french_only)


@dataclass(frozen=True)
class _RuntimeCopy:
    """A string literal with an actionable source origin."""

    relative_path: str
    line: int
    column: int
    value: str

    def render(self) -> str:
        normalized = " ".join(self.value.split())
        if len(normalized) > 180:
            normalized = f"{normalized[:177]}..."
        return (
            f"{self.relative_path}:{self.line}:{self.column}: "
            f"{normalized!r}"
        )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _assigned_names(statement: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = statement.targets if isinstance(statement, ast.Assign) else [
        statement.target
    ]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _docstring_constant(
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.Constant | None:
    if not node.body:
        return None
    candidate = node.body[0]
    if (
        isinstance(candidate, ast.Expr)
        and isinstance(candidate.value, ast.Constant)
        and isinstance(candidate.value.value, str)
    ):
        return candidate.value
    return None


def _decorator_uses_default_tool_description(decorator: ast.expr) -> bool:
    """Whether FastMCP will expose a function docstring as tool metadata."""

    if isinstance(decorator, ast.Call):
        return _call_name(decorator) == "tool" and not any(
            keyword.arg == "description" for keyword in decorator.keywords
        )
    if isinstance(decorator, ast.Name):
        return decorator.id == "tool"
    return isinstance(decorator, ast.Attribute) and decorator.attr == "tool"


def _legacy_french_condition(test: ast.expr) -> tuple[bool, bool]:
    """Return whether the test is explicit legacy-French and whether negated."""

    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    inspected = test.operand if negated else test
    identifiers = {
        identifier.casefold()
        for node in ast.walk(inspected)
        for identifier in (
            ([node.id] if isinstance(node, ast.Name) else [])
            + ([node.attr] if isinstance(node, ast.Attribute) else [])
        )
    }
    return (
        any("legacy" in item and "french" in item for item in identifiers),
        negated,
    )


def _mark_descendants(nodes: list[ast.AST], excluded: set[int]) -> None:
    for node in nodes:
        excluded.update(id(descendant) for descendant in ast.walk(node))


def _runtime_literal_exclusions(tree: ast.Module) -> set[int]:
    """Exclude only compatibility branches and parser-input vocabulary."""

    excluded: set[int] = set()
    public_tool_docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            docstring = _docstring_constant(node)
            if docstring is not None:
                is_public_tool_docstring = isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and any(
                    _decorator_uses_default_tool_description(decorator)
                    for decorator in node.decorator_list
                )
                if is_public_tool_docstring:
                    public_tool_docstrings.add(id(docstring))
                else:
                    excluded.add(id(docstring))

        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
            names = _assigned_names(node)
            if "SYSTEM_PROMPT_FRENCH" in names:
                _mark_descendants([node.value], excluded)
            if names & PARSER_INPUT_VOCAB_ASSIGNMENTS:
                _mark_descendants([node.value], excluded)

        if isinstance(node, ast.If):
            is_legacy, negated = _legacy_french_condition(node.test)
            if is_legacy:
                _mark_descendants(node.orelse if negated else node.body, excluded)
        elif isinstance(node, ast.IfExp):
            is_legacy, negated = _legacy_french_condition(node.test)
            if is_legacy:
                _mark_descendants(
                    [node.orelse if negated else node.body], excluded
                )

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr
            in {
                "compile", "findall", "finditer", "fullmatch", "match",
                "search", "split", "sub", "subn",
            }
            and node.args
        ):
            _mark_descendants([node.args[0]], excluded)

    excluded.difference_update(public_tool_docstrings)
    return excluded


def _scan_python_runtime_literals(
    source: str,
    *,
    relative_path: str = "<memory>",
) -> list[_RuntimeCopy]:
    """Inventory every ordinary default-runtime literal, not selected fields.

    A literal can reach arbitrary returned JSON fields, persisted payloads,
    ctx.info/progress notifications, logger output, or str(exception)/to_dict
    serialization. Scanning the whole default runtime prevents these routes
    from silently escaping a hand-maintained response-key list.
    """

    tree = ast.parse(source)
    excluded = _runtime_literal_exclusions(tree)
    matches: list[_RuntimeCopy] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or id(node) in excluded
            or not _looks_french(node.value)
        ):
            continue
        matches.append(
            _RuntimeCopy(
                relative_path=relative_path,
                line=node.lineno,
                column=node.col_offset + 1,
                value=node.value,
            )
        )
    return sorted(matches, key=lambda item: (item.line, item.column, item.value))


def _yaml_string_values(value: object, location: str = "$") -> list[tuple[str, str]]:
    """Return deserialized YAML strings, never comments or raw tokens."""

    if isinstance(value, str):
        return [(location, value)]
    if isinstance(value, list):
        return [
            result
            for index, child in enumerate(value)
            for result in _yaml_string_values(child, f"{location}[{index}]")
        ]
    if isinstance(value, dict):
        return [
            result
            for key, child in value.items()
            for result in _yaml_string_values(child, f"{location}.{key}")
        ]
    return []


def _scan_embedded_ontology_values(
    path: Path, *, source: str | None = None
) -> list[_RuntimeCopy]:
    """Scan values the default embedded ontology loader can deserialize."""

    loaded = yaml.safe_load(
        source if source is not None else path.read_text(encoding="utf-8")
    )
    return [
        _RuntimeCopy(
            relative_path=path.relative_to(ROOT).as_posix(),
            line=0,
            column=0,
            value=f"{location}: {value}",
        )
        for location, value in _yaml_string_values(loaded)
        if _looks_french(value)
    ]


def _literal_strings(node: ast.AST) -> list[ast.Constant]:
    """Return string literals embedded in an operator-facing call argument."""

    return [
        descendant
        for descendant in ast.walk(node)
        if isinstance(descendant, ast.Constant)
        and isinstance(descendant.value, str)
    ]


def _scan_operator_cli_literals(
    source: str,
    *,
    relative_path: str = "scripts/test_recette.py",
) -> list[_RuntimeCopy]:
    """Scan direct recipe-runner output and argparse presentation sinks.

    The recipe has test-data fixtures with intentional French content. Its
    observable operator surface instead flows through output helpers, print,
    and argparse metadata. This keeps fixtures and implementation
    documentation out of scope while guarding every emitted prompt, heading,
    section, pass/fail/skip result, and option description.
    """

    tree = ast.parse(source)
    sink_names = {
        "print",
        "header",
        "section",
        "pause",
        "test_pass",
        "test_fail",
        "test_skip",
        "vprint",
        "ArgumentParser",
        "add_argument",
        "SystemExit",
    }
    matches: list[_RuntimeCopy] = []
    for node in ast.walk(tree):
        arguments: tuple[ast.AST, ...] = ()
        if isinstance(node, ast.Call) and _call_name(node) in sink_names:
            arguments = (*node.args, *(keyword.value for keyword in node.keywords))
        elif (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and node.value is not None
            and "SUITES" in _assigned_names(node)
        ):
            # The --list command prints these descriptions verbatim; unlike
            # RECETTE_NOTES they are not test fixtures.
            arguments = (node.value,)
        if not arguments:
            continue
        for argument in arguments:
            for literal in _literal_strings(argument):
                if not _looks_french(literal.value):
                    continue
                matches.append(
                    _RuntimeCopy(
                        relative_path=relative_path,
                        line=literal.lineno,
                        column=literal.col_offset + 1,
                        value=literal.value,
                    )
                )
    return sorted(matches, key=lambda item: (item.line, item.column, item.value))


def _scan_click_cli_literals(
    source: str,
    *,
    relative_path: str,
) -> list[_RuntimeCopy]:
    """Scan shipped Click/Rich CLI copy and metadata.

    The general runtime inventory covers ordinary literals, including Rich
    Table/Panel titles and cells that later reach console.print. Click command
    docstrings are added separately because Click exposes them as command help
    while the runtime scanner rightly excludes ordinary implementation
    docstrings. Comments and non-command docstrings remain out of scope.
    """

    tree = ast.parse(source)
    matches = _scan_python_runtime_literals(
        source,
        relative_path=relative_path,
    )
    seen = {(match.line, match.column - 1, match.value) for match in matches}

    def add(literal: ast.Constant) -> None:
        if not _looks_french(literal.value):
            return
        marker = (literal.lineno, literal.col_offset, literal.value)
        if marker in seen:
            return
        seen.add(marker)
        matches.append(
            _RuntimeCopy(
                relative_path=relative_path,
                line=literal.lineno,
                column=literal.col_offset + 1,
                value=literal.value,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(
                _call_name(decorator) in {"command", "group"}
                for decorator in node.decorator_list
            ):
                docstring = _docstring_constant(node)
                if docstring is not None:
                    add(docstring)

    return sorted(matches, key=lambda item: (item.line, item.column, item.value))


class _HtmlCopyParser(HTMLParser):
    _VISIBLE_ATTRIBUTES = frozenset(
        {
            "alt",
            "aria-description",
            "aria-label",
            "aria-placeholder",
            "aria-roledescription",
            "aria-valuetext",
            "label",
            "placeholder",
            "title",
            "value",
        }
    )
    _VISIBLE_DATA_ATTRIBUTE_TOKENS = frozenset(
        {
            "caption",
            "confirm",
            "content",
            "description",
            "empty",
            "help",
            "hint",
            "label",
            "message",
            "placeholder",
            "text",
            "title",
            "tooltip",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.copy: list[str] = []
        self._non_copy_tag_depth = 0
        self._script_depth = 0
        self._style_depth = 0
        self.script: list[str] = []
        self.style: list[str] = []

    def handle_data(self, data: str) -> None:
        if self._script_depth:
            self.script.append(data)
        elif self._style_depth:
            self.style.append(data)
        elif not self._non_copy_tag_depth:
            self.copy.append(data)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style"}:
            self._non_copy_tag_depth += 1
        if tag == "script":
            self._script_depth += 1
        if tag == "style":
            self._style_depth += 1
        self.copy.extend(
            value
            for name, value in attrs
            if value and self._is_visible_attribute(tag, name)
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_depth:
            self._script_depth -= 1
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
        if tag in {"script", "style"} and self._non_copy_tag_depth:
            self._non_copy_tag_depth -= 1

    @classmethod
    def _is_visible_attribute(cls, tag: str, name: str) -> bool:
        normalized_name = name.lower()
        if normalized_name in cls._VISIBLE_ATTRIBUTES:
            return True
        if normalized_name.startswith("data-"):
            data_words = set(normalized_name[5:].split("-"))
            return bool(data_words & cls._VISIBLE_DATA_ATTRIBUTE_TOKENS)
        return (
            tag == "meta"
            and normalized_name == "content"
        )


def _javascript_regex_starts(source: str, index: int) -> bool:
    """Whether a slash can introduce a regex literal at this token boundary."""

    cursor = index - 1
    while cursor >= 0 and source[cursor].isspace():
        cursor -= 1
    if cursor < 0:
        return True
    if source[cursor] in "([{:;,=!?&|+-*%^~<>":
        return True
    if not (source[cursor].isalnum() or source[cursor] in "_$"):
        return False

    end = cursor + 1
    while cursor >= 0 and (
        source[cursor].isalnum() or source[cursor] in "_$"
    ):
        cursor -= 1
    return source[cursor + 1:end] in {
        "case",
        "delete",
        "in",
        "instanceof",
        "new",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }


def _skip_javascript_regex(source: str, index: int) -> int:
    """Return the index after a regex literal and its flags."""

    index += 1
    in_character_class = False
    while index < len(source):
        character = source[index]
        if character == "\\":
            index += 2
            continue
        if character == "[":
            in_character_class = True
        elif character == "]":
            in_character_class = False
        elif character == "/" and not in_character_class:
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1
            return index
        elif character in "\r\n":
            return index
        index += 1
    return index


def _copy_javascript_quoted(
    source: str, index: int, output: list[str]
) -> int:
    quote = source[index]
    output.append(quote)
    index += 1
    while index < len(source):
        character = source[index]
        output.append(character)
        if character == "\\" and index + 1 < len(source):
            output.append(source[index + 1])
            index += 2
            continue
        index += 1
        if character == quote:
            break
    return index


def _copy_javascript_template(
    source: str, index: int, output: list[str]
) -> int:
    output.append("`")
    index += 1
    while index < len(source):
        character = source[index]
        if character == "\\" and index + 1 < len(source):
            output.extend((character, source[index + 1]))
            index += 2
            continue
        if character == "`":
            output.append(character)
            return index + 1
        if character == "$" and index + 1 < len(source) and source[index + 1] == "{":
            output.extend((character, "{"))
            index = _copy_javascript_code(
                source, index + 2, output, until_closing_brace=True
            )
            continue
        output.append(character)
        index += 1
    return index


def _copy_javascript_code(
    source: str,
    index: int,
    output: list[str],
    *,
    until_closing_brace: bool = False,
) -> int:
    brace_depth = 1 if until_closing_brace else 0
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if character in {"'", '"'}:
            index = _copy_javascript_quoted(source, index, output)
            continue
        if character == "`":
            index = _copy_javascript_template(source, index, output)
            continue
        if character == "/" and following == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if character == "/" and following == "*":
            index += 2
            while index < len(source):
                if source[index:index + 2] == "*/":
                    index += 2
                    break
                if source[index] in "\r\n":
                    output.append(source[index])
                index += 1
            continue
        if character == "/" and _javascript_regex_starts(source, index):
            end = _skip_javascript_regex(source, index)
            output.append(source[index:end])
            index = end
            continue

        output.append(character)
        index += 1
        if until_closing_brace and character == "{":
            brace_depth += 1
        elif until_closing_brace and character == "}":
            brace_depth -= 1
            if brace_depth == 0:
                return index
    return index


def _strip_javascript_comments(source: str) -> str:
    """Remove comments without mistaking strings, templates, or regexes for them."""

    output: list[str] = []
    _copy_javascript_code(source, 0, output)
    return "".join(output)


def _read_javascript_quoted(source: str, index: int) -> tuple[int, str]:
    quote = source[index]
    index += 1
    literal: list[str] = []
    while index < len(source):
        character = source[index]
        if character == "\\" and index + 1 < len(source):
            literal.extend((character, source[index + 1]))
            index += 2
            continue
        if character == quote:
            return index + 1, "".join(literal)
        literal.append(character)
        index += 1
    return index, "".join(literal)


def _read_javascript_template(
    source: str, index: int, literals: list[str]
) -> tuple[int, str]:
    index += 1
    literal: list[str] = []
    while index < len(source):
        character = source[index]
        if character == "\\" and index + 1 < len(source):
            literal.extend((character, source[index + 1]))
            index += 2
            continue
        if character == "`":
            return index + 1, "".join(literal)
        if character == "$" and index + 1 < len(source) and source[index + 1] == "{":
            literal.append("${...}")
            index = _read_javascript_code(
                source, index + 2, literals, until_closing_brace=True
            )
            continue
        literal.append(character)
        index += 1
    return index, "".join(literal)


def _read_javascript_code(
    source: str,
    index: int,
    literals: list[str],
    *,
    until_closing_brace: bool = False,
) -> int:
    brace_depth = 1 if until_closing_brace else 0
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if character in {"'", '"'}:
            index, literal = _read_javascript_quoted(source, index)
            literals.append(literal)
            continue
        if character == "`":
            index, literal = _read_javascript_template(source, index, literals)
            literals.append(literal)
            continue
        if character == "/" and following == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if character == "/" and following == "*":
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        if character == "/" and _javascript_regex_starts(source, index):
            index = _skip_javascript_regex(source, index)
            continue
        if until_closing_brace and character == "{":
            brace_depth += 1
        elif until_closing_brace and character == "}":
            brace_depth -= 1
            index += 1
            if brace_depth == 0:
                return index
            continue
        index += 1
    return index


def _javascript_string_literals(source: str) -> list[str]:
    """Read JS literals after quote-aware removal of comments."""

    literals: list[str] = []
    _read_javascript_code(_strip_javascript_comments(source), 0, literals)
    return literals


def _javascript_product_copy(source: str) -> list[str]:
    """Keep JavaScript literals while discarding comments and markup wrappers."""

    literals = _javascript_string_literals(source)
    segments = [
        unescape(segment)
        for literal in literals
        for segment in re.split(
            r"\n|\$\{[^}]*\}",
            re.sub(r"<[^>]*>", "\n", literal),
        )
        if segment.strip()
    ]
    return [
        segment
        for segment in segments
        if not re.search(
            r"(?:^\s*\.|=>|\breturn\b|\bclass=|\blang=|\.toLocale|"
            r"font-family|\ben-[A-Z]{2}\b)",
            segment,
        )
    ]


CSS_CONTENT = re.compile(
    r"\bcontent\s*:\s*(?:"
    r"'((?:\\.|[^'\\])*)'|"
    r'"((?:\\.|[^"\\])*)"'
    r")",
    re.DOTALL,
)


def _css_product_copy(source: str) -> list[str]:
    """Extract only rendered CSS pseudo-element content, never comments."""

    without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return [
        unescape(next(group for group in match.groups() if group is not None))
        for match in CSS_CONTENT.finditer(without_comments)
    ]


def _template_markup_copy(literal: str) -> list[str]:
    """Extract visible text and attributes embedded in dynamic HTML templates."""

    if "<" not in literal or ">" not in literal:
        return []
    parser = _HtmlCopyParser()
    parser.feed(literal)
    return parser.copy


def _static_product_copy(
    source: str, *, html: bool, css: bool = False
) -> list[str]:
    if html:
        parser = _HtmlCopyParser()
        parser.feed(source)
        return (
            parser.copy
            + _javascript_product_copy("\n".join(parser.script))
            + _css_product_copy("\n".join(parser.style))
        )
    if css:
        return _css_product_copy(source)
    javascript_copy = _javascript_product_copy(source)
    template_copy = [
        candidate
        for literal in _javascript_string_literals(source)
        for candidate in _template_markup_copy(literal)
    ]
    return javascript_copy + template_copy


def test_consolidation_prompt_defaults_to_english() -> None:
    assert SYSTEM_PROMPT.startswith(
        "You are an assistant specialized in maintaining project Memory Banks."
    )
    assert "Write all generated bank prose" in SYSTEM_PROMPT
    assert "[inferred]" in SYSTEM_PROMPT
    assert "Tu es un assistant" not in SYSTEM_PROMPT


def test_french_compatibility_allowlist_is_explicit_and_non_default() -> None:
    assert INTENTIONAL_FRENCH_COMPATIBILITY == {
        "SYSTEM_PROMPT_FRENCH": SYSTEM_PROMPT_FRENCH,
        "historical_inferred_marker": "[inféré]",
    }
    assert SYSTEM_PROMPT_FRENCH.startswith("Tu es un assistant")
    assert SYSTEM_PROMPT is not SYSTEM_PROMPT_FRENCH
    assert _looks_french(SYSTEM_PROMPT_FRENCH)
    assert _looks_french("[inféré]")


def test_operator_ui_uses_english_locale_and_language_tag() -> None:
    config = _read("src/live_mem/static/js/config.js")
    admin = _read("src/live_mem/static/js/admin-app.js")

    assert "fr-FR" not in config
    assert "lang=\"fr\"" not in admin
    assert "en-US" in config
    assert "lang=\"en\"" in admin


def test_graph_ui_defaults_to_english() -> None:
    graph_html = _read(
        "services/graph-memory/src/mcp_memory/static/graph.html"
    )
    graph_js = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "services/graph-memory/src/mcp_memory/static/js"
        ).glob("*.js")
    )
    assert '<html lang="en">' in graph_html
    assert '<html lang="fr">' not in graph_html
    assert "fr-FR" not in graph_js
    for misplaced_label in (
        "Se connecter", "-- Mémoire --", "Réflexion en cours",
        "Isoler le sujet", "Token invalide",
    ):
        assert misplaced_label not in graph_html
        assert misplaced_label not in graph_js


def test_graph_mcp_metadata_has_english_defaults() -> None:
    server = _read("services/graph-memory/src/mcp_memory/server.py")
    assert (
        '@mcp.tool(description="Create an isolated graph-memory namespace.")'
        in server
    )
    assert 'Field(description="Identifiant' not in server
    assert 'Field(description="ID de la mémoire' not in server


def test_private_runtime_additions_keep_client_messages_in_english() -> None:
    surfaces = {
        "src/live_mem/core/tokens.py": "No operation was requested",
        "src/live_mem/core/backup.py": "Restore refused",
        "src/live_mem/core/graph_bridge.py": "The embedded long runtime is configured",
        "src/live_mem/core/gc.py": "No orphaned notes to consolidate",
        "services/graph-memory/src/mcp_memory/core/ingest_queue.py": (
            "Do not wait for completion or poll automatically"
        ),
    }
    for path, expected in surfaces.items():
        assert expected in _read(path)


def test_python_default_runtime_inventory_rejects_french_copy() -> None:
    failures = [
        match.render()
        for path in PYTHON_RUNTIME_SURFACES
        for match in _scan_python_runtime_literals(
            path.read_text(encoding="utf-8"),
            relative_path=path.relative_to(ROOT).as_posix(),
        )
    ]
    assert not failures, (
        "French default-runtime literals found:\n" + "\n".join(failures)
    )


def test_embedded_ontology_loaded_values_reject_french_copy() -> None:
    failures = [
        match.render()
        for path in EMBEDDED_ONTOLOGY_SURFACES
        for match in _scan_embedded_ontology_values(path)
    ]
    assert not failures, (
        "French values loaded by default embedded ontologies found:\n"
        + "\n".join(failures)
    )


def test_static_client_surfaces_reject_french_product_copy() -> None:
    failures: dict[str, list[str]] = {}
    for path in STATIC_CLIENT_SURFACES:
        product_copy = _static_product_copy(
            path.read_text(encoding="utf-8"),
            html=path.suffix in {".html", ".svg"},
            css=path.suffix == ".css",
        )
        matches = sorted(
            {
                candidate.strip()
                for candidate in product_copy
                if _looks_french(candidate)
            }
        )
        if matches:
            failures[path.relative_to(ROOT).as_posix()] = matches
    assert failures == {}


def test_operator_recipe_cli_rejects_french_messages() -> None:
    failures = [
        match.render()
        for path in OPERATOR_CLI_SURFACES
        for match in _scan_operator_cli_literals(
            path.read_text(encoding="utf-8"),
            relative_path=path.relative_to(ROOT).as_posix(),
        )
    ]

    assert not failures, (
        "French messages in the operator recipe CLI found:\n"
        + "\n".join(failures)
    )


def test_operator_recipe_cli_guard_covers_all_direct_output_sinks() -> None:
    mutation = '''
print("Données corrompues")
header("Étape suivante")
section("Opération")
pause("Réessayez plus tard")
test_pass("Token révoqué")
test_fail("Échec")
test_skip("Résultat indisponible")
vprint("Sauvegarde corrompue")
raise SystemExit("Erreur de réplication")
parser = argparse.ArgumentParser(description="Opération")
parser.add_argument("--example", help="Échec")
SUITES = {"demo": ("Nouvelle suite", object())}
'''

    matches = _scan_operator_cli_literals(
        mutation,
        relative_path="scripts/test_recette.py",
    )

    assert {
        "Données corrompues",
        "Étape suivante",
        "Opération",
        "Réessayez plus tard",
        "Token révoqué",
        "Échec",
        "Résultat indisponible",
        "Sauvegarde corrompue",
        "Erreur de réplication",
        "Nouvelle suite",
    } <= {match.value for match in matches}


def test_click_cli_surfaces_reject_french_messages() -> None:
    failures = [
        match.render()
        for path in CLI_RUNTIME_SURFACES
        for match in _scan_click_cli_literals(
            path.read_text(encoding="utf-8"),
            relative_path=path.relative_to(ROOT).as_posix(),
        )
    ]

    assert not failures, (
        "French messages in the shipped Click/Rich CLI found:\n"
        + "\n".join(failures)
    )


def test_click_cli_guard_covers_metadata_console_and_usage_sinks() -> None:
    mutation = '''
import click

SHELL_COMMANDS = {"help": "Opération suivante"}
table = Table(title="Résultat indisponible")

@click.command()
@click.option("--example", help="Données corrompues")
def command():
    """Nouvelle description : échec de réplication."""
    console.print("Réessayez plus tard")
    raise click.UsageError("Token révoqué")
'''

    matches = _scan_click_cli_literals(
        mutation,
        relative_path="scripts/cli/commands.py",
    )

    assert {
        "Données corrompues",
        "Nouvelle description : échec de réplication.",
        "Réessayez plus tard",
        "Token révoqué",
        "Résultat indisponible",
        "Opération suivante",
    } <= {match.value for match in matches}


def test_runtime_inventory_covers_arbitrary_output_and_durable_paths() -> None:
    mutation = '''
def persist_synthesis():
    payload = {"synthesis": "Données corrompues"}
    storage.put(payload)
    return payload

class DurableReason:
    def to_dict(self):
        return {"reason": "Opération refusée"}

class BackupError(Exception):
    pass

def restore():
    try:
        raise BackupError("Sauvegarde corrompue")
    except BackupError as exc:
        return {"serialized": str(exc)}

async def ingest(ctx, progress):
    await ctx.info("Étape suivante")
    progress(message="Réessayez plus tard")
    logger.error("Échec de synchronisation")
    job = {"error": "Échec de l'extraction"}
    return {"job": job}
'''
    matches = _scan_python_runtime_literals(
        mutation, relative_path="mutation/runtime_paths.py"
    )
    values = {match.value for match in matches}
    assert {
        "Données corrompues",
        "Opération refusée",
        "Sauvegarde corrompue",
        "Étape suivante",
        "Réessayez plus tard",
        "Échec de synchronisation",
        "Échec de l'extraction",
    } <= values
    assert all(match.relative_path == "mutation/runtime_paths.py" for match in matches)
    assert all(match.line > 0 and match.column > 0 for match in matches)


def test_runtime_inventory_keeps_only_structural_compatibility_exclusions() -> None:
    mutation = '''
SYSTEM_PROMPT_FRENCH = "Tu es un assistant de maintenance."
_INFERRED_MARKER_RE = re.compile(r"\\[(?:inferred|inféré)\\]")
STOP_WORDS = {"données", "corrompues"}
_STATUS_KEYWORDS = ("résolu", "fermée")

def choose(legacy_french):
    if legacy_french:
        return {"message": "Opération héritée"}
    return {"message": "Operation complete"}

def public_tool():
    """Retourne une opération."""
'''
    assert _scan_python_runtime_literals(mutation) == []


def test_runtime_inventory_does_not_allow_marker_on_an_output_path() -> None:
    mutation = '''
_INFERRED_MARKER_RE = re.compile(r"\\[(?:inferred|inféré)\\]")

def report():
    return {"message": "[inféré]"}
'''

    assert [match.value for match in _scan_python_runtime_literals(mutation)] == [
        "[inféré]"
    ]


def test_runtime_inventory_keeps_public_tool_docstring_defaults_visible() -> None:
    mutation = '''
@mcp.tool()
async def system_about():
    """Retourne une description complète du service."""
'''
    matches = _scan_python_runtime_literals(mutation)
    assert [match.value for match in matches] == [
        "Retourne une description complète du service."
    ]


def test_runtime_inventory_mutation_proves_real_default_surfaces() -> None:
    mutations = (
        # B4: a fallback synthesis is persisted and returned by space_summary.
        (
            "src/live_mem/core/consolidator.py",
            "(partial consolidation — JSON repaired automatically; ",
            "(consolidation partielle — JSON réparé automatiquement; ",
        ),
        # NodeHealth.reason is persisted in node_status.json.
        (
            "src/live_mem/core/hivemind/models.py",
            'reason: str = ""',
            'reason: str = "Données corrompues"',
        ),
        # A backup exception can reach the normal restore response.
        (
            "src/live_mem/core/backup.py",
            "invalid JSON",
            "Sauvegarde corrompue",
        ),
        # Queue errors are serialized for polling clients.
        (
            "services/graph-memory/src/mcp_memory/core/ingest_queue.py",
            "Text extraction failed",
            "Échec de l'extraction",
        ),
        # MCP ctx.info progress is an externally visible default surface.
        (
            "services/graph-memory/src/mcp_memory/server.py",
            "📦 Decoding",
            "📦 Étape suivante",
        ),
        # The system-about text is operator-visible, even without accents.
        (
            "src/live_mem/server.py",
            "MCP tools:",
            "outils MCP",
        ),
    )
    for path, fixed, regressed in mutations:
        source = _read(path)
        mutated = source.replace(fixed, regressed, 1)
        assert mutated != source, f"expected mutation target missing in {path}"
        assert _scan_python_runtime_literals(
            mutated, relative_path=path
        ), f"French mutation escaped inventory in {path}"


def test_runtime_inventory_mutation_proves_to_dict_serialization() -> None:
    mutation = '''
class PeerChannelError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Erreur de réplication")

    def to_dict(self):
        return {"message": str(self)}
'''

    assert [match.value for match in _scan_python_runtime_literals(mutation)] == [
        "Erreur de réplication"
    ]


def test_language_guard_detects_short_and_single_accent_french_labels() -> None:
    for french_copy in (
        "Opération", "Échec", "Données corrompues", "Token révoqué",
        "Étape suivante", "Résultat indisponible", "Réessayez plus tard",
        "Compaction déclenchée", "Nouvelle description", "Recherche",
        "Outils MCP", "Service cloud (IaaS)",
        'English example: space update -d "Nouvelle description"',
        "Rôle requis",
    ):
        assert _looks_french(french_copy), french_copy


def test_language_guard_accepts_english_shared_words_and_loanwords() -> None:
    for english_copy in (
        "Quota exceeded",
        "Tokens expire after 30 days",
        "This token will expire soon",
        "The reindex façade keeps the public contract stable",
        "A managed as-a-service cloud service is available",
    ):
        assert not _looks_french(english_copy)


def test_embedded_ontology_scan_uses_loaded_values_not_comments() -> None:
    source = """
# Données corrompues in a comment must not matter.
description: English default
examples:
  - Opération refusée
cloud:
  description: Service cloud (IaaS)
"""
    path = ROOT / "services/graph-memory/ONTOLOGIES/general.yaml"
    matches = _scan_embedded_ontology_values(path, source=source)
    assert [match.value for match in matches] == [
        "$.examples[0]: Opération refusée",
        "$.cloud.description: Service cloud (IaaS)",
    ]


def test_python_runtime_surface_rule_covers_all_shipped_runtime_roots() -> None:
    relative_surfaces = {
        path.relative_to(ROOT).as_posix()
        for path in PYTHON_RUNTIME_SURFACES
    }
    assert "src/live_mem/core/graph_bridge.py" in relative_surfaces
    assert "src/live_mem/core/hivemind/models.py" in relative_surfaces
    assert "src/live_mem/mesh/transport.py" in relative_surfaces
    assert "src/hivemind_inference/runtime.py" in relative_surfaces
    assert "services/graph-memory/src/mcp_memory/core/backup.py" in relative_surfaces
    assert OPERATOR_CLI_SURFACES == (ROOT / "scripts/test_recette.py",)
    assert {
        path.relative_to(ROOT).as_posix() for path in CLI_RUNTIME_SURFACES
    } == {
        "scripts/cli/__init__.py",
        "scripts/cli/client.py",
        "scripts/cli/commands.py",
        "scripts/cli/display.py",
        "scripts/cli/shell.py",
        "scripts/mcp_cli.py",
    }
    static_surfaces = {
        path.relative_to(ROOT).as_posix()
        for path in STATIC_CLIENT_SURFACES
    }
    assert "src/live_mem/static/img/hivemind-favicon.svg" in static_surfaces
    assert (
        "services/graph-memory/src/mcp_memory/static/img/logo-cloudtemple.svg"
        in static_surfaces
    )


def test_static_guard_keeps_entity_and_semicolon_copy_visible() -> None:
    product_copy = _static_product_copy(
        'const label = "Op&eacute;ration refus&eacute;e; r&eacute;essayez";',
        html=False,
    )
    assert product_copy == ["Opération refusée; réessayez"]
    assert _looks_french(product_copy[0])


def test_static_guard_keeps_urls_inside_javascript_strings_visible() -> None:
    product_copy = _static_product_copy(
        '''
        // Opération uniquement dans un commentaire.
        const label = "Opération impossible: https://docs.example.test/help";
        /* Échec uniquement dans un autre commentaire. */
        ''',
        html=False,
    )

    assert product_copy == [
        "Opération impossible: https://docs.example.test/help"
    ]
    assert _looks_french(product_copy[0])


def test_static_guard_scans_template_interpolation_and_visible_markup() -> None:
    tick = chr(96)
    product_copy = _static_product_copy(
        "\n".join(
            (
                "const label = "
                + tick
                + "$"
                + '{condition ? "Opération refusée" : "OK"}'
                + tick
                + ";",
                "const button = "
                + tick
                + '<button title="Échec de sauvegarde">Go</button>'
                + tick
                + ";",
            )
        ),
        html=False,
    )

    visible_french = {
        candidate.strip()
        for candidate in product_copy
        if _looks_french(candidate)
    }
    assert {"Opération refusée", "Échec de sauvegarde"} <= visible_french


def test_static_guard_scans_rendered_css_content_not_comments() -> None:
    product_copy = _static_product_copy(
        '''
        /* content: "Échec seulement dans un commentaire"; */
        .banner::before { content: "Données corrompues"; }
        .icon::after { content: ''; }
        ''',
        html=False,
        css=True,
    )

    assert product_copy == ["Données corrompues", ""]
    assert _looks_french(product_copy[0])


def test_static_guard_mutation_proves_graph_search_and_cloud_ontology_copy() -> None:
    graph_path = ROOT / "services/graph-memory/src/mcp_memory/static/graph.html"
    graph_source = graph_path.read_text(encoding="utf-8")
    graph_mutation = graph_source.replace("🔍 Search", "🔍 Recherche", 1)
    assert graph_mutation != graph_source
    assert any(
        _looks_french(candidate)
        for candidate in _static_product_copy(graph_mutation, html=True)
    )

    ontology_path = ROOT / "services/graph-memory/ONTOLOGIES/cloud.yaml"
    ontology_source = ontology_path.read_text(encoding="utf-8")
    ontology_mutation = ontology_source.replace(
        "Cloud service (IaaS, PaaS, SaaS)",
        "Service cloud (IaaS, PaaS, SaaS)",
        1,
    )
    assert ontology_mutation != ontology_source
    assert _scan_embedded_ontology_values(ontology_path, source=ontology_mutation)


def test_static_guard_scans_visible_markup_copy_not_svg_ids_or_comments() -> None:
    product_copy = _static_product_copy(
        """
        <svg id="Groupe_3264" data-name="Groupe 3264">
          <title>Opération interrompue</title>
          <desc>Échec de la réplication</desc>
          <path aria-label="Données corrompues" data-message="Réessayez plus tard" />
          <style>
            /* Opération seulement dans un commentaire CSS. */
            .status::before { content: "Échec de rendu"; }
          </style>
          <script>
            // Échec seulement dans un commentaire JavaScript.
            const visibleLabel = "Échec de la synchronisation";
          </script>
        </svg>
        <img alt="Échec de sauvegarde" title="Étape suivante" />
        <input placeholder="Nouvelle description" value="Données corrompues" />
        <div data-tooltip="Opération refusée"></div>
        """,
        html=True,
    )

    visible_french = {
        candidate.strip()
        for candidate in product_copy
        if _looks_french(candidate)
    }
    assert {
        "Opération interrompue",
        "Échec de la réplication",
        "Données corrompues",
        "Réessayez plus tard",
        "Échec de sauvegarde",
        "Étape suivante",
        "Nouvelle description",
        "Opération refusée",
        "Échec de la synchronisation",
        "Échec de rendu",
    } <= visible_french
    assert "Groupe 3264" not in product_copy
    assert not any("commentaire" in candidate for candidate in product_copy)
