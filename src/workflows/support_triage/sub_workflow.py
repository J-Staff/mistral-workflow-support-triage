"""Sub-workflow: BugCodeAnalysisWorkflow — runs Devstral against a local repo.

WHY A SUB-WORKFLOW INSTEAD OF INLINE ACTIVITIES?
The code-analysis pipeline has multiple Devstral calls (rank → read → propose)
that share a clear contract and can fail independently. Wrapping them in their
own workflow gives us:

  - Independent retry surfaces: if Devstral hallucinates an invalid file list,
    only this sub-workflow retries, not the entire support-triage parent.
  - Clean AI Studio timeline: the parent shows one collapsible 'Code Analysis'
    node instead of four interleaved activity events.
  - Reusable: the BugCodeAnalysisWorkflow can be invoked directly from any other
    workflow that wants Devstral-on-repo for a bug description.

Pipeline:
  1. scan_codebase            (file-system, deterministic)
  2. rank_suspect_files       (Devstral, structured-output)
  3. read_suspect_files       (file-system, deterministic)
  4. propose_fix_options      (Devstral, structured-output)
  5. assemble CodeAnalysisResult
"""

from __future__ import annotations

import json
from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .activities_code import (
        propose_fix_options,
        rank_suspect_files,
        read_suspect_files,
        scan_codebase,
    )
    from .config import CONFIG

from .models import (  # noqa: E402
    BugAnalysisInput,
    CodeAnalysisResult,
    SuspectFile,
)


@workflows.workflow.define(
    name="bug-code-analysis",
    workflow_display_name="Code Inspection (Devstral)",
    workflow_description=(
        "Reads a local codebase and runs Devstral in one of two modes. "
        "In 'root_cause' mode (bug emails) it ranks the most likely suspect "
        "files and proposes 2-4 surgical fixes. In 'implementation' mode "
        "(feature requests) it ranks the most impacted files and proposes "
        "2-3 implementation approaches with effort and risk hints. The "
        "registered workflow name stays 'bug-code-analysis' for backward "
        "compatibility with existing triggers."
    ),
    execution_timeout=timedelta(minutes=8),
)
class BugCodeAnalysisWorkflow:
    """Child workflow that runs Devstral against a local repo for a bug report.

    The workflow body is pure orchestration: scan, rank, read, propose. All
    I/O and LLM calls happen inside activities so Temporal can replay this
    workflow safely on worker restarts.
    """

    @workflows.workflow.entrypoint
    async def run(self, params: BugAnalysisInput) -> CodeAnalysisResult:
        # ------------------------------------------------------------------
        # Step 1: Scan the repo for candidate source files
        # ------------------------------------------------------------------
        candidates: list[dict] = await scan_codebase(params.codebase_path)
        if not candidates:
            # Empty repo or unreadable path — return a degraded result so the
            # parent workflow can still draft a generic reply.
            return CodeAnalysisResult(
                suspect_files=[],
                root_cause_hypothesis=(
                    "No source files were readable at the provided codebase_path."
                ),
                fix_options=[
                    _no_op_fix(
                        "Investigate manually",
                        "Automated code analysis could not access the repository.",
                    )
                ],
            )

        # Trim to a manageable batch before sending to Devstral.
        # In production we'd plug in a smarter ranker (BM25 / embeddings),
        # but a slice is enough for the contest demo. The cap is config-driven
        # so forkers can raise it for larger repos without editing Python.
        trimmed = candidates[: CONFIG.code_analysis.max_candidates_to_devstral]

        # ------------------------------------------------------------------
        # Step 2: Devstral ranks the suspects (mode-aware prompt)
        # ------------------------------------------------------------------
        ranked_dict = await rank_suspect_files(
            errors_json=json.dumps(params.error_messages),
            features_json=json.dumps(params.mentioned_features),
            body_excerpt=params.body_excerpt,
            candidates_json=json.dumps(trimmed),
            mode=params.mode,
        )
        suspect_dicts: list[dict] = ranked_dict.get("suspects", [])
        suspect_files = [SuspectFile.model_validate(s) for s in suspect_dicts]

        if not suspect_files:
            return CodeAnalysisResult(
                suspect_files=[],
                root_cause_hypothesis=(
                    "Devstral could not link the reported error to any file in the repo."
                ),
                fix_options=[
                    _no_op_fix(
                        "Request reproduction details",
                        "Ask the customer for exact reproduction steps or a stack trace.",
                    )
                ],
            )

        # ------------------------------------------------------------------
        # Step 3: Read the suspect file contents (file-system activity)
        # ------------------------------------------------------------------
        file_contents = await read_suspect_files(
            params.codebase_path,
            [s.relative_path for s in suspect_files],
        )

        # ------------------------------------------------------------------
        # Step 4: Devstral proposes fix options OR implementation options,
        # depending on params.mode.
        # ------------------------------------------------------------------
        proposal_dict = await propose_fix_options(
            errors_json=json.dumps(params.error_messages),
            body_excerpt=params.body_excerpt,
            suspect_file_contents_json=json.dumps(file_contents),
            mode=params.mode,
        )

        # ------------------------------------------------------------------
        # Step 5: Assemble the final result
        # ------------------------------------------------------------------
        return CodeAnalysisResult(
            suspect_files=suspect_files,
            root_cause_hypothesis=proposal_dict["root_cause_hypothesis"],
            fix_options=proposal_dict["fix_options"],
        )


def _no_op_fix(title: str, description: str):
    """Return a placeholder FixOption used when analysis cannot proceed.

    Kept as a free function (no I/O) so it is safe to call from workflow code.
    """
    from .models import FixOption

    return FixOption(
        title=title,
        description=description,
        affected_files=[],
        estimated_effort="hours",
        risk_level="low",
        code_sketch=None,
    )
