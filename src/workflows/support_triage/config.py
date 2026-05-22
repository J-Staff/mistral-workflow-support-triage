"""Configuration loader for the Solo Support Triage workflow.

A single YAML file (``triage_config.yaml`` at the repo root by default)
drives every customizable knob:

  - which Mistral model is used at each step
  - the list of intent categories
  - the list of structured entities to extract
  - urgency rules (keywords, sentiment overrides, category mappings)
  - routing rules from (category, urgency) to a queue name
  - suggested-action templates per category and urgency

Forkers edit the YAML, restart the worker, and the entire workflow adapts
without touching Python code.

Why a YAML file and not Python constants?
  Because the audience for forks is mixed (founders, ops, support leads,
  not only Python devs). YAML is readable, comment-friendly, and avoids
  pull-request roulette over a single string change.

Why is this loaded once at module import?
  The config must be immutable for the lifetime of a workflow execution
  so Temporal can replay deterministically. Loading at import freezes the
  config at worker startup. Restart the worker after editing the YAML.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, create_model


# ---------------------------------------------------------------------------
# Schema (Pydantic) for the YAML config
# ---------------------------------------------------------------------------


class ModelSelection(BaseModel):
    """Mistral model identifier per workflow step.

    ``custom_output_extraction`` is split out from ``entity_extraction`` because
    typed custom outputs (with enum, optional, and integer fields) demand more
    instruction-following capability than simple list[str] extraction. Default
    bumps custom_output_extraction to ``mistral-large-latest`` so the typed
    schema is honored reliably; forkers with simple custom outputs can swap it
    back to small.
    """

    metadata_extraction: str = "mistral-small-latest"
    intent_detection: str = "mistral-small-latest"
    entity_extraction: str = "mistral-small-latest"
    custom_output_extraction: str = "mistral-large-latest"
    reply_drafting: str = "mistral-large-latest"
    code_ranking: str = "devstral-2512"
    code_fix_proposal: str = "devstral-2512"


class CategorySpec(BaseModel):
    """One intent category. The ``name`` is the canonical string returned
    by the LLM and stored in ``TriageReport.category``."""

    name: str = Field(min_length=1)
    description: str


class EntitySpec(BaseModel):
    """One structured entity field to extract from the email body."""

    name: str = Field(min_length=1)
    description: str


# Supported types for custom-output fields. Strings here are matched in
# ``_build_custom_outputs_model`` below to construct the right Python type.
CustomOutputType = Literal[
    "string",
    "list_string",
    "integer",
    "float",
    "boolean",
    "enum",
]


class CustomOutputSpec(BaseModel):
    """One typed, user-defined output field extracted by a dedicated LLM call.

    Where ``EntitySpec`` is always ``list[str]``, ``CustomOutputSpec`` lets
    forkers declare strongly-typed domain fields like 'which BPMN process is
    this about?' (string), 'customer tier' (enum), 'affected user count'
    (integer), or 'is the sender blocked?' (boolean).
    """

    name: str = Field(min_length=1)
    type: CustomOutputType
    description: str
    required: bool = Field(
        default=False,
        description=(
            "If true, the LLM must produce a non-null value. If false, "
            "missing values become null (for primitives) or empty (for "
            "strings/lists) so downstream code can branch on absence."
        ),
    )
    values: list[str] = Field(
        default_factory=list,
        description="Only used when type='enum'. The closed set of allowed values.",
    )


class UrgencyRules(BaseModel):
    """Deterministic urgency-rule configuration."""

    high_keywords: list[str] = Field(default_factory=list)
    high_sentiments: list[str] = Field(default_factory=lambda: ["angry"])
    bug_negative_to_high: list[str] = Field(default_factory=lambda: ["bug"])
    medium_categories: list[str] = Field(
        default_factory=lambda: ["bug", "billing"]
    )


class RoutingMatch(BaseModel):
    category: str | None = None
    urgency: str | None = None


class RoutingRule(BaseModel):
    match: RoutingMatch
    queue: str


class RoutingConfig(BaseModel):
    rules: list[RoutingRule] = Field(default_factory=list)
    default: str = "manual-review"


class ReplyConfig(BaseModel):
    """Constraints applied to the reply-drafting LLM step."""

    max_words: int = Field(default=180, ge=20, le=1000)
    tone: str = Field(
        default="warm but professional, no over-apologizing, no filler"
    )
    signature: str = Field(
        default="",
        description="Verbatim signature appended to the reply. Empty = LLM picks own closer.",
    )
    promise_fix_timeline: bool = Field(
        default=False,
        description="When false, the prompt explicitly forbids promising a fix date.",
    )
    prohibited_phrases: list[str] = Field(default_factory=list)


class HITLConfig(BaseModel):
    """Conditions that trigger a durable human-in-the-loop pause."""

    pause_on_urgency: list[str] = Field(default_factory=lambda: ["high"])
    pause_on_categories: list[str] = Field(default_factory=list)
    pause_on_low_confidence_below: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="0 disables the confidence trigger.",
    )


class CodeAnalysisConfig(BaseModel):
    """Scope, mode, and limits for the BugCodeAnalysisWorkflow sub-workflow."""

    codebase_path: str | None = Field(
        default=None,
        description=(
            "Server-side default path to the codebase the worker analyzes against. "
            "None disables code analysis unless the email overrides via "
            "EmailInput.codebase_path."
        ),
    )
    trigger_on_categories: list[str] = Field(
        default_factory=lambda: ["bug"],
        description=(
            "Categories that trigger the code-inspection sub-workflow. Add "
            "'feature_request' to also estimate implementation impact for new "
            "feature emails."
        ),
    )
    mode_by_category: dict[str, str] = Field(
        default_factory=lambda: {"bug": "root_cause"},
        description=(
            "Mode passed to Devstral per category. Supported modes: "
            "'root_cause' (find bug, propose fixes) and 'implementation' "
            "(find impacted files, propose implementation approaches)."
        ),
    )
    file_extensions: list[str] = Field(
        default_factory=lambda: [
            ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb"
        ]
    )
    skip_directories: list[str] = Field(
        default_factory=lambda: [
            ".git", "node_modules", "__pycache__", ".venv", "dist", "build"
        ]
    )
    max_candidates_to_devstral: int = Field(default=30, ge=1, le=200)
    max_files_per_analysis: int = Field(default=8, ge=1, le=50)
    max_bytes_per_file: int = Field(default=8_000, ge=500, le=200_000)


class TriageConfig(BaseModel):
    """Top-level config Pydantic schema. Loaded from YAML at startup."""

    models: ModelSelection = Field(default_factory=ModelSelection)
    categories: list[CategorySpec] = Field(min_length=1)
    entities: list[EntitySpec] = Field(min_length=1)
    custom_outputs: list[CustomOutputSpec] = Field(default_factory=list)
    sentiments: list[str] = Field(
        default_factory=lambda: ["positive", "neutral", "negative", "angry"]
    )
    languages: list[str] = Field(
        default_factory=lambda: ["en", "de", "fr", "es", "other"]
    )
    urgency: UrgencyRules = Field(default_factory=UrgencyRules)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    suggested_actions: dict[str, list[str]] = Field(default_factory=dict)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    hitl: HITLConfig = Field(default_factory=HITLConfig)
    code_analysis: CodeAnalysisConfig = Field(default_factory=CodeAnalysisConfig)


# ---------------------------------------------------------------------------
# Built-in defaults — used when no YAML file is present
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = TriageConfig(
    categories=[
        CategorySpec(name="bug", description="Something is broken or behaves unexpectedly."),
        CategorySpec(
            name="feature_request",
            description="User asks for a new capability that does not exist yet.",
        ),
        CategorySpec(name="question", description="User wants to know how to do something."),
        CategorySpec(
            name="billing",
            description="Invoices, refunds, plan changes, payment failures.",
        ),
        CategorySpec(
            name="spam",
            description="Unsolicited sales, phishing, off-topic.",
        ),
        CategorySpec(
            name="other",
            description="Real-but-uncategorizable. Use sparingly.",
        ),
    ],
    entities=[
        EntitySpec(
            name="error_messages",
            description="Verbatim error strings, stack traces, or HTTP status codes.",
        ),
        EntitySpec(
            name="mentioned_features",
            description="Product features the sender names.",
        ),
        EntitySpec(
            name="mentioned_versions",
            description="App, browser, OS, or library versions referenced.",
        ),
        EntitySpec(
            name="reproduction_steps",
            description="Step-by-step reproduction instructions if provided.",
        ),
    ],
    urgency=UrgencyRules(
        high_keywords=[
            "down",
            "outage",
            "lost data",
            "data loss",
            "cannot log in",
            "can't log in",
            "broken",
            "urgent",
            "critical",
            "emergency",
            "asap",
            "production",
            "payment failed",
            "refund",
        ],
    ),
    routing=RoutingConfig(
        rules=[
            RoutingRule(match=RoutingMatch(category="spam"), queue="trash"),
            RoutingRule(match=RoutingMatch(category="billing"), queue="billing-queue"),
            RoutingRule(
                match=RoutingMatch(category="bug", urgency="high"), queue="oncall"
            ),
            RoutingRule(match=RoutingMatch(category="bug"), queue="engineering-backlog"),
            RoutingRule(
                match=RoutingMatch(category="feature_request"), queue="product-backlog"
            ),
            RoutingRule(
                match=RoutingMatch(category="question"), queue="support-fast-track"
            ),
        ],
        default="manual-review",
    ),
    suggested_actions={
        "bug": ["create_linear_issue"],
        "bug-high": ["page_oncall"],
        "feature_request": ["add_to_product_board"],
        "billing": ["route_to_billing"],
        "spam": ["mark_as_spam"],
        "default": ["send_reply_after_review"],
    },
)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _find_config_path() -> Path | None:
    """Locate ``triage_config.yaml``.

    Resolution order:
      1. ``TRIAGE_CONFIG_PATH`` environment variable, if set
      2. ``triage_config.yaml`` in the current working directory
      3. ``triage_config.yaml`` next to the repository root (3 levels up from this file)
    """
    env = os.environ.get("TRIAGE_CONFIG_PATH")
    if env:
        return Path(env)
    cwd_candidate = Path.cwd() / "triage_config.yaml"
    if cwd_candidate.exists():
        return cwd_candidate
    repo_candidate = Path(__file__).resolve().parents[3] / "triage_config.yaml"
    if repo_candidate.exists():
        return repo_candidate
    return None


def load_config() -> TriageConfig:
    """Load the YAML config and validate via Pydantic.

    Falls back to built-in defaults if no YAML file is found. Re-raises any
    Pydantic validation error so misconfigurations surface at worker startup
    rather than silently mid-workflow.
    """
    path = _find_config_path()
    if path is None:
        return _DEFAULT_CONFIG
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return TriageConfig.model_validate(data)


# Module-level singleton. Loaded once at import time so the config is stable
# for the lifetime of the worker process.
CONFIG: TriageConfig = load_config()


# ---------------------------------------------------------------------------
# Derived constants — pre-built strings and types used by activities
# ---------------------------------------------------------------------------


def _categories_prompt_block(cats: list[CategorySpec]) -> str:
    """Render the categories list as a bullet block for LLM prompts."""
    return "\n".join(f"  - {c.name:<16}: {c.description}" for c in cats)


def _entities_prompt_block(ents: list[EntitySpec]) -> str:
    """Render the entities list as a bullet block for LLM prompts."""
    return "\n".join(f"  - {e.name}: {e.description}" for e in ents)


CATEGORY_NAMES: list[str] = [c.name for c in CONFIG.categories]
ENTITY_NAMES: list[str] = [e.name for e in CONFIG.entities]
CATEGORIES_PROMPT = _categories_prompt_block(CONFIG.categories)
ENTITIES_PROMPT = _entities_prompt_block(CONFIG.entities)


def _build_email_entities_model() -> type[BaseModel]:
    """Dynamically build a Pydantic model with one ``list[str]`` field per entity.

    Using ``create_model`` here lets us swap the schema based on YAML config
    without writing a static Pydantic class. The result is registered as a
    module-level type so its identity is stable for the worker lifetime,
    which keeps Temporal's data converter happy.
    """
    fields: dict[str, Any] = {}
    for spec in CONFIG.entities:
        fields[spec.name] = (
            list[str],
            Field(default_factory=list, description=spec.description),
        )
    return create_model("EmailEntities", **fields)


EmailEntities = _build_email_entities_model()


def _build_intent_detection_model() -> type[BaseModel]:
    """Build the IntentDetection model with the configured category set.

    We use a plain ``str`` field rather than a dynamic Enum so that the
    Pydantic schema serialised to the LLM is simple and the JSON output
    survives any post-hoc renaming of categories. Validation against the
    configured names happens at the workflow boundary.
    """
    return create_model(
        "IntentDetection",
        category=(str, Field(description=f"One of: {', '.join(CATEGORY_NAMES)}")),
        confidence=(float, Field(ge=0.0, le=1.0)),
        rationale=(str, Field(description="One-sentence explanation.")),
    )


IntentDetection = _build_intent_detection_model()


def _python_type_for_custom_output(spec: CustomOutputSpec) -> tuple[Any, Any]:
    """Map a CustomOutputSpec type string to a (python_type, default) pair.

    Optional fields use ``None`` as the default. Required fields use the
    natural empty value for their type (empty string, empty list). Enums
    use Literal[*values] so Pydantic enforces the closed set at parse time.
    """
    description = spec.description
    if spec.type == "string":
        default = "" if spec.required else None
        ann = str if spec.required else (str | None)
        return (ann, Field(default=default, description=description))
    if spec.type == "list_string":
        # Lists default to empty either way; "required" just enforces non-null.
        return (list[str], Field(default_factory=list, description=description))
    if spec.type == "integer":
        if spec.required:
            return (int, Field(description=description))
        return (int | None, Field(default=None, description=description))
    if spec.type == "float":
        if spec.required:
            return (float, Field(description=description))
        return (float | None, Field(default=None, description=description))
    if spec.type == "boolean":
        if spec.required:
            return (bool, Field(description=description))
        return (bool | None, Field(default=None, description=description))
    if spec.type == "enum":
        if not spec.values:
            raise ValueError(
                f"custom_output {spec.name!r} has type='enum' but no values list."
            )
        # Pydantic accepts Literal[...] at runtime, including from a tuple.
        literal_t = Literal[tuple(spec.values)]  # type: ignore[valid-type]
        if spec.required:
            return (literal_t, Field(description=description))
        return (literal_t | None, Field(default=None, description=description))
    raise ValueError(f"Unknown custom_output type: {spec.type!r}")


def _build_custom_outputs_model() -> type[BaseModel]:
    """Build a Pydantic model whose fields are the configured custom outputs.

    Returns a model with no fields when ``custom_outputs`` is empty. That
    keeps the call site uniform (always parse, even if the result is {}).
    """
    if not CONFIG.custom_outputs:
        return create_model("CustomOutputs")
    fields: dict[str, Any] = {}
    for spec in CONFIG.custom_outputs:
        fields[spec.name] = _python_type_for_custom_output(spec)
    return create_model("CustomOutputs", **fields)


CustomOutputs = _build_custom_outputs_model()


def _custom_outputs_prompt_block() -> str:
    """Render the custom-outputs spec list as a bullet block for prompts.

    The LLM sees the name, type hint, and description per field. Enums also
    include the allowed value set inline.
    """
    if not CONFIG.custom_outputs:
        return "(none configured)"
    lines: list[str] = []
    for spec in CONFIG.custom_outputs:
        type_hint = spec.type
        if spec.type == "enum":
            type_hint = f"enum[{', '.join(spec.values)}]"
        required = " (REQUIRED)" if spec.required else ""
        lines.append(f"  - {spec.name} ({type_hint}){required}: {spec.description}")
    return "\n".join(lines)


CUSTOM_OUTPUTS_PROMPT = _custom_outputs_prompt_block()
HAS_CUSTOM_OUTPUTS: bool = bool(CONFIG.custom_outputs)


# ---------------------------------------------------------------------------
# Routing helpers — used by the workflow body (pure functions, deterministic)
# ---------------------------------------------------------------------------


def resolve_queue(category: str, urgency: str) -> str:
    """Walk the routing rules and return the first matching queue.

    Falls back to ``routing.default`` if no rule matches. The lookup is
    pure Python so it stays deterministic for Temporal replay.
    """
    for rule in CONFIG.routing.rules:
        if rule.match.category is not None and rule.match.category != category:
            continue
        if rule.match.urgency is not None and rule.match.urgency != urgency:
            continue
        return rule.queue
    return CONFIG.routing.default


def suggested_actions_for(
    category: str, urgency: str, has_code_analysis: bool
) -> list[str]:
    """Compose the suggested-actions list from configured templates."""
    actions: list[str] = []
    actions.extend(CONFIG.suggested_actions.get(category, []))
    actions.extend(CONFIG.suggested_actions.get(f"{category}-{urgency}", []))
    if category == "bug" and has_code_analysis:
        # Always append the code-analysis hint when the sub-workflow ran.
        # Kept here rather than in YAML so the conditional is explicit.
        if "attach_fix_options_to_issue" not in actions:
            actions.append("attach_fix_options_to_issue")
    actions.extend(CONFIG.suggested_actions.get("default", []))
    return actions


def is_high_urgency(
    sentiment: str,
    category: str,
    urgency_keywords: list[str],
) -> bool:
    """Pure-Python rule that decides 'high' urgency from LLM signals."""
    rules = CONFIG.urgency
    keywords_lower = {k.lower() for k in urgency_keywords}
    if sentiment in rules.high_sentiments:
        return True
    if any(kw.lower() in keywords_lower for kw in rules.high_keywords):
        return True
    if category in rules.bug_negative_to_high and sentiment == "negative":
        return True
    return False


def default_urgency_for(category: str) -> str:
    """Return 'medium' or 'low' as the fallback when no high signal fired."""
    if category in CONFIG.urgency.medium_categories:
        return "medium"
    return "low"


def code_analysis_mode_for(category: str) -> str | None:
    """Return the Devstral analysis mode for a category, or None if not triggered.

    The category must appear in ``trigger_on_categories`` for any analysis to
    run. The returned mode is looked up in ``mode_by_category`` and defaults
    to ``root_cause`` when the category is triggered but not explicitly mapped.

    Modes:
      - root_cause     : Devstral diagnoses a bug and proposes fixes.
      - implementation : Devstral estimates feature implementation scope.
    """
    cfg = CONFIG.code_analysis
    if category not in cfg.trigger_on_categories:
        return None
    return cfg.mode_by_category.get(category, "root_cause")


def should_pause_for_human(
    urgency: str, category: str, intent_confidence: float
) -> bool:
    """Apply the configured HITL rules to decide if the workflow should pause.

    Any single trigger that fires causes a pause. Multiple triggers do not
    accumulate; one is enough.
    """
    cfg = CONFIG.hitl
    if urgency in cfg.pause_on_urgency:
        return True
    if category in cfg.pause_on_categories:
        return True
    if cfg.pause_on_low_confidence_below > 0 and intent_confidence < cfg.pause_on_low_confidence_below:
        return True
    return False
