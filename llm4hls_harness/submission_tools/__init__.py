"""Submission reporting and preflight helpers.

This package is deliberately separate from the runtime Agent.  It reads
immutable run artifacts and prepares a reviewable, explicitly non-final
staging tree; it never changes Candidate or tool decisions.
"""

from .reporting import generate_submission_docs
from .qa import Finding, scan_tree, stage_submission

__all__ = ["Finding", "generate_submission_docs", "scan_tree", "stage_submission"]
