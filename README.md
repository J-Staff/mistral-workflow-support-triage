# From Inbox to Action — A Mistral Workflow for Support

A durable [Mistral Workflows](https://mistral.ai/news/workflows) project that triages incoming support emails for founders, indie hackers, and small teams.

Email in, structured JSON out, with optional Devstral-powered code analysis when the sender reports a bug against your codebase. Designed for indie founders who would rather ship features than write canned responses.

> **Why this project exists.** Every solopreneur drowns in support email. Generic AI auto-responders do not understand the difference between a billing question, a feature request, and a production-down bug, and they never look at your code.
> This workflow does all three in one durable Temporal-backed pipeline.

---

## What it does

Given a single support email, the workflow:

1. **Extracts in parallel** sender metadata (sentiment, language, urgency keywords),
   the primary intent (your configured categories), structured `list[str]` entities
   (error messages, features, versions, repro steps), and **typed custom outputs**
   that you define in YAML (e.g. "which BPMN process is this about?" -> string,
   "customer_tier" -> enum, "affected_user_count" -> integer).
2. **Routes deterministically.** Urgency is computed from LLM signals through a hard
   Python rule set, so every routing decision is auditable. No model can hallucinate
   a downstream queue name.
3. **Optionally runs Devstral against your codebase.** When the intent is a bug and
   you pass a local repo path, a sub-workflow scans the repo, ranks suspect files
   with Devstral, reads the most relevant ones, and returns two to four concrete fix
   options with risk and effort hints.
4. **Drafts an empathetic reply** in the sender's detected language with Mistral
   Large 3.
5. **Pauses for human approval** on high-urgency tickets via durable
   `wait_for_input()`. Zero compute cost while waiting, resumes the moment you click
   accept or decline in AI Studio or Le Chat.
6. **Returns structured JSON** ready for downstream automation: create a Linear
   issue, open a GitHub issue with fix options attached, post to Slack, log to
   Notion, or feed into your favorite mail client.

```
                      ┌──────────────────────────────────────────────┐
                      │        SupportTriageWorkflow                 │
                      │                                              │
   EMAIL ────────────▶│  Step 1: Parallel extraction (4 activities)  │
   (Pydantic input)   │    extract_metadata        ┐                 │
                      │    detect_intent           │ all from        │
                      │    extract_entities        ├─ CONFIG.models  │
                      │    extract_custom_outputs  ┘   (per-step)    │
                      │                                              │
                      │  Step 2: Deterministic routing (YAML-driven) │
                      │    is_high_urgency  (urgency rules)          │
                      │    resolve_queue    (routing rules)          │
                      │                                              │
                      │  Step 3: BugCodeAnalysisWorkflow             │──┐
                      │    (only if category=bug + repo path)        │  │ Sub-workflow
                      │                                              │  │ uses code_*
                      │  Step 4: draft_response                      │  │ models from
                      │    reply_drafting model + reply tone/length  │  │ CONFIG
                      │                                              │  │
                      │  Step 5: wait_for_input (HITL, CONFIG.hitl)  │  │
                      │    pauses on urgency, category, or low conf  │  │
                      │                                              │  │
                      │  Step 6: TriageReport (JSON)                 │──┘
                      └──────────────────────────────────────────────┘
                                          │
                                          ▼
                                   STRUCTURED JSON
                          (incl. typed custom_outputs from YAML)
```

---

## Quick start

### 1. Install dependencies

```bash
uv sync
```

### 2. Create a Mistral workspace and API key

The workflow needs a dedicated workspace so the worker can register without colliding
with other projects.

1. Visit [https://console.mistral.ai/](https://console.mistral.ai/).
2. Create a new workspace (admin permission required, ask your org admin if you do not have one). The namespace is automatically derived from your API key as  `customer_id:workspace_id`.
3. Inside the new workspace, create a fresh API key.
4. Make sure your account has access to **Mistral Workflows** (the new product announced May 2026) and to **Devstral**.
   Both are required for the code-analysis sub-workflow.

### 3. Create your `.env`

Copy the provided template and fill in your real values:

```bash
cp .env.example .env
# then open .env in your editor and set MISTRAL_API_KEY
```

The template explains every variable inline. `.env` itself is gitignored, so
your key never leaves your machine.

### 4. Start the worker

```bash
make start-worker
```

This auto-discovers `SupportTriageWorkflow` and `BugCodeAnalysisWorkflow` in
`src/workflows/`, registers them with AI Studio, and starts polling. You should see:

```
Discovered 2 workflow(s): bug-code-analysis, solo-support-triage
```

Keep this terminal open.

### 5. Trigger an execution

Open [console.mistral.ai/build/workflows](https://console.mistral.ai/build/workflows),
select `solo-support-triage`, and click **Start Workflow** with a JSON input like the ones in [`sample_data/`](src/workflows/support_triage/sample_data/).

Or trigger from the CLI in a separate terminal:

```bash
make execute workflow=solo-support-triage input="$(cat src/workflows/support_triage/sample_data/email_bug.json)"
```

You will see the parallel extraction, the routing decision, the optional Devstral sub-workflow, and the final structured `TriageReport` in the AI Studio timeline.

---

## Inputs and outputs

### Input: `EmailInput`

```json
{
  "email_id": "msg-2026-05-19-001",
  "sender_email": "alex@acme.dev",
  "sender_name": "Alex",
  "subject": "App crashes on login",
  "body": "Hi, I get a 500 error every time I try to log in. ...",
  "received_at": "2026-05-19T08:14:00Z",
  "codebase_path": "/absolute/path/to/your/repo"
}
```

`codebase_path` is optional. Provide it to enable the Devstral code-analysis sub-workflow on bug reports.

### Output: `TriageReport`

```json
{
  "email_id": "msg-2026-05-19-001",
  "sender_email": "alex@acme.dev",
  "subject": "App crashes on login",
  "category": "bug",
  "urgency": "high",
  "sentiment": "negative",
  "language": "en",
  "metadata": { "...": "..." },
  "entities": {
    "error_messages": ["500 Internal Server Error"],
    "mentioned_features": ["login"],
    "mentioned_versions": ["v3.3.0"],
    "reproduction_steps": ["Open app", "Click login", "See error"]
  },
  "custom_outputs": {
    "bpmn_process": "Auftragsfreigabe",
    "bpmn_node_types": ["Gateway", "Task"],
    "affected_lanes": ["Sales", "Finance"],
    "customer_tier": "pro",
    "affected_user_count": 12,
    "is_blocker": true
  },
  "code_analysis": {
    "suspect_files": [
      {"relative_path": "auth/login.py", "relevance_score": 0.92, "rationale": "..."}
    ],
    "root_cause_hypothesis": "Unhandled exception in JWT decode path",
    "fix_options": [
      {
        "title": "Catch and log JWT decode errors",
        "description": "Wrap the decode call in try/except and return 401 instead of 500.",
        "affected_files": ["auth/login.py"],
        "estimated_effort": "minutes",
        "risk_level": "low",
        "code_sketch": "..."
      }
    ]
  },
  "suggested_reply": "Hi Alex, thanks for the report ...",
  "suggested_actions": [
    "create_linear_issue",
    "attach_fix_options_to_issue",
    "page_oncall",
    "send_reply_after_review"
  ],
  "requires_human_approval": true,
  "approved": false,
  "routing_queue": "oncall",
  "workflow_metadata": {
    "models_used": {
      "classification": "mistral-small-latest",
      "drafting": "mistral-large-latest",
      "code_analysis": "devstral-2512"
    }
  }
}
```

---

## How others integrate this

The JSON output is intentionally automation-friendly. Three common deployment
patterns:

| Pattern                                           | How it works                                                                                                                                     | Setup effort          |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------- |
| **A. Manual trigger via AI Studio**         | Paste email body into the AI Studio JSON input, hit Start, copy the JSON output.                                                                 | 0 minutes after setup |
| **B. Email forwarder + webhook**            | Forward your support inbox to a service like Mailgun or Postmark, configure a webhook that POSTs the email payload to the workflow API endpoint. | Around 30 minutes     |
| **C. Cron polling + downstream automation** | Run a cron job that polls IMAP, triggers this workflow per new email, then pipes the JSON into Linear/GitHub/Notion via their APIs.              | Around an hour        |

Pattern A is the right starting point. Patterns B and C are clean follow-ups once you trust the workflow on your own ticket stream.

---

## Architecture decisions

### Why Mistral Workflows and not a plain Python script?

- **Durability.** If your worker crashes mid-execution, the workflow resumes from the last completed activity via Temporal's event history. A plain script would lose state.
- **Parallel activities with independent retries.** `asyncio.gather` over async calls gives you concurrency but no retry isolation. Mistral Workflows tracks each activity separately, so a flaky Devstral call retries without re-running the metadata extraction.
- **Human in the loop without polling.** `wait_for_input()` suspends the workflow at zero compute cost and resumes the instant the reviewer responds. The plain-Python equivalent is hand-rolled cron polling against a database.
- **AI Studio observability for free.** Every activity, retry, and routing decision shows up in a clickable timeline.

### Why a sub-workflow for code analysis instead of inline activities?

- **Independent retry surface.** If Devstral returns an unparseable file list, only the sub-workflow retries, not the entire triage pipeline.
- **Reusability.** The `BugCodeAnalysisWorkflow` can be invoked directly from any other workflow that wants Devstral-on-repo for a free-text bug description.
- **Clean timeline.** Parent shows one collapsible Code Analysis node instead of four interleaved Devstral activities.

### Why a deterministic routing rule instead of an LLM-routed queue?

- Routing is high-stakes. An LLM could hallucinate a queue name that does not exist downstream, breaking automation silently.
- Regulators and post-mortem reviewers need a hard rule, not a probabilistic model output, when explaining why a ticket reached a given queue.
- The category and urgency signals are already LLM-derived. Routing on top of them is purely mechanical.

---

## Customizing for your product

Everything customizable lives in a single file at the repo root:
[`triage_config.yaml`](triage_config.yaml). Edit it, restart the worker,
and the entire pipeline adapts: new categories appear in the
classifier, new typed output fields show up in the JSON, different
models swap in at each step. Zero Python changes required.

> **TL;DR.** Open `triage_config.yaml`, change what you need, run
> `make start-worker`. That is the entire customization workflow.

### What's configurable

| Section in YAML | What it controls |
| --- | --- |
| `models` | Which Mistral model is called at each of the 6 steps (small for cheap classification, large for reply drafting, Devstral or Codestral for code analysis). |
| `categories` | The intent classes the LLM picks from. Add domain-specific buckets like `vendor_inquiry`, `press_request`, or `partnership`. |
| `entities` | The generic `list[str]` extraction fields. Add as many as you want; the LLM is told to leave them empty if not present. |
| `custom_outputs` | **Typed, domain-specific extraction targets.** `string`, `list_string`, `integer`, `float`, `boolean`, or `enum`. Each one becomes a strongly-typed field in the output JSON. |
| `urgency` | High-urgency keywords, sentiment overrides, and which categories default to medium urgency. |
| `routing` | (category, urgency) -> queue-name rules, evaluated top-to-bottom with a `default` fallback. |
| `suggested_actions` | Free-form action strings appended to the report per category/urgency, plus a `default` that always runs. |
| `reply` | Reply drafting constraints: max words, tone, signature, whether to promise fix timelines, prohibited phrases. |
| `hitl` | Conditions under which the workflow pauses for human approval: urgency level, specific categories, or low intent confidence. |
| `code_analysis` | Which file extensions to scan, which directories to skip, file-count and byte-size caps. |

### Custom outputs: typed domain extraction

The single most powerful customization knob. Each entry in
`custom_outputs` turns into a strongly-typed JSON field in the
`TriageReport`, extracted by a dedicated LLM step.

Six supported types:

| Type | Python type | Use it for |
| --- | --- | --- |
| `string` | `str` | "Which BPMN process is this about?", "Customer ID quoted", "Order reference" |
| `list_string` | `list[str]` | "BPMN node types mentioned", "Affected lanes", "Pasted URLs" |
| `integer` | `int` | "Affected user count", "Order quantity", "Days waited" |
| `float` | `float` | "Refund amount mentioned", "Conversion rate quoted" |
| `boolean` | `bool` | "Is the sender currently blocked?", "Did they attach a screenshot?" |
| `enum` | one-of (closed set) | "Customer tier" -> `free`, `pro`, `team`, `enterprise` |

Example (the YAML default, geared toward a BPMN-tooling vendor):

```yaml
custom_outputs:
  - name: bpmn_process
    type: string
    required: false
    description: "Which BPMN process is the customer asking about? Empty if none."

  - name: bpmn_node_types
    type: list_string
    description: "BPMN node types mentioned (Gateway, Task, Event, Pool, Lane)."

  - name: customer_tier
    type: enum
    values: [free, pro, team, enterprise, unknown]
    required: false
    description: "Subscription tier if mentioned or clearly inferable."

  - name: affected_user_count
    type: integer
    required: false
    description: "How many users are affected, if mentioned. Null otherwise."

  - name: is_blocker
    type: boolean
    required: false
    description: "True if the sender explicitly says they are blocked."
```

Adapt to your domain by replacing the entries. For an e-commerce shop:
`order_reference: string`, `affected_skus: list_string`,
`shipping_carrier: enum [DHL, UPS, Fedex, Other]`. For a SaaS:
`affected_feature: string`, `requested_plan_change: enum`,
`reported_metric_value: float`.

### Different model per step

Six independent model slots:

```yaml
models:
  metadata_extraction: mistral-small-latest    # sentiment, language
  intent_detection: mistral-small-latest        # category
  entity_extraction: mistral-small-latest       # list[str] entities + custom_outputs
  reply_drafting: mistral-large-latest          # nuanced multi-language reply
  code_ranking: devstral-2512                   # rank suspect files
  code_fix_proposal: devstral-2512              # propose 2-4 fix options
```

Mix and match. Examples:

- **Cheap mode**: every slot on `mistral-small-latest` for low-cost classification.
- **Pro mode**: small for classification, large for reply, Devstral for code.
- **Code-first**: Devstral for ALL steps (heavier, more accurate for technical tickets).

### Custom categories

```yaml
categories:
  - name: bug
    description: "Something is broken or behaves unexpectedly. Includes 500s, regressions, crashes."
  - name: feature_request
    description: "User asks for a new capability."
  - name: vendor_inquiry          # <-- new
    description: "Procurement, RFP, or vendor onboarding requests."
  - name: press_request            # <-- new
    description: "Journalists, podcasters, or analysts asking for interviews or comments."
  - name: partnership_inquiry     # <-- new
    description: "Companies proposing co-marketing, integration, or reseller deals."
  - name: spam
    description: "Unsolicited sales, phishing, off-topic."
```

The classifier prompt is regenerated automatically from the YAML at
worker startup. No code change.

### Reply tone, length, signature

```yaml
reply:
  max_words: 220
  tone: "direct, technical, no marketing fluff"
  signature: "— Acme Support · acme-bpmn.com"
  promise_fix_timeline: false
  prohibited_phrases:
    - "I am an AI"
    - "as a language model"
    - "we apologize for any inconvenience"
```

### HITL trigger conditions

```yaml
hitl:
  pause_on_urgency: [high]                 # always pause on high urgency
  pause_on_categories: [billing]            # always pause on billing emails
  pause_on_low_confidence_below: 0.6        # also pause if intent confidence < 0.6
```

Any trigger that fires causes a pause. Set
`pause_on_low_confidence_below: 0` to disable the confidence trigger.

### Code-analysis scope

```yaml
code_analysis:
  file_extensions: [.py, .ts, .tsx]         # only scan these
  skip_directories: [.git, node_modules, build, dist, vendor]
  max_candidates_to_devstral: 30            # cap before ranking
  max_files_per_analysis: 8                  # how many full files Devstral reads
  max_bytes_per_file: 8000                   # truncate large files
```

### Workflow for forkers

1. Clone the repo.
2. Open `triage_config.yaml` and edit any section. Comments inline
   explain every field.
3. (Optional) Point `TRIAGE_CONFIG_PATH` at a different file if you
   want to keep multiple config presets side-by-side.
4. `uv sync` once.
5. `make start-worker`. The startup log prints every loaded category,
   entity, custom output, and model so you can verify your config.
6. Trigger the workflow in AI Studio. Adjust YAML, restart, repeat.

---

## Project layout

```
.
├── triage_config.yaml          # ← THE single customization file
├── pyproject.toml
├── Makefile
└── src/
    ├── entrypoints/
    │   ├── worker.py            # Recursive auto-discovery + worker startup
    │   ├── start.py             # Trigger a workflow execution from the CLI
    │   └── dev.py               # Worker with file-watch auto-reload
    └── workflows/
        └── support_triage/
            ├── __init__.py
            ├── config.py            # YAML loader + dynamic Pydantic builders
            ├── models.py            # Static Pydantic schemas
            ├── activities.py        # LLM activities (4 LLM + 1 reply)
            ├── activities_code.py   # Code-analysis activities (Devstral + FS)
            ├── sub_workflow.py      # BugCodeAnalysisWorkflow
            ├── workflow.py          # SupportTriageWorkflow (parent)
            └── sample_data/         # Sample email inputs
```

---

## Development

```bash
# Run the worker with file-watch auto-reload
make dev-worker

# Format and lint
uv run ruff format .
uv run ruff check --fix .

# Type-check (mypy)
uv run mypy src/workflows/support_triage
```

---

## License and attribution

Built on the official Mistral Workflows SDK. Patterns inspired by the public Mistral cookbooks (parallel extraction, sub-workflow fan-out, durable HITL).

Free to fork and modify for your own product. If you ship a derivative, attribution back is appreciated but not required.

---

## Credits

Use case and Pydantic schemas designed by a solopreneur for solopreneurs, with
AI pair programming for the scaffolding and prompt engineering.
Built during the [Mistral Workflow Community Challenge](https://mistral.ai), May 2026.
