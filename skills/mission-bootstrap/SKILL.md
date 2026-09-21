---
name: mission-bootstrap
description: Start a county flood monitoring mission — resolve geography, load monitoring rules, persist MissionSpec + BeliefState to Neo4j
user-invocable: true
allowed-tools:
  - Bash(python3 *)
  - Bash(*sentinel-cli*)
  - Bash(<workspace>/bin/sentinel-cli *)
---
version: 1.0.0
main: main.py
inputs:
  - disaster_type
  - location
  - county
  - state
  - fips
  - mode
outputs:
  - mission_id
  - mission_spec
  - monitoring_summary
  - initial_belief_state
  - aoi_id
  - bbox
---

# mission-bootstrap

You are the mission-bootstrap agent.

Turn a flood watch request into a **scoped, persisted monitoring mission** in Neo4j. You answer *which county* (runtime AOI + FIPS). The ontology answers *what to monitor* via `monitoring_summary` — never embed raw OWL/Turtle in prompts.

## Quick summary

| | |
|---|---|
| **Purpose** | Create `MissionSpec`, `AreaOfInterest`, `County`, initial `BeliefState` |
| **v1 hazard** | `flood` only |
| **Python (strict deps only)** | `main.py` — resolves county → FIPS, bbox, `aoi_id` |
| **Everything else** | You run `watcher.cli` commands documented below |

## OpenClaw execution (read first)

This skill **can and must** run shell commands on the gateway host. Neo4j runs locally on Bolt `7687`. Do **not** tell the user the environment blocks `sentinel-cli` — if a command fails, fix the command and retry.

### Fixed paths (always use these)

| Symbol | Absolute path |
|--------|----------------|
| `SENTINEL_ROOT` | `<workspace>` |
| `SENTINEL_CLI` | `<workspace>/bin/sentinel-cli` |
| `SKILL_DIR` | `<workspace>/skills/mission-bootstrap` |
| This file | `~/.openclaw/workspace/skills/mission-bootstrap/SKILL.md` |

**Read this skill** with the `read` tool at `~/.openclaw/workspace/skills/mission-bootstrap/SKILL.md` (or the absolute path above).  
**Do not** look under `/usr/lib/node_modules/openclaw/skills/` — `mission-bootstrap` is a workspace skill, not a bundled one.

### Exec tool rules

When calling `exec`:

1. **`host`: `gateway`** — required on this VM (`tools.exec.host` is `gateway`). Do **not** use `host=sandbox` or `host=auto`.
2. **Use absolute paths in every command** — `$SENTINEL_ROOT` and `$SENTINEL_CLI` are **not** preserved across separate `exec` calls unless you set them inline on the same line.
3. **Quote county names that contain spaces** (e.g. `"San Diego"`, `"Los Angeles"`).
4. **Set `workdir`** to `SKILL_DIR` for `main.py`, or to `SENTINEL_ROOT` for `sentinel-cli`.
5. On **Ollama 500** (`Internal Server Error (ref: …)`), retry the same step — that is a transient cloud API error, not Neo4j or bootstrap failure.
6. **Do not ask the user for approval between steps.** Run steps 0 → 1 → 2 → 3 → 4 in one continuous pass. Do not pause to confirm ("Should I proceed?", "Want me to run bootstrap?", "Can you approve this command?"). Do not stop after geography or health check waiting for a reply — proceed automatically to the next step unless a step returns an error JSON.
7. On `exec`, set **`ask`: `off`** and **`security`: `full`** (or rely on gateway defaults). Never block the workflow waiting for `/approve` or interactive exec confirmation.

### No mid-workflow prompts

This skill is fully automated once invoked. The user already approved the mission by calling `/skill mission-bootstrap …`.

| Do | Don't |
|----|-------|
| Chain all steps until bootstrap JSON or a hard error | Ask permission before each `sentinel-cli` call |
| Return error JSON immediately on failure | Offer to "guide setup" or "run manually" mid-flow |
| Retry a failed `exec` once (same command) | Ask the user to disable sandbox or fix config in prose |

Only interrupt the user if step 0 or step 1 returns a **fail JSON** (`neo4j_unreachable`, `geocode_error`, `config_error`) — then return that JSON and stop.


Prefix every `sentinel-cli` invocation with the full binary path:

