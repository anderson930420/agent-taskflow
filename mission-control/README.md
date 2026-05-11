# Agent Taskflow Mission Control

Read-only Next.js frontend for the Agent Taskflow Mission Control API.

## Scope

This frontend displays task metadata only:

- tasks
- task detail
- executor run metadata
- artifact metadata
- validation metadata
- approval metadata

This phase intentionally does not include action controls such as start, approve, reject, block, cleanup, create task, or worker dispatch.

## API base URL

By default the frontend reads from:

```text
http://127.0.0.1:8100
```

Override with:

```bash
NEXT_PUBLIC_AGENT_TASKFLOW_API_BASE_URL=http://127.0.0.1:8100
```

Do not store secrets in frontend environment variables.

## Local usage

Start the API from the repo root:

```bash
python3 -m uvicorn agent_taskflow.api.main:app --host 127.0.0.1 --port 8100
```

Start the frontend:

```bash
cd mission-control
npm run dev
```

The frontend binds only to `127.0.0.1:3001`.
