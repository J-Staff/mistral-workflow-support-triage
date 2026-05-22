"""Solo Support Triage Workflow.

Triages incoming support emails for solopreneurs and small dev teams:
parallel intent/metadata extraction, deterministic category routing,
optional code-analysis sub-workflow for bug reports (Devstral),
draft reply generation (Mistral Large), and durable human-in-the-loop
approval for high-urgency tickets.

Returns a structured JSON TriageReport ready to plug into Linear,
GitHub Issues, Notion, or any email-followup automation.
"""

from .models import EmailInput, TriageReport
from .sub_workflow import BugCodeAnalysisWorkflow
from .workflow import SupportTriageWorkflow

__all__ = [
    "SupportTriageWorkflow",
    "BugCodeAnalysisWorkflow",
    "EmailInput",
    "TriageReport",
]
