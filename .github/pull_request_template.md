<!-- What this file does: the checklist every pull request must pass (definition of done). -->

Closes #

## What changed

## Definition of done
- [ ] CI is green (lint, types, unit + integration tests, audit, secret scan)
- [ ] The failure scenario for this change has a test (docs/scenarios.md updated)
- [ ] `docs/openapi.json` regenerated if the API changed (`uv run heatbeat-openapi`)
- [ ] Migration can go down and up again, and old code still works during the rollout
- [ ] No secrets, no new warnings
- [ ] ADR written if a real decision was made (docs/adr/)
