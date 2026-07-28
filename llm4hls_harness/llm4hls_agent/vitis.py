"""Vitis 2025.2 execution backend and deterministic report parsers."""

from __future__ import annotations

import math
import os
import shlex
import signal
import hashlib
import json
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .task import PublicTask
from .tools import BackendResult, ToolConfig
from .v3_evidence import parse_csynth_loop_evidence


_RESOURCES = ("LUT", "FF", "DSP", "BRAM_18K", "URAM")


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def parse_synth_report(path: str | Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    performance = root.find("PerformanceEstimates")
    timing = (
        performance.find("SummaryOfTimingAnalysis")
        if performance is not None
        else None
    )
    latency = (
        performance.find("SummaryOfOverallLatency")
        if performance is not None
        else None
    )
    clock_text = timing.findtext("EstimatedClockPeriod") if timing is not None else None
    estimated_clock = float(clock_text) if clock_text else None
    if estimated_clock is not None and (
        not math.isfinite(estimated_clock) or estimated_clock <= 0
    ):
        raise ValueError("EstimatedClockPeriod must be finite and positive")

    def latency_value(tag: str) -> int | None:
        return _to_int(latency.findtext(tag)) if latency is not None else None

    area = root.find("AreaEstimates")
    resources_element = area.find("Resources") if area is not None else None
    available_element = area.find("AvailableResources") if area is not None else None
    resources = {
        name: (
            _to_int(resources_element.findtext(name))
            if resources_element is not None
            else None
        )
        for name in _RESOURCES
    }
    available = {
        name: (
            _to_int(available_element.findtext(name))
            if available_element is not None
            else None
        )
        for name in _RESOURCES
    }
    utilization = {
        name: (
            round(100.0 * resources[name] / available[name], 3)
            if resources[name] is not None and available[name]
            else None
        )
        for name in _RESOURCES
    }
    loop_evidence = parse_csynth_loop_evidence(path)
    return {
        "estimated_clock_period_ns": estimated_clock,
        "latency": {
            "best": latency_value("Best-caseLatency"),
            "average": latency_value("Average-caseLatency"),
            "worst": latency_value("Worst-caseLatency"),
        },
        "interval": {
            "min": latency_value("Interval-min"),
            "max": latency_value("Interval-max"),
        },
        "loop_evidence": loop_evidence,
        "resources": resources,
        "available_resources": available,
        "utilization_percent": utilization,
    }


def parse_cosim_report(path: str | Path) -> dict[str, object] | None:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if (
            len(cells) >= 6
            and cells[1] in {"Verilog", "VHDL"}
            and cells[2] != "NA"
        ):
            return {
                "status": cells[2],
                "latency": {
                    "min": _to_int(cells[3]),
                    "average": _to_int(cells[4]),
                    "max": _to_int(cells[5]),
                },
            }
    return None


@dataclass(frozen=True)
class ProcessResult:
    return_code: int
    stdout: str
    stderr: str
    elapsed_s: float
    timed_out: bool


@dataclass(frozen=True)
class VitisToolchain:
    """Selected public Vitis entry point and its auditable preflight facts."""

    executable: str | None
    invocation_mode: str | None
    selection_source: str | None
    vitis_root: str
    executable_sha256: str | None
    version: str | None
    version_summary: str | None
    preflight_result: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "v3.vitis-toolchain-receipt.v1",
            "selected_executable": self.executable,
            "selected_executable_sha256": self.executable_sha256,
            "selection_source": self.selection_source,
            "invocation_mode": self.invocation_mode,
            "version": self.version,
            "version_summary": self.version_summary,
            "vitis_root": self.vitis_root,
            "preflight_result": self.preflight_result,
        }


def _default_version_probe(executable: str) -> tuple[str | None, str | None]:
    """Return a bounded, non-secret version summary without failing execution."""

    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if completed.returncode != 0:
        return None, None
    lines = [
        " ".join(line.split())[:512]
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    ]
    if not lines:
        return None, None
    summary = " | ".join(lines[:2])
    import re

    match = re.search(r"(?<!\d)(20\d{2}\.\d+(?:\.\d+)?)(?!\d)", summary)
    return (match.group(1) if match else None), summary


