"""Deterministic, dependency-free guard for the public HLS top interface.

The guard intentionally implements a small C/C++ declaration parser instead
of pretending that regular expressions can parse an arbitrary translation
unit.  It supports the ordinary free-function declarations used by Track A
tasks and fails closed, with machine-readable reasons, when the protected top
interface cannot be parsed reliably.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .task import PublicTask


_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_RAW_STRING = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\(')
_FUNCTION_NAME_EXCLUSIONS = {
    "__attribute__",
    "__declspec",
    "alignof",
    "alignas",
    "catch",
    "decltype",
    "for",
    "if",
    "noexcept",
    "sizeof",
    "static_assert",
    "switch",
    "while",
}
_TYPE_WORDS = {
    "auto",
    "bool",
    "char",
    "char8_t",
    "char16_t",
    "char32_t",
    "const",
    "double",
    "enum",
    "float",
    "int",
    "long",
    "register",
    "short",
    "signed",
    "struct",
    "typename",
    "union",
    "unsigned",
    "void",
    "volatile",
    "wchar_t",
}
_RETURN_SPECIFIERS = {
    "consteval",
    "constexpr",
    "constinit",
    "explicit",
    "extern",
    "friend",
    "inline",
    "static",
    "virtual",
}


@dataclass(frozen=True)
class ParameterSignature:
    """One positional parameter in a normalized function signature."""

    name: str | None
    type: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FunctionSignature:
    """A normalized free-function declaration or definition."""

    name: str
    qualified_name: str
    return_type: str
    parameters: tuple[ParameterSignature, ...]
    language_linkage: str
    public: bool
    definition: bool
    explicit_extern: bool
    line: int

    @property
    def canonical(self) -> str:
        parameters = ",".join(item.type for item in self.parameters)
        return f"{self.return_type} {self.qualified_name}({parameters})"

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["parameters"] = [item.to_dict() for item in self.parameters]
        value["canonical"] = self.canonical
        return value


@dataclass(frozen=True)
class InterfaceGuardReason:
    """A stable decision reason emitted by :class:`TopInterfaceGuard`."""

    code: str
    message: str
    blocking: bool
    symbol: str | None = None
    expected: object | None = None
    actual: object | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TopInterfaceGuardResult:
    """Structured static-gate result; callers may persist it as candidate risk."""

    allowed: bool
    parse_reliable: bool
    risk_level: str
    high_risk: bool
    requires_cosim: bool
    reasons: tuple[InterfaceGuardReason, ...]
    baseline_top: FunctionSignature | None
    candidate_top: FunctionSignature | None
    required_public_symbols: tuple[str, ...]
    interface_pragmas_before: tuple[str, ...]
    interface_pragmas_after: tuple[str, ...]

    @property
    def blocking_reasons(self) -> tuple[InterfaceGuardReason, ...]:
        return tuple(reason for reason in self.reasons if reason.blocking)

    def failure_summary(self) -> str:
        blocking = self.blocking_reasons
        if not blocking:
            return "top interface guard passed"
        return "; ".join(
            f"{reason.code}: {reason.message}" for reason in blocking
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "parse_reliable": self.parse_reliable,
            "risk_level": self.risk_level,
            "high_risk": self.high_risk,
            "requires_cosim": self.requires_cosim,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "baseline_top": (
                self.baseline_top.to_dict() if self.baseline_top is not None else None
            ),
            "candidate_top": (
                self.candidate_top.to_dict() if self.candidate_top is not None else None
            ),
            "required_public_symbols": list(self.required_public_symbols),
            "interface_pragmas_before": list(self.interface_pragmas_before),
            "interface_pragmas_after": list(self.interface_pragmas_after),
        }


@dataclass(frozen=True)
class _Token:
    value: str
    line: int
    kind: str = "punct"


@dataclass(frozen=True)
class _ParseIssue:
    code: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class _TranslationUnit:
    functions: tuple[FunctionSignature, ...]
    issues: tuple[_ParseIssue, ...]


def _text(value: bytes | str, *, label: str) -> tuple[str | None, _ParseIssue | None]:
    if isinstance(value, str):
        return value, None
    try:
        return bytes(value).decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, _ParseIssue(
            "SOURCE_NOT_UTF8", f"{label} is not valid UTF-8: {exc}"
        )


def _consume_quoted(source: str, start: int, quote_index: int) -> int | None:
    quote = source[quote_index]
    cursor = quote_index + 1
    while cursor < len(source):
        if source[cursor] == "\\":
            cursor += 2
            continue
        if source[cursor] == quote:
            return cursor + 1
        if source[cursor] in "\r\n" and quote == "'":
            return None
        cursor += 1
    return None


def _lex(source: str) -> tuple[list[_Token], list[_ParseIssue]]:
    tokens: list[_Token] = []
    issues: list[_ParseIssue] = []
    cursor = 0
    line = 1
    at_line_start = True
    length = len(source)
    multi_punctuation = ("...", "::", "&&", "||", "->", "[[", "]]", "<=", ">=", "==", "!=")

    while cursor < length:
        char = source[cursor]
        if char in " \t\f\v":
            cursor += 1
            continue
        if char in "\r\n":
            if char == "\r" and cursor + 1 < length and source[cursor + 1] == "\n":
                cursor += 1
            cursor += 1
            line += 1
            at_line_start = True
            continue
        if at_line_start and char == "#":
            cursor += 1
            while cursor < length:
                if source[cursor] == "\n":
                    escaped = cursor > 0 and source[cursor - 1] == "\\"
                    cursor += 1
                    line += 1
                    if not escaped:
                        break
                elif source[cursor] == "\r":
                    escaped = cursor > 0 and source[cursor - 1] == "\\"
                    cursor += 1
                    if cursor < length and source[cursor] == "\n":
                        cursor += 1
                    line += 1
                    if not escaped:
                        break
                else:
                    cursor += 1
            at_line_start = True
            continue
        at_line_start = False
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2)
            cursor = length if newline < 0 else newline
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            if end < 0:
                issues.append(
                    _ParseIssue(
                        "UNTERMINATED_COMMENT",
                        "translation unit contains an unterminated block comment",
                        line,
                    )
                )
                break
            line += source.count("\n", cursor, end + 2)
            cursor = end + 2
            continue

        raw_match = _RAW_STRING.match(source, cursor)
        if raw_match is not None:
            terminator = ")" + raw_match.group(1) + '"'
            end = source.find(terminator, raw_match.end())
            if end < 0:
                issues.append(
                    _ParseIssue(
                        "UNTERMINATED_STRING",
                        "translation unit contains an unterminated raw string",
                        line,
                    )
                )
                break
            end += len(terminator)
            line += source.count("\n", cursor, end)
            tokens.append(_Token("<string>", line, "string"))
            cursor = end
            continue

        quote_match = re.match(r'(?:u8|u|U|L)?(["\'])', source[cursor:])
        if quote_match is not None:
            quote_index = cursor + quote_match.end() - 1
            end = _consume_quoted(source, cursor, quote_index)
            if end is None:
                issues.append(
                    _ParseIssue(
                        "UNTERMINATED_STRING",
                        "translation unit contains an unterminated string or character literal",
                        line,
                    )
                )
                break
            raw_value = source[cursor:end]
            kind = "string" if source[quote_index] == '"' else "character"
            tokens.append(_Token(raw_value, line, kind))
            line += raw_value.count("\n")
            cursor = end
            continue

        identifier = re.match(r"[A-Za-z_][A-Za-z0-9_]*", source[cursor:])
        if identifier is not None:
            value = identifier.group(0)
            tokens.append(_Token(value, line, "identifier"))
            cursor += len(value)
            continue
        number = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)[A-Za-z0-9_.'+-]*", source[cursor:])
        if number is not None:
            value = number.group(0)
            tokens.append(_Token(value, line, "number"))
            cursor += len(value)
            continue
        punctuation = next(
            (item for item in multi_punctuation if source.startswith(item, cursor)),
            None,
        )
        if punctuation is None:
            punctuation = char
        tokens.append(_Token(punctuation, line))
        cursor += len(punctuation)
    return tokens, issues


def _balanced_pairs(
    tokens: list[_Token], opening: str, closing: str
) -> tuple[dict[int, int], list[_ParseIssue]]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    issues: list[_ParseIssue] = []
    for index, token in enumerate(tokens):
        if token.value == opening:
            stack.append(index)
        elif token.value == closing:
            if not stack:
                issues.append(
                    _ParseIssue(
                        "UNBALANCED_DELIMITER",
                        f"unexpected {closing!r}",
                        token.line,
                    )
                )
            else:
                pairs[stack.pop()] = index
    for index in stack:
        issues.append(
            _ParseIssue(
                "UNBALANCED_DELIMITER",
                f"unclosed {opening!r}",
                tokens[index].line,
            )
        )
    return pairs, issues


def _strip_attributes(values: list[str]) -> list[str]:
    output: list[str] = []
    cursor = 0
    while cursor < len(values):
        if values[cursor] == "[[":
            depth = 1
            cursor += 1
            while cursor < len(values) and depth:
                if values[cursor] == "[[":
                    depth += 1
                elif values[cursor] == "]]":
                    depth -= 1
                cursor += 1
            continue
        if values[cursor] in {"__attribute__", "__declspec", "alignas"}:
            cursor += 1
            if cursor < len(values) and values[cursor] == "(":
                depth = 0
                while cursor < len(values):
                    if values[cursor] == "(":
                        depth += 1
                    elif values[cursor] == ")":
                        depth -= 1
                        if depth == 0:
                            cursor += 1
                            break
                    cursor += 1
            continue
        output.append(values[cursor])
        cursor += 1
    return output


def _canonical_tokens(values: Iterable[str]) -> str:
    value = " ".join(values).strip()
    value = re.sub(r"\s*::\s*", "::", value)
    value = re.sub(r"\s*([<>{}\[\](),*&])\s*", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _canonical_type(values: list[str], *, parameter: bool) -> str:
    """Normalize spelling differences that do not change a function type."""

    values = list(values)
    boundary = next(
        (
            index
            for index, value in enumerate(values)
            if value in {"*", "&", "&&", "[", "("}
        ),
        len(values),
    )
    base = values[:boundary]
    suffix = values[boundary:]
    if base and all(_IDENTIFIER.fullmatch(value) for value in base):
        qualifiers = [
            qualifier
            for qualifier in ("const", "volatile")
            if qualifier in base
        ]
        base = [value for value in base if value not in {"const", "volatile"}]
        if parameter and not suffix:
            # Top-level cv on a by-value parameter is not part of the
            # function type. Pointee/referent cv remains in place.
            qualifiers = []
        base = qualifiers + base
    if parameter and suffix:
        # Likewise, top-level cv on the outermost pointer is not part of a
        # function parameter type. Do not remove cv on an inner pointer.
        while suffix and suffix[-1] in {"const", "volatile"}:
            suffix.pop()

    canonical = _canonical_tokens(base + suffix)
    replacements = {
        "signed int": "int",
        "unsigned int": "unsigned",
        "short int": "short",
        "signed short": "short",
        "signed short int": "short",
        "unsigned short int": "unsigned short",
        "long int": "long",
        "signed long": "long",
        "signed long int": "long",
        "unsigned long int": "unsigned long",
        "long long int": "long long",
        "signed long long": "long long",
        "signed long long int": "long long",
        "unsigned long long int": "unsigned long long",
    }
    for old, new in replacements.items():
        if canonical == old:
            return new
        if canonical.startswith(old + "*") or canonical.startswith(old + "&"):
            return new + canonical[len(old) :]
        if canonical.startswith("const " + old):
            return "const " + new + canonical[len("const " + old) :]
        if canonical.startswith("volatile " + old):
            return "volatile " + new + canonical[len("volatile " + old) :]
    return canonical


def _depths(values: list[str]) -> list[tuple[int, int, int]]:
    paren = bracket = angle = 0
    result: list[tuple[int, int, int]] = []
    for value in values:
        result.append((paren, bracket, angle))
        if value == "(":
            paren += 1
        elif value == ")":
            paren = max(0, paren - 1)
        elif value == "[":
            bracket += 1
        elif value == "]":
            bracket = max(0, bracket - 1)
        elif value == "<":
            angle += 1
        elif value == ">":
            angle = max(0, angle - 1)
    return result


def _split_parameters(tokens: list[_Token]) -> tuple[list[list[_Token]] | None, str | None]:
    if not tokens:
        return [], None
    values = [token.value for token in tokens]
    if values == ["void"]:
        return [], None
    parts: list[list[_Token]] = []
    start = 0
    paren = bracket = brace = angle = 0
    for index, token in enumerate(tokens):
        value = token.value
        if value == "(":
            paren += 1
        elif value == ")":
            paren -= 1
        elif value == "[":
            bracket += 1
        elif value == "]":
            bracket -= 1
        elif value == "{":
            brace += 1
        elif value == "}":
            brace -= 1
        elif value == "<":
            angle += 1
        elif value == ">":
            angle = max(0, angle - 1)
        elif value == "," and paren == bracket == brace == angle == 0:
            if index == start:
                return None, "empty parameter before comma"
            parts.append(tokens[start:index])
            start = index + 1
        if min(paren, bracket, brace, angle) < 0:
            return None, "unbalanced parameter declarator"
    if paren or bracket or brace or angle:
        return None, "unbalanced parameter declarator"
    if start == len(tokens):
        return None, "trailing comma in parameter list"
    parts.append(tokens[start:])
    return parts, None


def _without_default(tokens: list[_Token]) -> list[_Token]:
    paren = bracket = brace = angle = 0
    for index, token in enumerate(tokens):
        value = token.value
        if value == "=" and paren == bracket == brace == angle == 0:
            return tokens[:index]
        if value == "(":
            paren += 1
        elif value == ")":
            paren = max(0, paren - 1)
        elif value == "[":
            bracket += 1
        elif value == "]":
            bracket = max(0, bracket - 1)
        elif value == "{":
            brace += 1
        elif value == "}":
            brace = max(0, brace - 1)
        elif value == "<":
            angle += 1
        elif value == ">":
            angle = max(0, angle - 1)
    return tokens


def _parameter_signature(tokens: list[_Token]) -> tuple[ParameterSignature | None, str | None]:
    tokens = _without_default(tokens)
    values = _strip_attributes([token.value for token in tokens])
    if not values:
        return None, "empty parameter declarator"
    if values == ["..."]:
        return ParameterSignature(None, "..."), None

    depths = _depths(values)
    name_index: int | None = None
    # Function-pointer parameters put the identifier inside ``(*name)``.
    for index in range(1, len(values) - 1):
        if (
            _IDENTIFIER.fullmatch(values[index])
            and values[index - 1] in {"*", "&", "&&"}
            and values[index + 1] == ")"
        ):
            name_index = index
    if name_index is None:
        candidates = [
            index
            for index, value in enumerate(values)
            if (
                _IDENTIFIER.fullmatch(value)
                and value not in _TYPE_WORDS
                and depths[index] == (0, 0, 0)
                and (index == 0 or values[index - 1] != "::")
                and (index + 1 == len(values) or values[index + 1] != "::")
            )
        ]
        if candidates:
            candidate = candidates[-1]
            other_type_material = [
                value
                for index, value in enumerate(values)
                if index != candidate and value not in {"const", "volatile", "register"}
            ]
            if other_type_material:
                name_index = candidate

    name = values[name_index] if name_index is not None else None
    if name_index is not None:
        before = values[:name_index]
        after = values[name_index + 1 :]
        # C/C++ adjust an outer array parameter to a pointer. Preserve any
        # additional dimensions because they remain part of the pointee type.
        if after and after[0] == "[":
            depth = 0
            closing = None
            for index, value in enumerate(after):
                if value == "[":
                    depth += 1
                elif value == "]":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing is None:
                return None, "unbalanced array parameter"
            values = before + ["*"] + after[closing + 1 :]
        else:
            values = before + after
    values = [value for value in values if value != "register"]
    normalized = _canonical_type(values, parameter=True)
    if not normalized:
        return None, "parameter type is empty"
    return ParameterSignature(name, normalized), None


def _function_from_chunk(
    chunk: list[_Token],
    *,
    definition: bool,
    inherited_linkage: str,
    scope: tuple[str, ...],
    inherited_public: bool,
) -> tuple[FunctionSignature | None, _ParseIssue | None]:
    if not chunk:
        return None, None
    values = [token.value for token in chunk]
    depth = 0
    pairs: list[tuple[int, int]] = []
    opening: int | None = None
    for index, value in enumerate(values):
        if value == "(":
            if depth == 0:
                opening = index
            depth += 1
        elif value == ")":
            depth -= 1
            if depth < 0:
                return None, _ParseIssue(
                    "FUNCTION_SIGNATURE_UNCERTAIN",
                    "function-like declaration has an unexpected ')'",
                    chunk[index].line,
                )
            if depth == 0 and opening is not None:
                pairs.append((opening, index))
                opening = None
    if depth:
        return None, _ParseIssue(
            "FUNCTION_SIGNATURE_UNCERTAIN",
            "function-like declaration has unbalanced parentheses",
            chunk[0].line,
        )

    selected: tuple[int, int] | None = None
    for open_index, close_index in pairs:
        if open_index == 0:
            continue
        name = values[open_index - 1]
        if (
            _IDENTIFIER.fullmatch(name)
            and name not in _FUNCTION_NAME_EXCLUSIONS
            and name not in _TYPE_WORDS
        ):
            selected = (open_index, close_index)
    if selected is None:
        return None, None
    open_index, close_index = selected
    name_index = open_index - 1
    name = values[name_index]
    prefix = values[:name_index]
    if "=" in prefix or not prefix:
        return None, None

    explicit_extern = "extern" in prefix
    linkage = inherited_linkage
    for index, value in enumerate(prefix[:-1]):
        if value == "extern" and prefix[index + 1] in {'"C"', "u8\"C\""}:
            linkage = "C"
    public = inherited_public and "static" not in prefix
    stripped_prefix = _strip_attributes(prefix)
    stripped_prefix = [
        value
        for value in stripped_prefix
        if value not in _RETURN_SPECIFIERS and value not in {'"C"', "u8\"C\""}
    ]
    # A template top is outside the intentionally supported ABI subset.
    if "template" in stripped_prefix:
        return None, _ParseIssue(
            "FUNCTION_SIGNATURE_UNCERTAIN",
            f"template function {name!r} is not supported by the static interface parser",
            chunk[name_index].line,
        )
    return_type = _canonical_type(stripped_prefix, parameter=False)
    if not return_type:
        return None, _ParseIssue(
            "FUNCTION_SIGNATURE_UNCERTAIN",
            f"cannot determine return type for {name!r}",
            chunk[name_index].line,
        )

    parameter_parts, parameter_error = _split_parameters(
        chunk[open_index + 1 : close_index]
    )
    if parameter_error is not None or parameter_parts is None:
        return None, _ParseIssue(
            "FUNCTION_SIGNATURE_UNCERTAIN",
            f"cannot parse parameters for {name!r}: {parameter_error}",
            chunk[name_index].line,
        )
    parameters: list[ParameterSignature] = []
    for index, part in enumerate(parameter_parts):
        parameter, error = _parameter_signature(part)
        if error is not None or parameter is None:
            return None, _ParseIssue(
                "FUNCTION_SIGNATURE_UNCERTAIN",
                f"cannot parse parameter {index + 1} for {name!r}: {error}",
                chunk[name_index].line,
            )
        parameters.append(parameter)
    qualified_name = "::".join((*scope, name)) if scope else name
    return (
        FunctionSignature(
            name=name,
            qualified_name=qualified_name,
            return_type=return_type,
            parameters=tuple(parameters),
            language_linkage=linkage,
            public=public,
            definition=definition,
            explicit_extern=explicit_extern,
            line=chunk[name_index].line,
        ),
        None,
    )


def _scope_kind(chunk: list[_Token]) -> tuple[str, str | None]:
    values = [token.value for token in chunk]
    if len(values) >= 2 and values[-2:] == ["extern", '"C"']:
        return "extern_c", None
    if values and values[0] == "namespace":
        if len(values) == 1:
            return "anonymous_namespace", None
        if len(values) == 2 and _IDENTIFIER.fullmatch(values[1]):
            return "namespace", values[1]
    return "other", None


def _parse_translation_unit(source: str) -> _TranslationUnit:
    tokens, issues = _lex(source)
    brace_pairs, brace_issues = _balanced_pairs(tokens, "{", "}")
    _paren_pairs, paren_issues = _balanced_pairs(tokens, "(", ")")
    _bracket_pairs, bracket_issues = _balanced_pairs(tokens, "[", "]")
    issues.extend(brace_issues)
    issues.extend(paren_issues)
    issues.extend(bracket_issues)
    if issues:
        return _TranslationUnit((), tuple(issues))

    functions: list[FunctionSignature] = []

    def parse_scope(
        start: int,
        end: int,
        *,
        linkage: str,
        scope: tuple[str, ...],
        public: bool,
    ) -> None:
        statement_start = start
        cursor = start
        while cursor < end:
            value = tokens[cursor].value
            if value == "{":
                closing = brace_pairs[cursor]
                chunk = tokens[statement_start:cursor]
                signature, issue = _function_from_chunk(
                    chunk,
                    definition=True,
                    inherited_linkage=linkage,
                    scope=scope,
                    inherited_public=public,
                )
                if issue is not None:
                    issues.append(issue)
                if signature is not None:
                    functions.append(signature)
                else:
                    kind, name = _scope_kind(chunk)
                    if kind == "extern_c":
                        parse_scope(
                            cursor + 1,
                            closing,
                            linkage="C",
                            scope=scope,
                            public=public,
                        )
                    elif kind == "namespace":
                        assert name is not None
                        parse_scope(
                            cursor + 1,
                            closing,
                            linkage=linkage,
                            scope=(*scope, name),
                            public=public,
                        )
                    elif kind == "anonymous_namespace":
                        parse_scope(
                            cursor + 1,
                            closing,
                            linkage=linkage,
                            scope=scope,
                            public=False,
                        )
                cursor = closing + 1
                statement_start = cursor
                continue
            if value == ";":
                signature, issue = _function_from_chunk(
                    tokens[statement_start:cursor],
                    definition=False,
                    inherited_linkage=linkage,
                    scope=scope,
                    inherited_public=public,
                )
                if issue is not None:
                    issues.append(issue)
                if signature is not None:
                    functions.append(signature)
                statement_start = cursor + 1
            cursor += 1

    parse_scope(0, len(tokens), linkage="C++", scope=(), public=True)
    return _TranslationUnit(tuple(functions), tuple(issues))


def _comment_mask(source: str) -> tuple[str, _ParseIssue | None]:
    output = list(source)
    cursor = 0
    while cursor < len(source):
        raw_match = _RAW_STRING.match(source, cursor)
        if raw_match is not None:
            terminator = ")" + raw_match.group(1) + '"'
            end = source.find(terminator, raw_match.end())
            if end < 0:
                return source, _ParseIssue(
                    "UNTERMINATED_STRING",
                    "translation unit contains an unterminated raw string",
                    source.count("\n", 0, cursor) + 1,
                )
            cursor = end + len(terminator)
            continue
        quote_match = re.match(r'(?:u8|u|U|L)?(["\'])', source[cursor:])
        if quote_match is not None:
            quote_index = cursor + quote_match.end() - 1
            end = _consume_quoted(source, cursor, quote_index)
            if end is None:
                return source, _ParseIssue(
                    "UNTERMINATED_STRING",
                    "translation unit contains an unterminated string or character literal",
                    source.count("\n", 0, cursor) + 1,
                )
            cursor = end
            continue
        if source.startswith("//", cursor):
            end = source.find("\n", cursor + 2)
            end = len(source) if end < 0 else end
            for index in range(cursor, end):
                output[index] = " "
            cursor = end
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            if end < 0:
                return source, _ParseIssue(
                    "UNTERMINATED_COMMENT",
                    "translation unit contains an unterminated block comment",
                    source.count("\n", 0, cursor) + 1,
                )
            for index in range(cursor, end + 2):
                if output[index] not in "\r\n":
                    output[index] = " "
            cursor = end + 2
            continue
        cursor += 1
    return "".join(output), None


def _interface_pragmas(source: str) -> tuple[tuple[str, ...], _ParseIssue | None]:
    masked, issue = _comment_mask(source)
    if issue is not None:
        return (), issue
    physical = masked.splitlines()
    logical: list[str] = []
    pending = ""
    for line in physical:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        return (), _ParseIssue(
            "INTERFACE_PRAGMA_PARSE_UNCERTAIN",
            "translation unit ends inside a continued preprocessor directive",
        )
    pragmas: list[str] = []
    pattern = re.compile(r"^\s*#\s*pragma\s+hls\s+interface\b(.*)$", re.IGNORECASE)
    for line in logical:
        match = pattern.match(line)
        if match is None:
            continue
        arguments = re.sub(r"\s*=\s*", "=", match.group(1).strip())
        arguments = re.sub(r"\s+", " ", arguments)
        pragmas.append(
            "#pragma HLS INTERFACE" + (f" {arguments}" if arguments else "")
        )
    return tuple(sorted(pragmas)), None


def _same_function_type(left: FunctionSignature, right: FunctionSignature) -> bool:
    return (
        left.return_type == right.return_type
        and tuple(item.type for item in left.parameters)
        == tuple(item.type for item in right.parameters)
        and left.language_linkage == right.language_linkage
    )


def _unique_top(
    unit: _TranslationUnit, top: str
) -> tuple[FunctionSignature | None, str | None]:
    definitions = [
        function
        for function in unit.functions
        if function.definition and function.qualified_name == top
    ]
    if not definitions:
        return None, "missing"
    identities = {
        (
            function.return_type,
            tuple(item.type for item in function.parameters),
            function.language_linkage,
            function.public,
        )
        for function in definitions
    }
    if len(definitions) != 1 or len(identities) != 1:
        return None, "ambiguous"
    return definitions[0], None


def _normalize_changed_path(value: str) -> str | None:
    if "\\" in value:
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


class TopInterfaceGuard:
    """Compare a candidate kernel with the immutable public interface contract."""

    def __init__(
        self,
        *,
        top: str,
        kernel_name: str,
        baseline_source: bytes | str,
        headers: Mapping[str, bytes | str] | None = None,
        public_tb_name: str | None = None,
    ) -> None:
        if _IDENTIFIER.fullmatch(top) is None:
            raise ValueError("top must be a C identifier")
        if not kernel_name:
            raise ValueError("kernel_name must not be empty")
        self.top = top
        self.kernel_name = kernel_name
        self.baseline_source = baseline_source
        self.headers = dict(headers or {})
        self.public_tb_name = public_tb_name

    @classmethod
    def from_task(cls, task: PublicTask) -> "TopInterfaceGuard":
        return cls(
            top=task.top,
            kernel_name=task.kernel_name,
            baseline_source=task.kernel_bytes,
            headers=task.headers,
            public_tb_name=task.public_tb_name,
        )

    def check(
        self,
        candidate_source: bytes | str,
        *,
        changed_files: Iterable[str] | None = None,
    ) -> TopInterfaceGuardResult:
        reasons: list[InterfaceGuardReason] = []
        changed = (
            (self.kernel_name,)
            if changed_files is None
            else tuple(changed_files)
        )
        normalized_changed: list[str] = []
        for raw_path in changed:
            normalized = _normalize_changed_path(str(raw_path))
            if normalized is None or normalized != self.kernel_name:
                reasons.append(
                    InterfaceGuardReason(
                        code="PROTECTED_FILE_CHANGED",
                        message=(
                            f"only {self.kernel_name!r} may change; received {raw_path!r}"
                        ),
                        blocking=True,
                        expected=self.kernel_name,
                        actual=str(raw_path),
                    )
                )
            elif normalized not in normalized_changed:
                normalized_changed.append(normalized)
        if self.kernel_name not in normalized_changed:
            reasons.append(
                InterfaceGuardReason(
                    code="KERNEL_CHANGE_MISSING",
                    message="the guarded change set does not contain the kernel source",
                    blocking=True,
                    expected=self.kernel_name,
                    actual=list(changed),
                )
            )

        baseline_text, baseline_decode_issue = _text(
            self.baseline_source, label="baseline kernel"
        )
        candidate_text, candidate_decode_issue = _text(
            candidate_source, label="candidate kernel"
        )
        baseline_unit = (
            _parse_translation_unit(baseline_text)
            if baseline_text is not None
            else _TranslationUnit((), (baseline_decode_issue,))  # type: ignore[arg-type]
        )
        candidate_unit = (
            _parse_translation_unit(candidate_text)
            if candidate_text is not None
            else _TranslationUnit((), (candidate_decode_issue,))  # type: ignore[arg-type]
        )
        parse_issues = [
            ("baseline", issue) for issue in baseline_unit.issues
        ] + [("candidate", issue) for issue in candidate_unit.issues]
        for side, issue in parse_issues:
            reasons.append(
                InterfaceGuardReason(
                    code="PARSE_UNCERTAIN",
                    message=(
                        f"{side} signature parse is unreliable: {issue.code}: "
                        f"{issue.message}"
                        + (f" (line {issue.line})" if issue.line is not None else "")
                    ),
                    blocking=True,
                    actual={
                        "side": side,
                        "parser_code": issue.code,
                        "line": issue.line,
                    },
                )
            )

        baseline_top, baseline_top_state = _unique_top(baseline_unit, self.top)
        candidate_top, candidate_top_state = _unique_top(candidate_unit, self.top)
        if baseline_top_state == "missing":
            reasons.append(
                InterfaceGuardReason(
                    code="BASELINE_TOP_MISSING",
                    message=f"baseline does not define configured top {self.top!r}",
                    blocking=True,
                    symbol=self.top,
                )
            )
        elif baseline_top_state == "ambiguous":
            reasons.append(
                InterfaceGuardReason(
                    code="BASELINE_TOP_AMBIGUOUS",
                    message=f"baseline has multiple definitions of top {self.top!r}",
                    blocking=True,
                    symbol=self.top,
                )
            )
        if candidate_top_state == "missing":
            reasons.append(
                InterfaceGuardReason(
                    code="TOP_FUNCTION_MISSING",
                    message=f"candidate does not define configured top {self.top!r}",
                    blocking=True,
                    symbol=self.top,
                )
            )
        elif candidate_top_state == "ambiguous":
            reasons.append(
                InterfaceGuardReason(
                    code="TOP_FUNCTION_AMBIGUOUS",
                    message=f"candidate has multiple definitions of top {self.top!r}",
                    blocking=True,
                    symbol=self.top,
                )
            )

        if baseline_top is not None and not baseline_top.public:
            reasons.append(
                InterfaceGuardReason(
                    code="BASELINE_TOP_NOT_PUBLIC",
                    message="configured baseline top does not have public linkage",
                    blocking=True,
                    symbol=self.top,
                )
            )
        if baseline_top is not None and candidate_top is not None:
            if baseline_top.return_type != candidate_top.return_type:
                reasons.append(
                    InterfaceGuardReason(
                        code="TOP_RETURN_TYPE_CHANGED",
                        message="top return type changed",
                        blocking=True,
                        symbol=self.top,
                        expected=baseline_top.return_type,
                        actual=candidate_top.return_type,
                    )
                )
            if len(baseline_top.parameters) != len(candidate_top.parameters):
                reasons.append(
                    InterfaceGuardReason(
                        code="TOP_PARAMETER_COUNT_CHANGED",
                        message="top parameter count changed",
                        blocking=True,
                        symbol=self.top,
                        expected=len(baseline_top.parameters),
                        actual=len(candidate_top.parameters),
                    )
                )
            else:
                for index, (expected, actual) in enumerate(
                    zip(baseline_top.parameters, candidate_top.parameters), start=1
                ):
                    if expected.type != actual.type:
                        reasons.append(
                            InterfaceGuardReason(
                                code="TOP_PARAMETER_TYPE_CHANGED",
                                message=f"top parameter {index} type changed",
                                blocking=True,
                                symbol=self.top,
                                expected=expected.type,
                                actual=actual.type,
                            )
                        )
            if baseline_top.language_linkage != candidate_top.language_linkage:
                reasons.append(
                    InterfaceGuardReason(
                        code="TOP_LANGUAGE_LINKAGE_CHANGED",
                        message="top C/C++ language linkage changed",
                        blocking=True,
                        symbol=self.top,
                        expected=baseline_top.language_linkage,
                        actual=candidate_top.language_linkage,
                    )
                )
            if baseline_top.public != candidate_top.public:
                reasons.append(
                    InterfaceGuardReason(
                        code="TOP_VISIBILITY_CHANGED",
                        message="top public/static visibility changed",
                        blocking=True,
                        symbol=self.top,
                        expected=baseline_top.public,
                        actual=candidate_top.public,
                    )
                )

        header_functions: list[FunctionSignature] = []
        for header_name in sorted(self.headers):
            header_text, decode_issue = _text(
                self.headers[header_name], label=f"header {header_name}"
            )
            if decode_issue is not None or header_text is None:
                issue = decode_issue or _ParseIssue(
                    "SOURCE_NOT_UTF8", f"header {header_name} cannot be decoded"
                )
                reasons.append(
                    InterfaceGuardReason(
                        code="PARSE_UNCERTAIN",
                        message=f"public header parse is unreliable: {issue.message}",
                        blocking=True,
                        actual={"side": "header", "file": header_name},
                    )
                )
                continue
            header_unit = _parse_translation_unit(header_text)
            for issue in header_unit.issues:
                reasons.append(
                    InterfaceGuardReason(
                        code="PARSE_UNCERTAIN",
                        message=(
                            f"public header {header_name!r} parse is unreliable: "
                            f"{issue.code}: {issue.message}"
                        ),
                        blocking=True,
                        actual={
                            "side": "header",
                            "file": header_name,
                            "parser_code": issue.code,
                            "line": issue.line,
                        },
                    )
                )
            header_functions.extend(header_unit.functions)

        if baseline_top is not None:
            header_tops = [
                function
                for function in header_functions
                if function.qualified_name == self.top
            ]
            for header_top in header_tops:
                if not _same_function_type(baseline_top, header_top):
                    reasons.append(
                        InterfaceGuardReason(
                            code="BASELINE_HEADER_TOP_MISMATCH",
                            message="baseline top definition disagrees with its public header",
                            blocking=True,
                            symbol=self.top,
                            expected=header_top.canonical,
                            actual=baseline_top.canonical,
                        )
                    )

        baseline_public_definitions = [
            function
            for function in baseline_unit.functions
            if function.definition and function.public
        ]
        required: list[FunctionSignature] = list(baseline_public_definitions)
        for function in baseline_unit.functions:
            if function.explicit_extern and all(
                not (
                    existing.qualified_name == function.qualified_name
                    and _same_function_type(existing, function)
                )
                for existing in required
            ):
                required.append(function)
        # Header declarations become required only when the baseline actually
        # implements that public symbol; unrelated library declarations do not.
        for header_function in header_functions:
            if any(
                baseline.qualified_name == header_function.qualified_name
                and _same_function_type(baseline, header_function)
                for baseline in baseline_public_definitions
            ) and all(
                not (
                    existing.qualified_name == header_function.qualified_name
                    and _same_function_type(existing, header_function)
                )
                for existing in required
            ):
                required.append(header_function)

        required_labels = tuple(
            sorted(
                {
                    f"{function.language_linkage}:{function.canonical}"
                    for function in required
                }
            )
        )
        candidate_by_name: dict[str, list[FunctionSignature]] = {}
        for function in candidate_unit.functions:
            candidate_by_name.setdefault(function.qualified_name, []).append(function)
        for required_function in sorted(
            required,
            key=lambda item: (item.qualified_name, item.canonical, item.language_linkage),
        ):
            candidates = candidate_by_name.get(required_function.qualified_name, [])
            compatible = [
                function
                for function in candidates
                if _same_function_type(required_function, function)
                and function.public
                and (not required_function.definition or function.definition)
            ]
            if compatible:
                continue
            if not candidates:
                reasons.append(
                    InterfaceGuardReason(
                        code="REQUIRED_PUBLIC_SYMBOL_MISSING",
                        message=(
                            f"required public/extern symbol "
                            f"{required_function.qualified_name!r} is missing"
                        ),
                        blocking=True,
                        symbol=required_function.qualified_name,
                        expected=required_function.canonical,
                    )
                )
            else:
                reasons.append(
                    InterfaceGuardReason(
                        code="REQUIRED_PUBLIC_SYMBOL_CHANGED",
                        message=(
                            f"required public/extern symbol "
                            f"{required_function.qualified_name!r} changed signature, "
                            "linkage, visibility, or definition status"
                        ),
                        blocking=True,
                        symbol=required_function.qualified_name,
                        expected=required_function.to_dict(),
                        actual=[function.to_dict() for function in candidates],
                    )
                )

        before_pragmas, before_pragma_issue = (
            _interface_pragmas(baseline_text) if baseline_text is not None else ((), None)
        )
        after_pragmas, after_pragma_issue = (
            _interface_pragmas(candidate_text) if candidate_text is not None else ((), None)
        )
        for side, issue in (
            ("baseline", before_pragma_issue),
            ("candidate", after_pragma_issue),
        ):
            if issue is not None:
                reasons.append(
                    InterfaceGuardReason(
                        code="PARSE_UNCERTAIN",
                        message=(
                            f"{side} interface pragma parse is unreliable: "
                            f"{issue.code}: {issue.message}"
                        ),
                        blocking=True,
                        actual={"side": side, "parser_code": issue.code},
                    )
                )
        interface_changed = Counter(before_pragmas) != Counter(after_pragmas)
        if interface_changed:
            reasons.append(
                InterfaceGuardReason(
                    code="INTERFACE_PRAGMA_CHANGED",
                    message=(
                        "HLS INTERFACE pragma set changed; force high-risk validation"
                    ),
                    blocking=False,
                    symbol=self.top,
                    expected=list(before_pragmas),
                    actual=list(after_pragmas),
                )
            )

        blocking = tuple(reason for reason in reasons if reason.blocking)
        parse_reliable = not any(
            reason.code in {
                "PARSE_UNCERTAIN",
                "BASELINE_TOP_MISSING",
                "BASELINE_TOP_AMBIGUOUS",
            }
            for reason in reasons
        )
        return TopInterfaceGuardResult(
            allowed=not blocking,
            parse_reliable=parse_reliable,
            risk_level="HIGH" if interface_changed else "LOW",
            high_risk=interface_changed,
            requires_cosim=interface_changed,
            reasons=tuple(reasons),
            baseline_top=baseline_top,
            candidate_top=candidate_top,
            required_public_symbols=required_labels,
            interface_pragmas_before=before_pragmas,
            interface_pragmas_after=after_pragmas,
        )
