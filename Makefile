.PHONY: start-worker execute installdeps

## Install dependencies
installdeps:
	uv sync

## Auto-discover all workflows and start the worker (with file-watch auto-reload)
start-worker:
	PYTHONPATH=src uv run --no-sync python -m entrypoints.dev

## Trigger a workflow execution
## Usage: make execute workflow=solo-support-triage input="$(cat src/workflows/support_triage/sample_data/email_01_bug_login_outage.json)"
execute:
	PYTHONPATH=src uv run --no-sync python -m entrypoints.start $(if $(workflow),--workflow $(workflow),) $(if $(input),--input '$(input)',)
