"""LLM activities for the Solo Support Triage workflow.

Each activity is a discrete, idempotent unit of work that calls a Mistral
model and returns a structured payload. Activities return dicts (not
Pydantic models) so Temporal's data converter round-trips them as JSON
without class-identity issues across the workflow sandbox boundary.

Every customizable knob is read from ``config.py``:
  - the model used per step is ``CONFIG.models.*``
  - the category list, entity list, and custom-output spec come from YAML
  - the reply tone and length constraints come from ``CONFIG.reply``

To run the workflow against a different domain, edit ``triage_config.yaml``
and restart the worker. No Python edits required.
"""

from __future__ import annotations

from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.client import models as mistralai_models
from mistralai.workflows.plugins.mistralai.activities import chat_parse_to_model

from .config import (
    CATEGORIES_PROMPT,
    CONFIG,
    CUSTOM_OUTPUTS_PROMPT,
    ENTITIES_PROMPT,
    HAS_CUSTOM_OUTPUTS,
    CustomOutputs,
    EmailEntities,
    IntentDetection,
)
from .models import DraftReply, EmailMetadata


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

_METADATA_PROMPT = (
    "You are a customer-success analyst. Read the email below and extract "
    "structured metadata.\n\n"
    "Allowed sentiment values: {sentiments}\n"
    "Allowed language codes:   {languages}\n\n"
    "Return JSON with fields:\n"
    "  - sentiment: one of the allowed sentiment values\n"
    "  - language:  one of the allowed language codes\n"
    "  - customer_lifetime_signal: short hint about whether the sender sounds "
    "like a free user, paying customer, prospect, ex-customer, or unknown\n"
    "  - urgency_keywords: words from the email body that signal urgency "
    "(e.g. 'down', 'broken', 'urgent', 'lost data', 'cannot log in')\n\n"
    "Email subject: {subject}\n"
    "Email body:\n{body}"
)


