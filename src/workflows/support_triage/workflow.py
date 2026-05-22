"""Parent workflow: SupportTriageWorkflow.

End-to-end triage for incoming solopreneur support emails. Combines all
three Mistral Workflows primitives demonstrated in the official cookbooks:

  - Parallel activities         (insurance_claims pattern)
  - Sub-workflow fan-out         (code_modernization pattern)
  - Durable human-in-the-loop    (cargo_release / code_modernization pattern)

Pipeline:
  1. PARALLEL    : extract_metadata + detect_intent + extract_entities
                 + extract_custom_outputs (typed domain-specific fields)
  2. DETERMINISTIC ROUTING : category + urgency mapped to queue via
                 ``config.py`` helpers (rules live in ``triage_config.yaml``)
  3. SUB-WORKFLOW (only if category=bug AND codebase_path)
                 : BugCodeAnalysisWorkflow (Devstral or whichever code model
                 ``CONFIG.models.code_*`` points at)
  4. DRAFT REPLY : Reply-drafting model in the sender's language
  5. HITL (rules in CONFIG.hitl)
                 : wait_for_input — durable pause for reviewer approval
  6. RETURN JSON : TriageReport ready for Linear / GitHub / Notion automation

Every customizable knob lives in ``triage_config.yaml``. The workflow body
itself is pure orchestration and stays the same regardless of which
categories, entities, custom-outputs, models, or routing rules are loaded.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import mistralai.workflows as workflows
import mistralai.workflows.plugins.mistralai as wm
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .activities import (
        detect_intent,
        draft_response,
        extract_custom_outputs,
        extract_entities,
        extract_metadata,
    )
    from .config import (
        CATEGORY_NAMES,
        CONFIG,
        code_analysis_mode_for,
        default_urgency_for,
        is_high_urgency,
        resolve_queue,
        should_pause_for_human,
        suggested_actions_for,
    )

from .models import (  # noqa: E402
    BugAnalysisInput,
    CodeAnalysisResult,
    EmailInput,
    EmailMetadata,
    TriageReport,
    UrgencyLevel,
)
from .sub_workflow import BugCodeAnalysisWorkflow  # noqa: E402


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflows.workflow.define(
    name="solo-support-triage",
    workflow_display_name="Solo Support Triage",
    workflow_description=(
        "Triages an inbound support email for a solopreneur. Pipeline: "
        "(1) parallel intent + metadata + entity + custom-output extraction, "
        "(2) deterministic urgency routing from a YAML rule set, "
        "(3) code-inspection sub-workflow that runs Devstral against the "
        "configured codebase in 'root_cause' mode for bug emails or "
        "'implementation' mode for feature requests, "
        "(4) reply drafting with brand-voice constraints in the sender's "
        "language, (5) durable HITL approval. Returns structured JSON ready "
        "for Linear, GitHub, or Notion automation. Every knob lives in "
        "triage_config.yaml."
    ),
    execution_timeout=timedelta(minutes=15),
)
class SupportTriageWorkflow(workflows.InteractiveWorkflow):
    """Orchestrates the full triage pipeline for one inbound support email."""

    @workflows.workflow.entrypoint
    async def run(self, email: EmailInput) -> TriageReport:
        # ------------------------------------------------------------------
        # Step 1: Parallel extraction (four independent LLM activities).
        #
        # asyncio.gather is the right primitive here because each activity
        # is a single Mistral call against a different prompt with its own
        # retry budget. Temporal tracks each activity event separately in
        # AI Studio, so the timeline shows four concurrent tasks.
        # ------------------------------------------------------------------
        meta_dict, intent_dict, entities_dict, custom_outputs_dict = await asyncio.gather(
            extract_metadata(email.subject, email.body),
            detect_intent(email.subject, email.body),
            extract_entities(email.subject, email.body),
            extract_custom_outputs(email.subject, email.body),
        )
        metadata = EmailMetadata.model_validate(meta_dict)

        category: str = intent_dict["category"]
        intent_confidence: float = intent_dict["confidence"]
        intent_rationale: str = intent_dict["rationale"]

        # Defensive fallback: if the LLM returned a category that is not in
        # the configured list, route to "other" to avoid undefined behaviour
        # in downstream rules.
        if category not in CATEGORY_NAMES:
            category = "other" if "other" in CATEGORY_NAMES else CATEGORY_NAMES[-1]

        # ------------------------------------------------------------------
        # Step 2: Deterministic routing.
        # All rules live in triage_config.yaml; the workflow body just calls
        # the helpers in config.py. No LLM, no I/O, fully replayable.
        # ------------------------------------------------------------------
        if is_high_urgency(
            sentiment=metadata.sentiment,
            category=category,
            urgency_keywords=metadata.urgency_keywords,
        ):
            urgency_str = "high"
        else:
            urgency_str = default_urgency_for(category)

        urgency = UrgencyLevel(urgency_str)
        routing_queue = resolve_queue(category, urgency_str)

        # ------------------------------------------------------------------
        # Step 3: Optional code-analysis sub-workflow.
        #
        # The codebase_path is resolved with precedence:
        #   1. EmailInput.codebase_path (per-trigger override, multi-tenant)
        #   2. CONFIG.code_analysis.codebase_path (server-side default)
        #   3. None → skip code analysis
        #
        # The analysis MODE is resolved from CONFIG.code_analysis.mode_by_category
        # via the helper code_analysis_mode_for. Two modes ship today:
        #   - root_cause     : bug diagnosis with fix proposals (needs error_messages)
        #   - implementation : feature feasibility with implementation options
        #
        # Bug mode also requires at least one extracted error message; feature
        # mode runs even without errors because the email body itself describes
        # the desired feature.
        # ------------------------------------------------------------------
        code_analysis: CodeAnalysisResult | None = None
        error_messages: list[str] = entities_dict.get("error_messages", []) or []
        mentioned_features: list[str] = entities_dict.get("mentioned_features", []) or []
        effective_codebase_path: str | None = (
            email.codebase_path or CONFIG.code_analysis.codebase_path
        )
        analysis_mode: str | None = code_analysis_mode_for(category)

        # Bug-mode requires at least one error_message to avoid burning Devstral
        # calls on vague bug-shaped tickets. Feature-mode does not need errors.
        has_actionable_signal = (
            analysis_mode == "implementation"
            or (analysis_mode == "root_cause" and bool(error_messages))
        )

        if (
            analysis_mode is not None
            and effective_codebase_path is not None
            and has_actionable_signal
        ):
            code_analysis = await workflows.workflow.execute_workflow(
                BugCodeAnalysisWorkflow,
                params=BugAnalysisInput(
                    email_id=email.email_id,
                    mode=analysis_mode,
                    error_messages=error_messages,
                    mentioned_features=mentioned_features,
                    body_excerpt=email.body[:2000],
                    codebase_path=effective_codebase_path,
                ),
                execution_timeout=timedelta(minutes=8),
            )

        # ------------------------------------------------------------------
        # Step 4: Draft reply in the sender's language with the configured
        # reply-drafting model, honoring tone/length/signature constraints.
        # ------------------------------------------------------------------
        code_analysis_json = (
            json.dumps(code_analysis.model_dump(), default=str)
            if code_analysis is not None
            else ""
        )
        custom_outputs_json = (
            json.dumps(custom_outputs_dict, default=str)
            if custom_outputs_dict
            else "{}"
        )
        reply_dict = await draft_response(
            subject=email.subject,
            body=email.body,
            language=metadata.language,
            category=category,
            sentiment=metadata.sentiment,
            urgency=urgency_str,
            code_analysis_json=code_analysis_json,
            custom_outputs_json=custom_outputs_json,
        )
        reply_text: str = reply_dict["reply_text"]

        # ------------------------------------------------------------------
        # Step 5: HITL approval, driven by CONFIG.hitl rules.
        #
        # wait_for_input() suspends the workflow at zero compute cost. The
        # reviewer sees the draft in AI Studio / Le Chat and clicks Accept
        # or Decline. The workflow resumes the instant they respond — even
        # if hours have passed. No polling, no timeouts on our side.
        # ------------------------------------------------------------------
        approved = False
        pause = should_pause_for_human(
            urgency=urgency_str,
            category=category,
            intent_confidence=intent_confidence,
        )
        if pause:
            preview = (
                f"Routing queue: {routing_queue}\n"
                f"Category: {category} (confidence {intent_confidence:.2f})\n"
                f"Sentiment: {metadata.sentiment}\n"
                f"Urgency: {urgency_str}\n\n"
                f"Custom outputs:\n{custom_outputs_json}\n\n"
                f"--- Suggested reply ---\n{reply_text}"
            )
            await wm.send_assistant_message(preview)

            confirmation = await self.wait_for_input(
                wm.AcceptDeclineConfirmation(
                    description="Reviewer: approve this auto-reply?",
                    accept_label="Approve & flag for send",
                    decline_label="Take over manually",
                ),
                label="Reply Approval",
            )
            approved = wm.is_accepted(confirmation)

        # ------------------------------------------------------------------
        # Step 6: Assemble structured TriageReport for downstream automation.
        # ------------------------------------------------------------------
        actions = suggested_actions_for(
            category=category,
            urgency=urgency_str,
            has_code_analysis=code_analysis is not None,
        )

        return TriageReport(
            email_id=email.email_id,
            sender_email=email.sender_email,
            subject=email.subject,
            category=category,
            urgency=urgency,
            sentiment=metadata.sentiment,
            language=metadata.language,
            metadata=metadata,
            entities=entities_dict,
            custom_outputs=custom_outputs_dict,
            code_analysis=code_analysis,
            suggested_reply=reply_text,
            suggested_actions=actions,
            requires_human_approval=pause,
            approved=approved,
            routing_queue=routing_queue,
            workflow_metadata={
                "models_used": CONFIG.models.model_dump(),
                "intent_confidence": intent_confidence,
                "intent_rationale": intent_rationale,
                "config_categories": CATEGORY_NAMES,
                "config_custom_outputs_count": len(CONFIG.custom_outputs),
                "code_analysis_mode": analysis_mode,  # null when no analysis ran
                "code_analysis_skipped_reason": (
                    None if code_analysis is not None
                    else (
                        "category-not-triggered" if analysis_mode is None
                        else "no-codebase-path" if effective_codebase_path is None
                        else "no-error-messages-for-bug-mode" if not has_actionable_signal
                        else "unknown"
                    )
                ),
            },
        )
