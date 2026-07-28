"""Public-only loader for reference-compatible Track A task packages."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class TaskPackageError(ValueError):
    """Raised when a public task package violates the supported contract."""


_FORBIDDEN_DIRECTORIES = {
    "answer",
    "golden",
    "hidden",
    "hidden_like",
    "reference",
}
_SAFE_PUBLIC_PATH = re.compile(r"\A[A-Za-z0-9_./+-]+\Z")
_SAFE_C_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_TCL_ATOM = re.compile(r"\A[A-Za-z0-9_.+-]+\Z")
_SUPPORTED_TASK_TYPES = frozenset(
    {
        "generate",
        "repair",
        "optimize",
        "synth_fix",
        # ``structural`` is the existing reference-harness spelling for a
        # structural/CoSim repair task.  Keep it public and explicit rather
        # than silently treating it as an unknown task type.
        "structural",
    }
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _public_path(root: Path, name: object, *, field: str) -> Path:
    if not isinstance(name, str) or not name:
        raise TaskPackageError(f"{field} must be a non-empty relative path")
    if _SAFE_PUBLIC_PATH.fullmatch(name) is None:
        raise TaskPackageError(f"{field} contains unsupported path characters")
    relative = Path(name)
    lowered = {part.casefold() for part in relative.parts}
    if relative.is_absolute() or ".." in relative.parts:
        raise TaskPackageError(f"{field} escapes the task directory: {name}")
    if lowered.intersection(_FORBIDDEN_DIRECTORIES):
        raise TaskPackageError(f"{field} enters a forbidden directory: {name}")
    resolved = (root / relative).resolve()
    try:
        resolved_relative = resolved.relative_to(root)
    except ValueError as exc:
        raise TaskPackageError(f"{field} escapes the task directory: {name}") from exc
    if {part.casefold() for part in resolved_relative.parts}.intersection(
        _FORBIDDEN_DIRECTORIES
    ):
        raise TaskPackageError(f"{field} resolves into a forbidden directory: {name}")
    return resolved


def _read_required(path: Path, *, field: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TaskPackageError(f"cannot read {field}: {path}") from exc


@dataclass(frozen=True)
class PublicTask:
    """Immutable public inputs that may be supplied to the metered tools."""

    directory: Path
    id: str
    task_type: str
    difficulty: int
    difficulty_declared: bool
    generation_required: bool
    top: str
    max_credits: int | None
    max_credits_source: str | None
    max_tokens: int | None
    max_tokens_source: str | None
    part: str
    clock_ns: float
    requires_cosim: bool
    initial_condition: str
    description: str
    kernel_name: str
    kernel_bytes: bytes
    headers: Mapping[str, bytes]
    public_tb_name: str
    public_tb_bytes: bytes
    public_file_hashes: Mapping[str, str]

    @property
    def kernel_code(self) -> str:
        return self.kernel_bytes.decode("utf-8")

    @property
    def kernel_sha256(self) -> str:
        return _sha256(self.kernel_bytes)

    @property
    def budget(self) -> int:
        """Legacy spelling for task packages that still declare ``budget``.

        New run setup must use ``max_credits`` plus the shared budget resolver
        so a missing task limit receives an explicit, recorded development
        fallback instead of silently becoming 40 Credits.
        """

        if self.max_credits is None:
            raise TaskPackageError(
                "task does not declare max_credits (or legacy budget)"
            )
        return self.max_credits


def _optional_non_negative_limit(
    spec: Mapping[str, object],
    name: str,
) -> int | None:
    if name not in spec:
        return None
    raw = spec[name]
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    parsed = int(raw)
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def current_public_file_hashes(task: PublicTask) -> Mapping[str, str]:
    """Re-hash the snapshotted public inputs through the same path guard.

    Re-resolving every path matters: a package entry may have been replaced by
    a symlink after the initial load.  Callers must never reconstruct these
    paths with ``task.directory / name`` directly.
    """

    hashes: dict[str, str] = {}
    for name in task.public_file_hashes:
        path = _public_path(task.directory, name, field=name)
        hashes[name] = _sha256(_read_required(path, field=name))
    return MappingProxyType(hashes)


def load_public_task(task_dir: str | Path) -> PublicTask:
    """Load only task metadata, baseline, headers, description, and public TB.

    The implementation deliberately never probes or reads ``answer/``,
    ``golden/``, ``hidden/``, ``hidden_like/``, or ``reference/``. Those inputs
    belong only to external grading or offline corpus validation.
    """

    supplied_root = Path(task_dir)
    if {part.casefold() for part in supplied_root.parts}.intersection(
        _FORBIDDEN_DIRECTORIES
    ):
        raise TaskPackageError("task directory is inside a forbidden directory")
    root = supplied_root.resolve()
    if {part.casefold() for part in root.parts}.intersection(_FORBIDDEN_DIRECTORIES):
        raise TaskPackageError("task directory resolves inside a forbidden directory")
    if not root.is_dir():
        raise TaskPackageError(f"task directory does not exist: {root}")

    task_toml_path = _public_path(root, "task.toml", field="task.toml")
    task_toml_bytes = _read_required(task_toml_path, field="task.toml")
    try:
        spec = tomllib.loads(task_toml_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TaskPackageError(f"invalid task.toml: {exc}") from exc

    try:
        kernel_name = spec["kernel_file"]
        top = spec["top"]
        public_tb_name = spec["public_tb"]
    except KeyError as exc:
        raise TaskPackageError(f"task.toml is missing {exc.args[0]}") from exc

    if not isinstance(top, str) or _SAFE_C_IDENTIFIER.fullmatch(top) is None:
        raise TaskPackageError("top must be a C identifier without Tcl metacharacters")

    kernel_path = _public_path(root, kernel_name, field="kernel_file")
    public_tb_path = _public_path(root, public_tb_name, field="public_tb")
    header_names = spec.get("header_files", [])
    if not isinstance(header_names, list):
        raise TaskPackageError("header_files must be an array")

    kernel_bytes = _read_required(kernel_path, field="kernel_file")
    public_tb_bytes = _read_required(public_tb_path, field="public_tb")
    headers: dict[str, bytes] = {}
    for header_name in header_names:
        header_path = _public_path(root, header_name, field="header_files")
        headers[str(header_name)] = _read_required(header_path, field="header_files")

    description_path = _public_path(root, "description.md", field="description.md")
    description_exists = description_path.is_file()
    description_bytes = description_path.read_bytes() if description_exists else b""
    try:
        description = description_bytes.decode("utf-8")
        kernel_bytes.decode("utf-8")
        public_tb_bytes.decode("utf-8")
        for content in headers.values():
            content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskPackageError(f"public task source is not UTF-8: {exc}") from exc

    target = spec.get("target", {})
    if not isinstance(target, dict):
        raise TaskPackageError("target must be a TOML table")
    part = (
        os.environ.get("LLM4HLS_PART")
        or target.get("part")
        or "xcu55c-fsvh2892-2L-e"
    )
    if not isinstance(part, str) or _SAFE_TCL_ATOM.fullmatch(part) is None:
        raise TaskPackageError("target part contains Tcl metacharacters")
    clock_ns = os.environ.get("LLM4HLS_CLOCK_NS") or target.get("clock_ns", 5.0)

    public_hashes = {
        "task.toml": _sha256(task_toml_bytes),
        str(kernel_name): _sha256(kernel_bytes),
        str(public_tb_name): _sha256(public_tb_bytes),
    }
    public_hashes.update({name: _sha256(content) for name, content in headers.items()})
    if description_exists:
        public_hashes["description.md"] = _sha256(description_bytes)

    task_type = str(spec.get("task_type", "generate")).strip().lower()
    if task_type not in _SUPPORTED_TASK_TYPES:
        raise TaskPackageError(
            "unsupported task_type "
            f"{task_type!r}; expected one of {sorted(_SUPPORTED_TASK_TYPES)}"
        )
    raw_generation_required = spec.get("generation_required", task_type == "generate")
    if not isinstance(raw_generation_required, bool):
        raise TaskPackageError("generation_required must be boolean when supplied")
    difficulty_declared = "difficulty" in spec
    raw_difficulty = spec.get("difficulty", 1)

    try:
        declared_max_credits = _optional_non_negative_limit(spec, "max_credits")
        legacy_budget = _optional_non_negative_limit(spec, "budget")
        if (
            declared_max_credits is not None
            and legacy_budget is not None
            and declared_max_credits != legacy_budget
        ):
            raise ValueError(
                "max_credits conflicts with legacy budget; declare one limit"
            )
        max_credits = (
            declared_max_credits
            if declared_max_credits is not None
            else legacy_budget
        )
        max_credits_source = (
            "task.toml:max_credits"
            if declared_max_credits is not None
            else "task.toml:budget(legacy)"
            if legacy_budget is not None
            else None
        )
        max_tokens = _optional_non_negative_limit(spec, "max_tokens")
        parsed_clock_ns = float(clock_ns)
        if not math.isfinite(parsed_clock_ns) or parsed_clock_ns <= 0:
            raise ValueError("target clock_ns must be finite and positive")
        return PublicTask(
            directory=root,
            id=str(spec.get("task_id", root.name)),
            task_type=task_type,
            difficulty=int(raw_difficulty),
            difficulty_declared=difficulty_declared,
            generation_required=raw_generation_required,
            top=str(top),
            max_credits=max_credits,
            max_credits_source=max_credits_source,
            max_tokens=max_tokens,
            max_tokens_source=(
                "task.toml:max_tokens" if max_tokens is not None else None
            ),
            part=str(part),
            clock_ns=parsed_clock_ns,
            requires_cosim=bool(spec.get("requires_cosim", False)),
            initial_condition=str(spec.get("initial_condition", "")),
            description=description,
            kernel_name=str(kernel_name),
            kernel_bytes=kernel_bytes,
            headers=MappingProxyType(headers),
            public_tb_name=str(public_tb_name),
            public_tb_bytes=public_tb_bytes,
            public_file_hashes=MappingProxyType(public_hashes),
        )
    except (TypeError, ValueError) as exc:
        raise TaskPackageError(f"invalid task metadata: {exc}") from exc
