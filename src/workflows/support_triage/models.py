"""Static Pydantic models for the Solo Support Triage workflow.

The schemas that depend on user configuration (categories, entities,
custom outputs) live in ``config.py`` because they are constructed at
worker startup via ``pydantic.create_model``. Everything that is fixed
across configurations stays here.

Static here:
  - EmailInput          (workflow input)
  - EmailMetadata       (sentiment, language, urgency keywords)
  - UrgencyLevel        (closed set: low/medium/high)
  - BugAnalysisInput    (sub-workflow input)
  - SuspectFile, SuspectFileList
  - FixOption, FixProposal
  - CodeAnalysisResult
  - DraftReply
  - TriageReport        (workflow output; uses dict[str, Any] for config-driven
                         fields so the public schema is stable regardless of
                         which categories/entities/custom_outputs are enabled)

Dynamic in config.py:
  - IntentDetection     (category list comes from config.categories)
  - EmailEntities       (fields come from config.entities)
  - CustomOutputs       (fields come from config.custom_outputs)
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Workflow input
# ---------------------------------------------------------------------------


class EmailInput(BaseModel):
    """Top-level input submitted when a new support email arrives.

    The ``codebase_path`` field is optional and only used when the email
    is classified as a bug report. If absent, the workflow skips the
    code-analysis sub-workflow and only drafts a generic reply.
    """

    email_id: str = Field(description="Stable identifier, e.g. message-id header.")
    sender_email: str
    sender_name: str | None = None
    subject: str
    body: str
    received_at: str = Field(description="ISO-8601 timestamp.")
    codebase_path: str | None = Field(
        default=None,
        description=(
            "Optional absolute path to a local repo. If provided and category=bug, "
            "the BugCodeAnalysisWorkflow runs Devstral against the repo to suggest fixes."
        ),
    )


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


class EmailMetadata(BaseModel):
    """Structured metadata extracted from an email body.

    Sentiment and language are stored as plain strings (validated against the
    config-provided closed set in the workflow) rather than dynamic enums, to
    keep the JSON schema stable across config changes.
    """

    sentiment: str = Field(
        description="One of the configured sentiment values."
    )
    language: str = Field(
        description="ISO-639-1 language code, or one of the configured languages."
    )
    customer_lifetime_signal: str = Field(
        description="Free-text hint about whether sender is a free trial user, paying, prospect, etc."
    )
    urgency_keywords: list[str] = Field(
        default_factory=list,
        description="Words like 'down', 'broken', 'urgent' that drive urgency scoring.",
    )


# ---------------------------------------------------------------------------
# Urgency classification (deterministic, no LLM)
# ---------------------------------------------------------------------------


class UrgencyLevel(str, Enum):
    """Closed three-tier urgency. Not user-configurable on purpose: downstream
    routing and HITL rules depend on these exact labels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Reply drafting (single-field Pydantic wrapper for structured output)
# ---------------------------------------------------------------------------


class DraftReply(BaseModel):
    """Wraps the free-form reply text so chat_parse_to_model can validate it.

    A single str field is the cleanest way to keep all LLM responses
    structured while still producing prose output.
    """

    reply_text: str = Field(
        description="The full reply text in the customer's language."
    )


# ---------------------------------------------------------------------------
# Code analysis sub-workflow models
# ---------------------------------------------------------------------------


class BugAnalysisInput(BaseModel):
    """Input to the BugCodeAnalysisWorkflow child.

    The same workflow handles two analysis modes:
      - ``root_cause``     : bug diagnosis, used for category=bug
      - ``implementation`` : feature feasibility, used for category=feature_request
    The mode steers prompt selection inside the Devstral activities.
    """

    email_id: str
    mode: str = Field(
        default="root_cause",
        description="One of 'root_cause' or 'implementation'.",
    )
    error_messages: list[str] = Field(default_factory=list)
    mentioned_features: list[str] = Field(default_factory=list)
    body_excerpt: str = Field(
        description="Trimmed email body relevant to the issue or feature request."
    )
    codebase_path: str = Field(description="Absolute path to the repo root.")


class SuspectFile(BaseModel):
    """A single file flagged by Devstral as relevant to the reported bug."""

    relative_path: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    rationale: str


class SuspectFileList(BaseModel):
    """Wrapper for the LLM JSON-list response, used with chat_parse_to_model."""

    suspects: list[SuspectFile] = Field(
        default_factory=list,
        description="Files ranked by relevance to the reported bug.",
    )


class FixOption(BaseModel):
    """One proposed fix from Devstral, with cost and risk hints."""

    title: str = Field(description="Short label, e.g. 'Add input validation'.")
    description: str = Field(description="What the fix does and why.")
    affected_files: list[str]
    estimated_effort: str = Field(
        description="One of 'minutes', 'hours', 'half-day', 'day-plus'."
    )
    risk_level: str = Field(description="One of 'low', 'medium', 'high'.")
    code_sketch: str | None = Field(
        default=None,
        description="Optional minimal diff or pseudo-code showing the change.",
    )


class FixProposal(BaseModel):
    """LLM-shaped subset returned by propose_fix_options."""

    root_cause_hypothesis: str
    fix_options: list[FixOption] = Field(
        min_length=1,
        max_length=4,
        description="Two to four prioritized fix proposals.",
    )


class CodeAnalysisResult(BaseModel):
    """Full result of the BugCodeAnalysisWorkflow."""

    suspect_files: list[SuspectFile]
    root_cause_hypothesis: str
    fix_options: list[FixOption] = Field(
        min_length=1,
        max_length=4,
    )


# ---------------------------------------------------------------------------
# Final triage report (workflow output)
# ---------------------------------------------------------------------------


class TriageReport(BaseModel):
    """The structured JSON report returned by the workflow.

    Designed for downstream automation: pipe this into Linear (auto-create
    issue), GitHub (open issue with fix options), Notion (log ticket),
    or simply respond with suggested_reply via your mail client.

    Config-driven fields (entities, custom_outputs, category) are stored as
    ``dict[str, Any]`` or ``str`` so the public JSON schema stays stable when
    a forker edits ``triage_config.yaml``. The dynamic Pydantic models that
    validated the data live in ``config.py`` and are only used inside
    activities.
    """

    email_id: str
    sender_email: str
    subject: str
    category: str = Field(description="One of the configured intent categories.")
    urgency: UrgencyLevel
    sentiment: str
    language: str

    metadata: EmailMetadata
    entities: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Structured entities as configured in triage_config.yaml.",
    )
    custom_outputs: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Domain-specific typed outputs (string, list[str], int, float, bool, "
            "enum) as configured in triage_config.yaml. Empty when no custom "
            "outputs are configured."
        ),
    )

    code_analysis: CodeAnalysisResult | None = Field(
        default=None,
        description="Populated only if category=bug AND codebase_path was provided.",
    )

    suggested_reply: str = Field(
        description="Draft response in the sender's language, ready to send or edit."
    )
    suggested_actions: list[str] = Field(
        default_factory=list,
        description=(
            "Downstream actions for the human or automation layer: "
            "['create_linear_issue', 'tag_pro_customer', 'route_to_billing', ...]."
        ),
    )

    requires_human_approval: bool = Field(
        description="True when any HITL trigger fired and the workflow paused."
    )
    approved: bool = Field(
        default=False,
        description="Set to True after the reviewer accepts in AI Studio / Le Chat.",
    )
    routing_queue: str = Field(
        description="Deterministic Python-routed queue name for downstream dispatch."
    )

    workflow_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Activity timings, model versions, intent confidence, etc.",
    )