def detect_vitis_toolchain(
    vitis_root: str | Path,
    *,
    path_lookup: Callable[[str], str | None] = shutil.which,
    version_probe: Callable[[str], tuple[str | None, str | None]] = _default_version_probe,
) -> VitisToolchain:
    """Select Vitis in 2025.2-first order without requiring ``vitis_hls``.

    The official 2025.2 harness uses ``vitis-run --mode hls --tcl``.  Older
    ``vitis_hls`` remains a compatibility fallback only.
    """

    root = Path(vitis_root).expanduser()
    candidates = (
        (root / "bin" / "vitis-run", "vitis-run", "root_bin_vitis_run"),
        (path_lookup("vitis-run"), "vitis-run", "path_vitis_run"),
        (root / "bin" / "vitis_hls", "vitis_hls", "root_bin_vitis_hls"),
        (path_lookup("vitis_hls"), "vitis_hls", "path_vitis_hls"),
    )
    for raw_path, mode, source in candidates:
        if raw_path is None:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        resolved = str(path.resolve())
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        version, summary = version_probe(resolved)
        return VitisToolchain(
            executable=resolved,
            invocation_mode=mode,
            selection_source=source,
            vitis_root=str(root),
            executable_sha256=digest,
            version=version,
            version_summary=summary,
            preflight_result="READY",
        )
    return VitisToolchain(
        executable=None,
        invocation_mode=None,
        selection_source=None,
        vitis_root=str(root),
        executable_sha256=None,
        version=None,
        version_summary=None,
        preflight_result="TOOLCHAIN_UNAVAILABLE",
    )


def vitis_invocation_command(
    toolchain: VitisToolchain, *, tcl_file: str = "run_hls.tcl"
) -> list[str]:
    """Build the version-appropriate command without invoking the shell."""

    if toolchain.preflight_result not in {"READY", "INJECTED_RUNNER"}:
        raise ValueError("Vitis toolchain is unavailable")
    if not toolchain.executable or not toolchain.invocation_mode:
        raise ValueError("Vitis toolchain selection is incomplete")
    if toolchain.invocation_mode == "vitis-run":
        return [toolchain.executable, "--mode", "hls", "--tcl", tcl_file]
    if toolchain.invocation_mode == "vitis_hls":
        return [toolchain.executable, "-f", tcl_file]
    raise ValueError("unsupported Vitis invocation mode")