@workflows.activity(
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=60),
)
async def extract_metadata(subject: str, body: str) -> dict:
    """Extract sentiment, language, and urgency signals from the email."""
    prompt = _METADATA_PROMPT.format(
        subject=subject,
        body=body,
        sentiments=", ".join(CONFIG.sentiments),
        languages=", ".join(CONFIG.languages),
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.metadata_extraction,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(EmailMetadata, request)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_INTENT_PROMPT = (
    "You classify support emails into one primary intent category.\n\n"
    "Categories (pick exactly one):\n"
    "{categories}\n\n"
    "Return JSON with fields:\n"
    "  - category: one of the category names above\n"
    "  - confidence: float 0-1\n"
    "  - rationale: one short sentence explaining your choice\n\n"
    "Email subject: {subject}\n"
    "Email body:\n{body}"
)


@workflows.activity(
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=60),
)
async def detect_intent(subject: str, body: str) -> dict:
    """Classify the email into one of the configured intent categories."""
    prompt = _INTENT_PROMPT.format(
        subject=subject,
        body=body,
        categories=CATEGORIES_PROMPT,
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.intent_detection,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(IntentDetection, request)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Entity extraction (configured list[str] fields)
# ---------------------------------------------------------------------------

_ENTITIES_PROMPT = (
    "You are a technical-support analyst. Extract the following structured "
    "entities from the email below. Each entity is a list of strings.\n\n"
    "Entities to extract (read every description carefully, some fields exclude "
    "categories that belong to other entities):\n"
    "{entities}\n\n"
    "HARD RULES:\n"
    "  1. Only include items LITERALLY present in the email text. If you cannot "
    "point to a substring that justifies an entry, do not include it.\n"
    "  2. De-duplicate within each list. If the same feature or version appears "
    "three times, include it once.\n"
    "  3. Do not cross-categorize. A plan tier ('Pro-Plan') is NOT a version. "
    "An error message ('500') is NOT a feature name.\n"
    "  4. Empty lists are perfectly fine and expected. Most emails will not fill "
    "all four entities.\n\n"
    "Return JSON whose keys are exactly the entity names.\n\n"
    "Email subject: {subject}\n"
    "Email body:\n{body}"
)


@workflows.activity(
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=60),
)
async def extract_entities(subject: str, body: str) -> dict:
    """Pull every configured list[str] entity from the email body."""
    prompt = _ENTITIES_PROMPT.format(
        subject=subject,
        body=body,
        entities=ENTITIES_PROMPT,
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.entity_extraction,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(EmailEntities, request)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Custom-output extraction (typed domain-specific fields)
# ---------------------------------------------------------------------------

_CUSTOM_OUTPUTS_PROMPT_TEMPLATE = (
    "You extract domain-specific structured information from a support email.\n\n"
    "HARD RULES (violating these is worse than returning null):\n"
    "  1. Only include values that are LITERALLY present in the email text. "
    "Quote-test: if you cannot point to a substring of the body that justifies the value, "
    "return the absence sentinel (null, empty string, or empty list).\n"
    "  2. Do not invent typical defaults. Common hallucinations to avoid: "
    "'Customer', 'Admin', 'User' as lane names; 'unknown' as a plan when no plan is mentioned; "
    "process names guessed from the topic; round numbers like 1, 10, 100 as user counts.\n"
    "  3. Strings, lists, and numbers are case-insensitive when matching the email body. "
    "But the OUTPUT value should follow the description's casing if specified.\n"
    "  4. Do not refuse partial output. Return what you can verify, leave the rest absent.\n\n"
    "Each field has its own type. Honor it strictly:\n"
    "  - string         : a plain string. Null when the field's description allows it, otherwise empty string.\n"
    "  - list_string    : a list of strings, de-duplicated. Empty list when absent.\n"
    "  - integer        : an integer, or null when absent.\n"
    "  - float          : a floating-point number, or null when absent.\n"
    "  - boolean        : true/false, or null when ambiguous and the field is not required.\n"
    "  - enum           : one of the explicitly listed values, or null when no value matches.\n\n"
    "Fields to extract (each description tells you exactly when to populate the field):\n"
    "{custom_outputs}\n\n"
    "Return JSON whose keys match the field names exactly. Repeat the hard rules in your "
    "head before producing each field.\n\n"
    "Email subject: {subject}\n"
    "Email body:\n{body}"
)


@workflows.activity(
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=60),
)
async def extract_custom_outputs(subject: str, body: str) -> dict:
    """Extract every typed custom-output field configured in YAML.

    Returns an empty dict when no custom outputs are configured. Otherwise
    returns a dict keyed by field name, with values typed per ``CustomOutputs``
    Pydantic model built dynamically in ``config.py``.
    """
    if not HAS_CUSTOM_OUTPUTS:
        return {}

    prompt = _CUSTOM_OUTPUTS_PROMPT_TEMPLATE.format(
        subject=subject,
        body=body,
        custom_outputs=CUSTOM_OUTPUTS_PROMPT,
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.custom_output_extraction,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(CustomOutputs, request)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Reply drafting
# ---------------------------------------------------------------------------

_REPLY_PROMPT_TEMPLATE = (
    "You draft support replies for a solopreneur.\n\n"
    "Tone: {tone}\n"
    "Max length: {max_words} words.\n"
    "Reply in the same language as the customer (detected: {language}).\n"
    "Acknowledge the specific issue, do not give generic answers.\n"
    "{timeline_clause}\n"
    "{prohibited_clause}\n"
    "{signature_clause}\n\n"
    "If a bug analysis is provided, reference 1-2 concrete next steps from it.\n"
    "If the issue is a question or feature request, give a direct answer or honest "
    "'on our roadmap / not planned' response.\n\n"
    "Customer email subject: {subject}\n"
    "Customer email body:\n{body}\n\n"
    "Detected category: {category}\n"
    "Detected sentiment: {sentiment}\n"
    "Detected urgency: {urgency}\n"
    "Custom outputs (domain-specific extracted info):\n{custom_outputs_json}\n"
    "Bug analysis (if any):\n{code_analysis}\n\n"
    "Return JSON with a single field 'reply_text' containing the full reply."
)


def _build_reply_clauses() -> dict[str, str]:
    """Render the optional reply-rule clauses from CONFIG.reply."""
    timeline = (
        ""
        if CONFIG.reply.promise_fix_timeline
        else "Do NOT promise a specific fix timeline."
    )
    prohibited = (
        ""
        if not CONFIG.reply.prohibited_phrases
        else (
            "Never use any of these phrases: "
            + ", ".join(f"'{p}'" for p in CONFIG.reply.prohibited_phrases)
            + "."
        )
    )
    signature = (
        ""
        if not CONFIG.reply.signature
        else f"End the reply with this exact signature line:\n{CONFIG.reply.signature}"
    )
    return {
        "timeline_clause": timeline,
        "prohibited_clause": prohibited,
        "signature_clause": signature,
    }


@workflows.activity(
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
    start_to_close_timeout=timedelta(seconds=120),
)
async def draft_response(
    subject: str,
    body: str,
    language: str,
    category: str,
    sentiment: str,
    urgency: str,
    code_analysis_json: str,
    custom_outputs_json: str,
) -> dict:
    """Generate the draft reply text using the reply-drafting model.

    Returns ``{"reply_text": "..."}``. The Pydantic wrapper keeps the
    activity consistent with every other structured-output call.
    """
    clauses = _build_reply_clauses()
    prompt = _REPLY_PROMPT_TEMPLATE.format(
        subject=subject,
        body=body,
        language=language,
        category=category,
        sentiment=sentiment,
        urgency=urgency,
        tone=CONFIG.reply.tone,
        max_words=CONFIG.reply.max_words,
        code_analysis=code_analysis_json or "(none)",
        custom_outputs_json=custom_outputs_json or "{}",
        **clauses,
    )
    request = mistralai_models.ChatCompletionRequest(
        model=CONFIG.models.reply_drafting,
        messages=[mistralai_models.UserMessage(content=prompt)],
    )
    result = await chat_parse_to_model(DraftReply, request)
    return result.model_dump()
