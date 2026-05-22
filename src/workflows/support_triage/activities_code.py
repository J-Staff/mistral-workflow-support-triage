"""Devstral-powered code-analysis activities.

These activities run inside the BugCodeAnalysisWorkflow sub-workflow.
They scan a local codebase, identify suspect files, and generate concrete
fix options for the bug described in an inbound support email.

Devstral (or whichever code-specialized model the YAML config points at)
is the right tool for static code reasoning. We do NOT execute code here:
we only read it and prompt the model for hypotheses and patches.

Every limit, model name, and file-filter list is config-driven via
``CONFIG.code_analysis`` and ``CONFIG.models``.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import mistralai.workflows as workflows
from mistralai.client import models as mistralai_models
from mistralai.workflows.plugins.mistralai.activities import chat_parse_to_model

from .config import CONFIG
from .models import FixProposal, SuspectFileList


# ---------------------------------------------------------------------------
# Repo scanning (deterministic file-system activity, no LLM)
# ---------------------------------------------------------------------------


@workflows.activity(
    retry_policy_max_attempts=2,
    start_to_close_timeout=timedelta(seconds=30),
)
async def scan_codebase(codebase_path: str) -> list[dict]:
    """List candidate source files in the repo.

    Honors ``CONFIG.code_analysis.file_extensions`` and ``skip_directories``
    so forkers can plug in Java, Ruby, Rust, or any other stack without
    touching Python code.

    Returns a list of ``{relative_path, size_bytes, snippet}`` dicts.
    The snippet is the first 200 chars of the file, useful for cheap
    keyword matching before we let Devstral read the full content.
    """
    cfg = CONFIG.code_analysis
    root = Path(codebase_path).resolve()
    if not root.exists() or not root.is_dir():
        return []

    skip_dirs = set(cfg.skip_directories)
    suffixes = set(cfg.file_extensions)
    candidates: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if Path(fn).suffix not in suffixes:
                continue
            full = Path(dirpath) / fn
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            candidates.append(
                {
                    "relative_path": str(full.relative_to(root)),
                    "size_bytes": full.stat().st_size,
                    "snippet": content[:200],
                }
            )
    return candidates


# ---------------------------------------------------------------------------
# Devstral: rank suspect or impacted files (mode-dependent prompt)
# ---------------------------------------------------------------------------

_RANK_PROMPT_ROOT_CAUSE = (
    "You are a senior software engineer reviewing a bug report. Given the "
    "error messages and the list of candidate files in the repository, "
    "rank the files by how likely they are to contain the root cause. "
    "Return only the top {top_n} most relevant files.\n\n"
    "Bug error messages:\n{errors}\n\n"
    "Mentioned features:\n{features}\n\n"
    "Body excerpt:\n{body}\n\n"
    "Candidate files (relative_path, snippet of first 200 chars):\n{files}\n\n"
    "Return JSON with a single field 'suspects' (list). Each entry has:\n"
    "  - relative_path: the file path\n"
    "  - relevance_score: float 0-1 (your confidence it's related)\n"
    "  - rationale: one sentence explaining why\n\n"
    "Be selective: better 3 strong candidates than 8 weak ones."
)

_RANK_PROMPT_IMPLEMENTATION = (
    "You are a senior software engineer estimating implementation impact for "
    "a new feature request. Given the feature description and the list of "
    "candidate files in the repository, rank the files by how likely they are "
    "to REQUIRE CHANGES if we build this feature. Return only the top {top_n} "
    "most impacted files.\n\n"
    "Feature description (verbatim from the customer):\n{body}\n\n"
    "Mentioned features and concepts:\n{features}\n\n"
    "(Any error messages provided, for context, usually empty for feature requests):\n{errors}\n\n"
    "Candidate files (relative_path, snippet of first 200 chars):\n{files}\n\n"
    "Return JSON with a single field 'suspects' (list). Each entry has:\n"
    "  - relative_path: the file path\n"
    "  - relevance_score: float 0-1 (your confidence the file would need changes)\n"
    "  - rationale: one sentence explaining what changes you would expect there\n\n"
    "Prefer specificity: 3 strongly-impacted files beat 8 weakly-related ones."
)


@workflows.activity(
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=120),
)
async def rank_suspect_files(
    errors_json: str,
    features_json: str,
    body_excerpt: str,
    candidates_json: str,
    mode: str = "root_cause",
    top_n: int | None = None,
) -> dict:
    """Ask the code-ranking model which files matter for the issue.

    ``mode`` selects between the bug (root_cause) prompt and the feature
    (implementation) prompt. Returns ``{"suspects": [...]}``.
    """
    effective_top_n = top_n if top_n is not None else CONFIG.code_analysis.max_files_per_analysis
    template = (
        _RANK_PROMPT_IMPLEMENTATION
        if mode == "implementation"
        else _RANK_PROMPT_ROOT_CAUSE
    )
    prompt = template.format(
        errors=errors_json,
        features=features_json,
        body=body_excerpt,
        files=candidates_json,
        top_n=effective_top_n,
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.code_ranking,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(SuspectFileList, request)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Helper: read suspect file contents (file-system activity, no LLM)
# ---------------------------------------------------------------------------


@workflows.activity(
    retry_policy_max_attempts=2,
    start_to_close_timeout=timedelta(seconds=30),
)
async def read_suspect_files(
    codebase_path: str, relative_paths: list[str]
) -> list[dict]:
    """Read full contents of the suspect files, capped per the YAML config.

    A path-traversal guard rejects any path that resolves outside ``codebase_path``.
    """
    cfg = CONFIG.code_analysis
    root = Path(codebase_path).resolve()
    files: list[dict] = []
    for rel in relative_paths[: cfg.max_files_per_analysis]:
        full = (root / rel).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            continue
        if not full.exists() or not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files.append(
            {
                "relative_path": rel,
                "content": content[: cfg.max_bytes_per_file],
                "truncated": len(content) > cfg.max_bytes_per_file,
            }
        )
    return files


# ---------------------------------------------------------------------------
# Devstral: read suspect files and propose concrete fix options
# ---------------------------------------------------------------------------

_FIX_PROMPT_ROOT_CAUSE = (
    "You are a senior engineer proposing fixes for a bug. Read the suspect "
    "files below and propose 2-4 concrete fix options.\n\n"
    "Bug error messages:\n{errors}\n\n"
    "Body excerpt:\n{body}\n\n"
    "Suspect files (full content):\n{files}\n\n"
    "Return JSON with:\n"
    "  - root_cause_hypothesis: string explaining the most likely cause\n"
    "  - fix_options: list of 2-4 entries, each with:\n"
    "      title: short label\n"
    "      description: what the fix does and why\n"
    "      affected_files: list of relative paths\n"
    "      estimated_effort: 'minutes' | 'hours' | 'half-day' | 'day-plus'\n"
    "      risk_level: 'low' | 'medium' | 'high'\n"
    "      code_sketch: optional small diff or pseudo-code (or null)\n\n"
    "Prefer minimal, surgical fixes over rewrites. Order options from "
    "lowest-risk to highest-risk."
)

_FIX_PROMPT_IMPLEMENTATION = (
    "You are a senior engineer estimating implementation approaches for a new "
    "feature request from a customer. Read the impacted files below and propose "
    "2-3 concrete implementation approaches.\n\n"
    "Feature description (verbatim from the customer):\n{body}\n\n"
    "Related concepts mentioned (often empty for feature emails):\n{errors}\n\n"
    "Impacted files (full content):\n{files}\n\n"
    "Return JSON with:\n"
    "  - root_cause_hypothesis: this field carries the IMPLEMENTATION HYPOTHESIS in "
    "feature mode. Write a short summary of where in the architecture the feature "
    "should live and what pattern fits best (e.g. 'Add a drop-target overlay to the "
    "lane renderer and have the drag handler emit drop coordinates so an autorouter "
    "can splice the new node into the existing sequence flow').\n"
    "  - fix_options: list of 2-3 IMPLEMENTATION-OPTION entries, each with:\n"
    "      title: short label (e.g. 'Lane-hover detector + autosplice on drop')\n"
    "      description: what the implementation does, key data flow, design tradeoffs\n"
    "      affected_files: list of relative paths that would change\n"
    "      estimated_effort: 'minutes' | 'hours' | 'half-day' | 'day-plus'\n"
    "      risk_level: 'low' | 'medium' | 'high' (risk of breaking existing flows)\n"
    "      code_sketch: optional small pseudo-code outlining the change (or null)\n\n"
    "Order options from lowest-effort/lowest-risk (a quick MVP that ships value fast) "
    "to highest-effort (a proper architectural refit that survives the next 5 features). "
    "Be honest about tradeoffs: a quick MVP and a refactor approach in the same list "
    "is better than three near-identical options."
)


@workflows.activity(
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=180),
)
async def propose_fix_options(
    errors_json: str,
    body_excerpt: str,
    suspect_file_contents_json: str,
    mode: str = "root_cause",
) -> dict:
    """Devstral returns hypothesis + options based on the analysis mode.

    ``mode='root_cause'`` produces a bug diagnosis with fix options.
    ``mode='implementation'`` produces a feature implementation hypothesis with
    implementation options. Output schema (FixProposal) is identical.
    """
    template = (
        _FIX_PROMPT_IMPLEMENTATION
        if mode == "implementation"
        else _FIX_PROMPT_ROOT_CAUSE
    )
    prompt = template.format(
        errors=errors_json,
        body=body_excerpt,
        files=suspect_file_contents_json,
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.code_fix_proposal,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(FixProposal, request)
    return result.model_dump()