class ProcessRunner(Protocol):
    def run(
        self, command: list[str], *, cwd: Path, timeout_s: float
    ) -> ProcessResult: ...


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "utf-16", "utf-16le"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class SubprocessRunner:
    """Run one process group and guarantee bounded termination."""

    def run(
        self, command: list[str], *, cwd: Path, timeout_s: float
    ) -> ProcessResult:
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        stdout, stderr = process.communicate(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            stdout, stderr = process.communicate(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            stdout, stderr = b"", b"process tree did not close pipes"
            else:
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                    creationflags=creation_flags,
                )
                try:
                    stdout, stderr = process.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        stdout, stderr = process.communicate(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        stdout, stderr = b"", b"process tree did not close pipes"
            return ProcessResult(
                -1,
                _decode(stdout),
                _decode(stderr),
                time.monotonic() - started,
                True,
            )
        return ProcessResult(
            process.returncode,
            _decode(stdout),
            _decode(stderr),
            time.monotonic() - started,
            process.returncode in {124, 137},
        )


def _artifact_ref(path: Path, work_dir: Path) -> str:
    return str(path.relative_to(work_dir.parent)).replace("\\", "/")


def _log_evidence(result: ProcessResult) -> list[str]:
    evidence = [f"return_code={result.return_code}"]
    if result.timed_out:
        evidence.append("subprocess timeout expired")
    combined = (result.stdout + "\n" + result.stderr).strip()
    if combined:
        lines = [
            " ".join(line.strip().split())[:512]
            for line in combined.splitlines()
            if line.strip()
        ]
        diagnostic_tokens = (
            "error",
            "fatal",
            "deadlock",
            "blocked",
            "fifo",
            "stream",
            "timeout",
            "timed out",
            "mismatch",
            "failed",
        )
        relevant = [
            line
            for line in lines
            if any(token in line.casefold() for token in diagnostic_tokens)
        ]
        evidence.extend((relevant or lines)[-12:])
    return evidence


class VitisBackend:
    """Raw Vitis adapter for Agent ``ToolServer`` or frozen final certification.

    Search-time calls must go through ``ToolServer`` and its Agent Ledger.  The
    post-search certification runner is the only direct caller and records its
    own receipt outside that Ledger.
    """

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()
        self._uses_default_runner = runner is None
        self._toolchains: dict[str, VitisToolchain] = {}

    def fingerprint(self) -> str:
        """Stable cache identity; bump when command/report semantics change."""

        return "llm4hls_agent.vitis.VitisBackend:v0.8"

    def _toolchain(self, config: ToolConfig) -> VitisToolchain:
        """Cache real detection; injected test runners stay deterministic."""

        key = str(Path(config.vitis_root).expanduser())
        if self._uses_default_runner:
            if key not in self._toolchains:
                self._toolchains[key] = detect_vitis_toolchain(config.vitis_root)
            return self._toolchains[key]
        return VitisToolchain(
            executable="vitis-run",
            invocation_mode="vitis-run",
            selection_source="injected_runner",
            vitis_root=key,
            executable_sha256=None,
            version=None,
            version_summary=None,
            preflight_result="INJECTED_RUNNER",
        )

    def run(
        self,
        kind: str,
        *,
        task: PublicTask,
        kernel_bytes: bytes,
        work_dir: Path,
        config: ToolConfig,
    ) -> BackendResult:
        work_dir.mkdir(parents=True, exist_ok=False)
        for name, content in task.headers.items():
            destination = work_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        (work_dir / task.kernel_name).write_bytes(kernel_bytes)
        if kind in {"csim", "cosim"}:
            tb_path = work_dir / task.public_tb_name
            tb_path.parent.mkdir(parents=True, exist_ok=True)
            tb_path.write_bytes(task.public_tb_bytes)

        if kind == "csim":
            return self._run_csim(task, work_dir, config)
        if kind == "synth":
            return self._run_synth(task, work_dir, config)
        if kind == "cosim":
            return self._run_cosim(task, work_dir, config)
        raise ValueError(f"unsupported Vitis tool: {kind}")

    def _common_tcl(self, task: PublicTask, config: ToolConfig) -> str:
        return (
            f"open_solution sol -flow_target {config.flow_target}\n"
            f"set_top {task.top}\n"
            f"set_part {config.part}\n"
            f"create_clock -period {config.clock_ns} -name clk_default\n"
        )

    def _run_vitis(
        self,
        *,
        kind: str,
        tcl: str,
        work_dir: Path,
        config: ToolConfig,
        timeout_s: float | None = None,
    ) -> tuple[ProcessResult, dict[str, str]]:
        tcl_path = work_dir / "run_hls.tcl"
        tcl_path.write_text(tcl, encoding="utf-8")
        timeout = config.timeout_for(kind) if timeout_s is None else float(timeout_s)
        toolchain = self._toolchain(config)
        receipt_path = work_dir / "vitis_toolchain.json"
        receipt_path.write_text(
            json.dumps(toolchain.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts = {
            "tcl": _artifact_ref(tcl_path, work_dir),
            "vitis_toolchain": _artifact_ref(receipt_path, work_dir),
        }
        if toolchain.preflight_result != "READY" and self._uses_default_runner:
            return ProcessResult(
                127,
                "",
                "TOOLCHAIN_UNAVAILABLE: no vitis-run or vitis_hls executable found",
                0.0,
                False,
            ), artifacts
        settings = Path(config.vitis_root) / "settings64.sh"
        invocation = " ".join(
            shlex.quote(item)
            for item in vitis_invocation_command(toolchain)
        )
        source_settings = (
            f". {shlex.quote(str(settings))} >/dev/null 2>&1 && "
            if settings.is_file()
            else ""
        )
        inner = source_settings + (
            "exec timeout --signal=TERM --kill-after=2s "
            f"{timeout:.6f}s {invocation}"
        )
        result = self._runner.run(
            ["bash", "-c", inner], cwd=work_dir, timeout_s=timeout
        )
        stdout_path = work_dir / "vitis.stdout.log"
        stderr_path = work_dir / "vitis.stderr.log"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        artifacts.update(
            {
                "vitis_stdout": _artifact_ref(stdout_path, work_dir),
                "vitis_stderr": _artifact_ref(stderr_path, work_dir),
            }
        )
        return result, artifacts

    def _run_csim(
        self, task: PublicTask, work_dir: Path, config: ToolConfig
    ) -> BackendResult:
        deadline = time.monotonic() + config.timeout_for("csim")
        tcl = (
            "open_project csim_proj\n"
            f"add_files -tb {{{task.kernel_name}}}\n"
            f"add_files -tb {{{task.public_tb_name}}}\n"
            + self._common_tcl(task, config)
            + "csim_design -setup\nexit\n"
        )
        compile_result, artifacts = self._run_vitis(
            kind="csim",
            tcl=tcl,
            work_dir=work_dir,
            config=config,
            timeout_s=min(
                config.timeout_for("csim"),
                max(1e-6, deadline - time.monotonic()),
            ),
        )
        if compile_result.timed_out:
            return BackendResult(
                False, "timeout", -1, compile_result.elapsed_s,
                _log_evidence(compile_result), artifacts,
            )
        if compile_result.return_code != 0:
            return BackendResult(
                False, "compile_error", compile_result.return_code,
                compile_result.elapsed_s, _log_evidence(compile_result), artifacts,
            )
        executable = work_dir / "csim_proj" / "sol" / "csim" / "build" / "csim.exe"
        if not executable.is_file():
            return BackendResult(
                False, "compile_error", compile_result.return_code,
                compile_result.elapsed_s,
                ["csim executable is missing after successful setup"], artifacts,
            )
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            return BackendResult(
                False,
                "timeout",
                -1,
                compile_result.elapsed_s,
                ["csim shared timeout expired before public test execution"],
                artifacts,
            )
        inner = (
            "exec timeout --signal=TERM --kill-after=2s "
            f"{timeout:.6f}s {shlex.quote(str(executable))}"
        )
        run_result = self._runner.run(
            ["bash", "-c", inner], cwd=executable.parent, timeout_s=timeout
        )
        stdout_path = work_dir / "csim.stdout.log"
        stderr_path = work_dir / "csim.stderr.log"
        stdout_path.write_text(run_result.stdout, encoding="utf-8")
        stderr_path.write_text(run_result.stderr, encoding="utf-8")
        artifacts.update(
            {
                "csim_stdout": _artifact_ref(stdout_path, work_dir),
                "csim_stderr": _artifact_ref(stderr_path, work_dir),
            }
        )
        elapsed = compile_result.elapsed_s + run_result.elapsed_s
        if run_result.timed_out:
            return BackendResult(False, "timeout", -1, elapsed, _log_evidence(run_result), artifacts)
        ok = run_result.return_code == 0
        return BackendResult(
            ok,
            "pass" if ok else "runtime_fail",
            run_result.return_code,
            elapsed,
            _log_evidence(run_result),
            artifacts,
        )

    def _run_synth(
        self, task: PublicTask, work_dir: Path, config: ToolConfig
    ) -> BackendResult:
        tcl = (
            "open_project synth_proj\n"
            f"add_files {{{task.kernel_name}}}\n"
            + self._common_tcl(task, config)
            + "config_compile -unsafe_math_optimizations\n"
            + "csynth_design\nexit\n"
        )
        process, artifacts = self._run_vitis(
            kind="synth", tcl=tcl, work_dir=work_dir, config=config
        )
        if process.timed_out:
            return BackendResult(False, "timeout", -1, process.elapsed_s, _log_evidence(process), artifacts)
        if process.return_code != 0:
            return BackendResult(
                False, "synth_error", process.return_code, process.elapsed_s,
                _log_evidence(process), artifacts,
            )
        report_path = work_dir / "synth_proj" / "sol" / "syn" / "report" / "csynth.xml"
        if not report_path.is_file():
            return BackendResult(
                False, "synth_error", process.return_code, process.elapsed_s,
                ["csynth.xml is missing"], artifacts,
            )
        artifacts["csynth_xml"] = _artifact_ref(report_path, work_dir)
        try:
            report = parse_synth_report(report_path)
        except (ET.ParseError, OSError, TypeError, ValueError) as exc:
            return BackendResult(
                False, "synth_error", process.return_code, process.elapsed_s,
                [f"cannot parse csynth.xml: {exc}"], artifacts,
            )
        estimated_clock = report.get("estimated_clock_period_ns")
        if not isinstance(estimated_clock, (int, float)) or isinstance(
            estimated_clock, bool
        ):
            return BackendResult(
                False,
                "synth_error",
                process.return_code,
                process.elapsed_s,
                ["csynth.xml is missing a valid EstimatedClockPeriod"],
                artifacts,
                report=report,
            )
        return BackendResult(
            True, "pass", process.return_code, process.elapsed_s,
            _log_evidence(process), artifacts, report=report,
        )

    def _run_cosim(
        self, task: PublicTask, work_dir: Path, config: ToolConfig
    ) -> BackendResult:
        tcl = (
            "open_project cosim_proj\n"
            f"add_files {{{task.kernel_name}}}\n"
            f"add_files -tb {{{task.public_tb_name}}}\n"
            + self._common_tcl(task, config)
            + "csynth_design\ncosim_design\nexit\n"
        )
        process, artifacts = self._run_vitis(
            kind="cosim", tcl=tcl, work_dir=work_dir, config=config
        )
        if process.timed_out:
            return BackendResult(False, "timeout", -1, process.elapsed_s, _log_evidence(process), artifacts)
        synth_path = work_dir / "cosim_proj" / "sol" / "syn" / "report" / "csynth.xml"
        if not synth_path.is_file():
            return BackendResult(
                False, "synth_error", process.return_code, process.elapsed_s,
                ["cosim synthesis report is missing"], artifacts,
            )
        artifacts["csynth_xml"] = _artifact_ref(synth_path, work_dir)
        report_path = work_dir / "cosim_proj" / "sol" / "sim" / "report" / f"{task.top}_cosim.rpt"
        if not report_path.is_file():
            return BackendResult(
                False, "cosim_fail", process.return_code, process.elapsed_s,
                ["cosim report is missing", *_log_evidence(process)], artifacts,
            )
        artifacts["cosim_report"] = _artifact_ref(report_path, work_dir)
        try:
            cosim = parse_cosim_report(report_path)
        except OSError as exc:
            return BackendResult(
                False, "cosim_fail", process.return_code, process.elapsed_s,
                [f"cannot parse cosim report: {exc}"], artifacts,
            )
        if process.return_code != 0:
            return BackendResult(
                False, "cosim_fail", process.return_code, process.elapsed_s,
                _log_evidence(process), artifacts, cosim=cosim,
            )
        if cosim is None or str(cosim["status"]).casefold() != "pass":
            return BackendResult(
                False, "cosim_fail", process.return_code, process.elapsed_s,
                _log_evidence(process), artifacts, cosim=cosim,
            )
        return BackendResult(
            True, "pass", process.return_code, process.elapsed_s,
            _log_evidence(process), artifacts, cosim=cosim,
        )