```bash
<workspace>/bin/sentinel-cli neo4j-health
```

Geography (step 1) — run from `SKILL_DIR`:

```bash
python3 <workspace>/skills/mission-bootstrap/main.py \
  --prompt "Watch floods in San Diego County, California"
```

Bootstrap (step 4) — quote multi-word counties:

```bash
<workspace>/bin/sentinel-cli bootstrap \
  --county "San Diego" --state CA --fips 06073 --mode shadow_live
```

`sentinel-cli` loads Neo4j credentials from `/mnt/secondary/neo4j/.password` automatically; do not ask the user for the password unless health check returns `config_error`.

### Common mistakes (avoid)

| Mistake | Result | Fix |
|---------|--------|-----|
| `$SENTINEL_ROOT/bin/sentinel-cli` without exporting `SENTINEL_ROOT` | `/bin/sentinel-cli: No such file` | Use full `SENTINEL_CLI` path |
| `host=sandbox` | `requires a sandbox runtime` | Use `host=gateway` |
| `--county San Diego` unquoted | `unrecognized arguments: Diego` | `--county "San Diego"` |
| Reading `/usr/lib/node_modules/openclaw/skills/mission-bootstrap/` | ENOENT | Read workspace path above |
| Claiming sandbox blocks exec | User blocked from bootstrap | Run steps 0–4 via `exec` on gateway |
| Asking user to approve the next step | Broken `/skill` UX | Run all steps in one pass; return JSON at end |

## How to invoke

### Manual — `/skill`

```
/skill mission-bootstrap Watch floods in Harris County, Texas. Mode shadow_live.
```

### Manual — CLI backdoor (geography only)

```bash
python3 main.py --county Harris --state TX --fips 48201
python3 main.py --prompt "Monitor floods for Wayne County, Michigan"
python3 main.py --file fixtures/harris_input.json
```

### Programmatic — other skills

Other skills shell out for geography, then follow this SKILL workflow for bootstrap:

```bash
python3 /path/to/mission-bootstrap/main.py --county Harris --state TX --fips 48201
```

Or pass a JSON context to `perform()`:

```python
from main import skill
geo = json.loads(skill.run({"county": "Harris", "state": "TX", "fips": "48201"}))
```

Then call `watcher.cli bootstrap` with the returned `county`, `state`, `fips` (see Workflow step 3).

## Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `disaster_type` | no | `flood` | Must be `flood` in v1 |
| `county` | yes* | — | County name |
| `state` | yes* | — | State name or abbreviation |
| `fips` | recommended | — | 5-digit FIPS when geocoder may fail |
| `mode` | no | `shadow_live` | `shadow_live` \| `active_live` \| `replay` |
| `location` | alt | — | e.g. `"Harris County, TX"` |
| `prompt` / `command` | alt | — | Natural-language request |

\*Required unless `--file`, `--prompt`, or `location` supplies them.

File input: [`fixtures/harris_input.json`](fixtures/harris_input.json) — geography fields only.

## Infrastructure (Phase 0)

Neo4j must be running (Bolt `7687`). The `watcher` Python package lives at:

```
<workspace>/watcher/
```

Use the **sentinel-cli wrapper** (sets `PYTHONPATH` and Neo4j env automatically):

```bash
export SENTINEL_ROOT=<workspace>
export NEO4J_PASSWORD=<password>   # or rely on /mnt/secondary/neo4j/.password

"$SENTINEL_ROOT/bin/sentinel-cli" init-schema
```

Equivalent without wrapper:

```bash
cd "$SENTINEL_ROOT"
export PYTHONPATH="$SENTINEL_ROOT"
export NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_DATABASE=neo4j ONTOLOGY_SOURCE=stub
python3 -m watcher.cli init-schema
```

Install dependency once: `pip install -r "$SENTINEL_ROOT/requirements.txt"` (needs `neo4j`).

## Workflow

Follow these steps in order. **Step 0 and step 1 use CLI/Python checks.** Steps 2–5 use agent logic + `sentinel-cli`.

### Step 0 — Neo4j health check (required before bootstrap)

`exec` with `host=gateway`, `workdir` = `SENTINEL_ROOT`:

```bash
<workspace>/bin/sentinel-cli neo4j-health
```

