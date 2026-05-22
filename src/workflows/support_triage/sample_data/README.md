# Sample Emails

Five JSON inputs that exercise different paths through the
`SupportTriageWorkflow`. Use them as `make execute` inputs to verify
your local worker before plugging in real production traffic.

Trigger one with:

```bash
make execute workflow=solo-support-triage \
  input="$(cat src/workflows/support_triage/sample_data/email_01_bug_login_outage.json)"
```

| File | Language | Expected category | Expected urgency | HITL? | Code analysis? |
| --- | --- | --- | --- | --- | --- |
| `email_01_bug_login_outage.json` | EN | bug | high | yes (urgency=high) | yes if you set `codebase_path` |
| `email_02_feature_bpmn_de.json` | DE | feature_request | low | no | no |
| `email_03_question_pool_de.json` | DE | question | low | no | no |
| `email_04_billing_double_charge.json` | EN | billing | medium-to-high | yes if you keep `billing` in `hitl.pause_on_categories` | no |
| `email_05_spam_seo_pitch.json` | EN | spam | low | no | no |

## What each email tests

### 01. Bug + production outage (English)

- Triggers high-urgency routing (`oncall` queue) via the keyword `production` and the explicit `down` claim.
- Provides a JWT-style stack trace, repro steps, and version info, so `extract_entities` returns rich data.
- Mentions the BPMN process `Auftragsfreigabe`, the Team plan, and 23 affected users, so `extract_custom_outputs` fills `bpmn_process`, `customer_tier`, `affected_user_count`, and `is_blocker`.
- To exercise the code-analysis sub-workflow, set `codebase_path` to a local repo before triggering. The default is `null`, which skips the sub-workflow but keeps the rest of the pipeline.
- HITL fires because `urgency = high` is in `pause_on_urgency`.

### 02. Feature request (German)

- Tests language-matching: the reply should come back in German.
- Demonstrates that `customer_tier` is inferable from text (`auf dem Pro-Plan`).
- Mentions a new `Gateway` node type and `Compliance-Lane`, so `bpmn_node_types` and `affected_lanes` get populated.
- Hints at a partnership opportunity in the body. If you add a `partnership_inquiry` category in YAML, this email is a good test of the new category surface.

### 03. Beginner question (German)

- Free-tier signal, neutral sentiment, fast-track support routing.
- Confused user asks about Pools. `bpmn_node_types` should include `Pool` and `Lane`.
- Reply must explain how to add a second pool in German, in a friendly tone.

### 04. Billing complaint (English)

- Medium-to-high urgency depending on the angry-sentiment trigger.
- Tests routing to the `billing-queue` regardless of urgency.
- The mention of `4-person modeling team` and `Pro plan` should populate `affected_user_count = 4` and `customer_tier = pro`.
- Add `billing` to `hitl.pause_on_categories` if you want every billing email reviewed by a human.

### 05. Cold sales pitch (Spam)

- Tests the spam classifier under realistic conditions: the sender mimics a partnership inquiry, uses persuasive language, and asks for a meeting.
- A correctly tuned classifier rejects this as `spam` despite the polite framing.
- Routing goes to `trash`. No reply is sent unless you change `suggested_actions.spam` in YAML.

## Custom-output assertions you should see

After running all five emails, your downstream automation should be able
to assert these on the resulting `TriageReport.custom_outputs`:

```python
assert reports[0]["custom_outputs"]["bpmn_process"] == "Auftragsfreigabe"
assert reports[0]["custom_outputs"]["customer_tier"] == "team"
assert reports[0]["custom_outputs"]["affected_user_count"] == 23
assert reports[0]["custom_outputs"]["is_blocker"] is True

assert reports[1]["custom_outputs"]["customer_tier"] == "pro"
assert "Gateway" in reports[1]["custom_outputs"]["bpmn_node_types"]

assert reports[2]["custom_outputs"]["customer_tier"] == "free"
assert "Pool" in reports[2]["custom_outputs"]["bpmn_node_types"]

assert reports[3]["custom_outputs"]["customer_tier"] == "pro"
assert reports[3]["custom_outputs"]["affected_user_count"] == 4

assert reports[4]["category"] == "spam"
assert reports[4]["routing_queue"] == "trash"
```

These are reasonable expectations, not contracts. The LLM may legitimately
disagree on edge cases (e.g. whether `affected_user_count` should be `null`
when the number is buried in a side sentence). Re-run with a stronger model
in `models.entity_extraction` if you need higher accuracy.
