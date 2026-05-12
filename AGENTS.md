# Agent Taskflow / Mission Control Instructions

You are working in the agent-taskflow repository.

Operating rules:
- Prefer small, reviewable changes.
- Do not touch secrets, .env files, SSH keys, or system credentials.
- Do not push, merge, delete branches, or run destructive cleanup unless explicitly asked.
- Before editing, inspect relevant files and explain intended changes.
- After code changes, run the relevant tests.
- For Python changes, prefer:
  - python3 -m unittest discover -s tests -v
  - python3 -m compileall agent_taskflow scripts tests
- For mission-control frontend changes, prefer:
  - cd mission-control && npm run build
- Keep Mission Control actions explicit and auditable.
- Keep executor, validator, store, API, and frontend boundaries clean.