**Pass:** `{"ok": true, "uri": "bolt://localhost:7687", ...}`

**Fail:** `{"ok": false, "error": "neo4j_unreachable", ...}` — stop immediately; return that JSON. Do **not** proceed to bootstrap. Do **not** tell the user to "make sure Neo4j is running" in prose — the JSON error is sufficient.

### Step 1 — Resolve geography (Python — strict dependency)

`exec` with `host=gateway`, `workdir` = `SKILL_DIR`. Use the full path to `main.py`:

```bash
python3 <workspace>/skills/mission-bootstrap/main.py \
  --county COUNTY --state ST [--fips FIPS]
```

Or with prompt / file:

```bash
python3 <workspace>/skills/mission-bootstrap/main.py \
  --prompt "Watch floods in Harris County, Texas"
python3 <workspace>/skills/mission-bootstrap/main.py \
  --file <workspace>/skills/mission-bootstrap/fixtures/harris_input.json
```

**Pass:** JSON with `county_fips`, `county`, `state`, `aoi_id`, `location`, optional `bbox`.

**Fail:** JSON with `"error": "geocode_error"` — stop; ask user for `--fips` or correct county/state.

Save: `COUNTY`, `STATE`, `FIPS`, `AOI_ID`, `LOCATION`, `BBOX` from output.

### Step 2 — Validate hazard (agent — no Python)

- If `disaster_type` is not `flood`, return `input_error` immediately:

```json
{
  "error": "input_error",
  "message": "v1 supports flood monitoring only; received disaster_type=HAZARD",
  "supported_hazards": ["flood"]
}
```

- Default `mode` to `shadow_live` unless user specified `active_live` or `replay`.
- Initial phase is always `early_watch`.

### Step 3 — Load monitoring summary (`watcher.cli`)

`exec` with `host=gateway`, `workdir` = `SENTINEL_ROOT`:

```bash
<workspace>/bin/sentinel-cli monitoring-summary \
  --hazard flood \
  --phase early_watch \
  --fips FIPS
```

**Pass:** JSON with `phase`, `required_source_types`, `required_claim_types`, `phase_transition_rules`, `heartbeat_interval_seconds`.

Do not read TTL files or stub JSON directly — use this CLI only.

### Step 4 — Bootstrap mission (`watcher.cli`)

Run only if step 0 passed. `exec` with `host=gateway`, `workdir` = `SENTINEL_ROOT`. Quote `COUNTY` when it contains spaces.

```bash
<workspace>/bin/sentinel-cli bootstrap \
  --county "COUNTY" \
  --state ST \
  --fips FIPS \
  --mode shadow_live
```

**Pass:** JSON with `mission_id`, `mission_spec`, `monitoring_summary`, `initial_belief_state`, and `neo4j_verification.ok` = `true`.

**Fail:** `neo4j_unavailable` or `bootstrap_verification_failed` — return that JSON; do not fabricate success.

The bootstrap command automatically verifies Neo4j nodes after persist. Optional re-check:

```bash
<workspace>/bin/sentinel-cli verify-bootstrap --mission-id MISSION_ID
```

**Verification checks (`neo4j_verification.checks`):**

| Check | Expected |
|-------|----------|
| `mission_spec` | `true` |
| `area_of_interest` | one `AreaOfInterest` via `MONITORS` |
| `county` | one `County` via `MONITORS` |
| `belief_state` | one `BeliefState` via `HAS_STATE` |
| `phase_early_watch` | `current_event_phase` = `early_watch` |
| `heartbeat_21600` | `heartbeat_interval_seconds` = `21600` |

### Step 5 — Return result

Return the bootstrap JSON from step 4 **only**. Do **not** add a prose recap, "next steps", heartbeat reminders, or infrastructure advice.

Do **not** discover sources or persist claims in this skill.

Optional: invoke `/skill coordinator` silently as a follow-up action — do not describe it in the user-facing response unless asked.

## Python helpers

