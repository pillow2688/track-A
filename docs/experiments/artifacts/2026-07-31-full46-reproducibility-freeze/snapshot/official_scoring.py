"""Server-side grading: hidden correctness + PPA, with a correctness gate.

Grading runs OUTSIDE the agent's metered budget. It:
  1. runs the HIDDEN testbench against the candidate  (functional correctness),
  2. C-synthesizes the candidate                       (synthesizability + PPA),
  3. C-synthesizes the task's original starting code   (baseline latency),
and scores with correctness-before-PPA, mirroring HLSTrans:

    Acceleration = baseline_latency / candidate_latency   (only if correct+synth)
    %OPT contribution = Acceleration > 1

    score = difficulty * ( 0.5*correct + 0.2*synthesizable + 0.3*ppa_norm )
    ppa_norm = min(Acceleration, ACCEL_CAP) / ACCEL_CAP

If the candidate fails the hidden testbench, PPA is not counted at all (score 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .report import SynthReport
from .task import Task
from .tools import CoSimTool, CSimTool, SynthTool, ToolResult

ACCEL_CAP = 8.0  # acceleration beyond this doesn't earn extra PPA credit


@dataclass
class Scorecard:
    task_id: str
    difficulty: int
    functional_pass: bool
    synth_pass: bool
    cosim_pass: bool | None  # None if the task doesn't require cosim
    baseline_latency: int | None
    candidate_latency: int | None
    acceleration: float | None
    is_opt: bool
    candidate_report: SynthReport | None
    baseline_report: SynthReport | None
    score: float

    def render(self) -> str:
        lines = [
            f"=== Scorecard: {self.task_id} (difficulty {self.difficulty}) ===",
            f"  functional (hidden TB): {'PASS' if self.functional_pass else 'FAIL'}",
            f"  synthesizable         : {'PASS' if self.synth_pass else 'FAIL'}",
        ]
        if self.cosim_pass is not None:
            lines.append(
                f"  cosim (C/RTL verify)  : {'PASS' if self.cosim_pass else 'FAIL'}"
            )
        if self.baseline_latency is not None:
            lines.append(f"  baseline latency      : {self.baseline_latency} cyc")
        if self.candidate_latency is not None:
            lines.append(f"  candidate latency     : {self.candidate_latency} cyc")
        if self.acceleration is not None:
            lines.append(
                f"  acceleration          : {self.acceleration:.2f}x  (opt={self.is_opt})"
            )
        if self.candidate_report is not None:
            r = self.candidate_report.resources
            lines.append(
                f"  candidate resources   : LUT={r['LUT']} FF={r['FF']} DSP={r['DSP']} BRAM={r['BRAM_18K']}"
            )
        lines.append(f"  SCORE                 : {self.score:.3f}")
        return "\n".join(lines)


def _latency(r: ToolResult | None) -> int | None:
    if r is None or r.report is None:
        return None
    rep = r.report
    return rep.latency_worst if rep.latency_worst is not None else rep.latency_avg


def grade(task: Task, candidate_kernel: str, work_root: Path) -> Scorecard:
    work_root = Path(work_root)
    csim, synth = CSimTool(), SynthTool()

    # 1. hidden functional test (C-simulation)
    hidden_files = task.assemble(
        candidate_kernel, task.hidden_tb_code, task.hidden_tb_name
    )
    func = csim.run(
        work_root / "grade_csim",
        hidden_files,
        top=task.top,
        part=task.part,
        clock_ns=task.clock_ns,
    )

    # 1b. optional RTL-verified gate: for structural tasks, csim is not enough
    # (it cannot see deadlock / invalid streaming). cosim is the real signal.
    cosim_pass: bool | None = None
    if task.requires_cosim:
        cosim = CoSimTool().run(
            work_root / "grade_cosim",
            hidden_files,
            synth_sources=[task.kernel_name],
            tb_sources=[task.hidden_tb_name],
            top=task.top,
            part=task.part,
            clock_ns=task.clock_ns,
        )
        cosim_pass = cosim.ok

    # 2. candidate synthesis
    cand_files = dict(task.headers)
    cand_files[task.kernel_name] = candidate_kernel
    cand_synth = synth.run(
        work_root / "grade_synth_cand",
        cand_files,
        synth_sources=[task.kernel_name],
        top=task.top,
        part=task.part,
        clock_ns=task.clock_ns,
    )

    # 3. baseline synthesis (task's original starting code)
    base_files = dict(task.headers)
    base_files[task.kernel_name] = task.kernel_code
    base_synth = synth.run(
        work_root / "grade_synth_base",
        base_files,
        synth_sources=[task.kernel_name],
        top=task.top,
        part=task.part,
        clock_ns=task.clock_ns,
    )

    functional_pass = func.ok and (cosim_pass is not False)
    synth_pass = cand_synth.ok
    cand_lat = _latency(cand_synth)
    base_lat = _latency(base_synth)

    acceleration = None
    if functional_pass and synth_pass and cand_lat and base_lat:
        acceleration = base_lat / cand_lat
    is_opt = acceleration is not None and acceleration > 1.0

    if not functional_pass:
        score = 0.0
    else:
        ppa_norm = min(acceleration, ACCEL_CAP) / ACCEL_CAP if acceleration else 0.0
        quality = 0.5 * 1.0 + 0.2 * (1.0 if synth_pass else 0.0) + 0.3 * ppa_norm
        score = task.difficulty * quality

    return Scorecard(
        task_id=task.id,
        difficulty=task.difficulty,
        functional_pass=functional_pass,
        synth_pass=synth_pass,
        cosim_pass=cosim_pass,
        baseline_latency=base_lat,
        candidate_latency=cand_lat,
        acceleration=acceleration,
        is_opt=is_opt,
        candidate_report=cand_synth.report,
        baseline_report=base_synth.report,
        score=round(score, 4),
    )