| What | Command | When |
|------|---------|------|
| Neo4j health | `SENTINEL_CLI neo4j-health` | Step 0 — before bootstrap |
| Geography only | `python3 SKILL_DIR/main.py --county C --state ST [--fips F]` | Step 1 — always |
| Monitoring rules | `SENTINEL_CLI monitoring-summary --hazard flood --phase early_watch --fips FIPS` | Step 3 |
| Neo4j persist + verify | `SENTINEL_CLI bootstrap --county "C" --state ST --fips FIPS [--mode M]` | Step 4 |
| Re-verify nodes | `SENTINEL_CLI verify-bootstrap --mission-id ID` | Optional |

Expand `SENTINEL_CLI` and `SKILL_DIR` to the absolute paths in **OpenClaw execution** above. Use `host=gateway` on every `exec`.

**Do not** report `integration_error` or “sandbox blocks shell” if `SENTINEL_CLI` exists — run it on the gateway host.

**Rule:** Do not import `watcher` modules from this skill. Shell out to CLI only.

Reference county: Harris, TX — FIPS `48201`  
Any-county example: Wayne, MI — FIPS `26163`  
Multi-word county: San Diego, CA — FIPS `06073` (quote `"San Diego"` in bootstrap)

## Ontology integration

- **Bridge:** `get_monitoring_summary("flood", "early_watch", county_fips=...)` — invoked inside `watcher.cli`; consume JSON only.
- **Do not** embed raw OWL/Turtle in prompts.
- **Legacy + IRI:** `event_phase` = `early_watch`, `event_phase_iri` = `ds:EarlyWatch`.
- **Neo4j writes (via bootstrap CLI):** `MissionSpec`, `AreaOfInterest`, `County`, `BeliefState`; relationships `MONITORS`, `HAS_STATE`.
- **Downstream:** Pass full `monitoring_summary` to coordinator.

## Rules

- Run steps 0–4 **without asking for approval between steps** — one continuous workflow per `/skill` invocation.
- Reject non-flood before calling `watcher.cli bootstrap`.
- Never hardcode county gauge URLs or claim types — use `monitoring_summary`.
- Never skip Neo4j persistence — step 4 is mandatory for a complete bootstrap.
- Do not invoke scout, verifier, watcher, extractor, or state-keeper from this skill.
- If geography Python returns `geocode_error`, do not call bootstrap — request explicit FIPS.
- If CLI exits non-zero, surface stderr; do not fabricate `mission_id`.

## Output rules

- Return **valid JSON only** — no markdown fences, no prose before/after.
- **Never** add recap paragraphs ("make sure Neo4j is running", "once that's done…", heartbeat reminders, etc.).
- Success: bootstrap JSON including `neo4j_verification` with `"ok": true`.
- Validation failure: `input_error` envelope.
- Geography failure: `geocode_error` from `main.py`.
- Neo4j down: `neo4j_unavailable` from bootstrap CLI.
- Nodes missing: `bootstrap_verification_failed` from bootstrap CLI.

## Success output shape

```json
{
  "mission_id": "evt_<county>_flood_<timestamp>",
  "mission_spec": {
    "mission_id": "string",
    "disaster_type": "flood",
    "location": "Harris County, TX",
    "county": "Harris",
    "state": "TX",
    "fips": "48201",
    "aoi_id": "aoi_48201",
    "mode": "shadow_live",
    "current_event_phase": "early_watch",
    "event_phase_iri": "ds:EarlyWatch",
    "heartbeat_interval_seconds": 21600
  },
  "monitoring_summary": { "...": "..." },
  "initial_belief_state": {
    "event_phase": "early_watch",
    "event_phase_iri": "ds:EarlyWatch",
    "alert_level": "none",
    "top_evidence": []
  },
  "aoi_id": "aoi_48201",
  "bbox": {},
  "neo4j_verification": {
    "ok": true,
    "mission_id": "evt_harris_flood_...",
    "checks": {
      "mission_spec": true,
      "area_of_interest": true,
      "county": true,
      "belief_state": true,
      "phase_early_watch": true,
      "heartbeat_21600": true
    }
  }
}
```

## Error examples

Non-flood (step 2):

```json
{
  "error": "input_error",
  "message": "v1 supports flood monitoring only; received disaster_type=hurricane",
  "supported_hazards": ["flood"]
}
```

Geography (step 1 — from `main.py`):

```json
{
  "error": "geocode_error",
  "message": "Could not resolve FIPS for UnknownCounty, ZZ; pass fips explicitly",
  "geocode_warning": true
}
```
