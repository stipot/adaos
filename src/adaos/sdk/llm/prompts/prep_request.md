You generate **discovery/prep** artifacts for an AdaOS skill.

Return **only files** using this exact multi-file protocol (no extra text):

<<<FILE: prep/prepare.py>>>

# (python code)

<<<END>>>

<<<FILE: skill_prompt_data.md>>>

# (markdown)

<<<END>>>

<<<FILE: skill.yaml.suggested>>>

# (yaml)

<<<END>>>

---

## Task

From the user’s goal below, produce:

1) `prep/prepare.py` — single-file Python script for the preparation stage.
2) `skill_prompt_data.md` — concise markdown with facts for the next LLM step (skill coding).
3) `skill.yaml.suggested` — minimal valid AdaOS manifest, including any runtime **dependencies** required by `prepare.py`.

**User goal:**  
`<<<USER_REQUEST>>>`

---

## AdaOS mini‑interfaces you may use

<<<ADAOS_MINIFACE>>>

---

## Contracts

### A) `prep/prepare.py`

Produce valid Python 3.11+ with exactly:

```python
from pathlib import Path
from typing import Dict, Any
import json, logging, datetime

from adaos.sdk.skills.i18n import _  # i18n keys required

def run_prep(skill_path: Path) -> Dict[str, Any]:
    """
    1) discover env/config (prefer stored values),
    2) test prerequisites (timeouts ≤ 5s),
    3) write artifacts to skill_path/'prep',
    4) return the prep_result dict.
    """
    ...
    return prep_result

def lang_res() -> Dict[str, str]:
    # English defaults for all i18n keys used
    return {...}
````

Rules:

* All user‑facing text (input/print/log) via i18n `_('key')`. **Keys must be short snake‑case** (e.g., `prep.start`, `prep.ask.token`, `prep.err.timeout`), not English sentences.
* Create `prep_dir = skill_path / "prep"` and ensure it exists.
* Logging: use a **dedicated logger** with `FileHandler` to `prep_dir / "prep.log"`; do **not** call `logging.basicConfig`.
* Persistence: use AdaOS helpers, **not** `os.environ`:

  Prefer existing values (`get_env`) and store discovered ones (`set_env`).
* Network: only stdlib + `requests`; timeouts ≤ 5s; robust JSON handling; fail fast with clear i18n reasons.
* Interactivity: ask via `input()` **only if necessary**; keep prompts minimal. Sending a **test message** requires explicit user consent (record the decision).
* Secrets: **never** print or write full secrets to human‑readable files; mask tokens in any markdown/log output. **Do not embed tokens in URLs** in any output.
* Always write both files and return the dict.

Artifacts written by `run_prep` into `prep_dir`:

* `prep_result.json` — exact shape:

```json
{
  "status": "ok | failed",
  "timestamp": "<UTC ISO8601>",
  "reason": "<string only when failed>",
  "resources": { "key": "value" },
  "tested_hypotheses": [
    { "name": "<string>", "result": true, "critical": true }
  ]
}
```

Notes: `resources` may be `{}`; `tested_hypotheses` may be `[]`. Use stable names like `token.valid`, `message.send`, with appropriate `critical`.

* Include "reason" ONLY when status="failed".
* Always include "tested_hypotheses" with explicit "critical" flags; record both successes and relevant failures.

Additionally, write (to **skill root**, not in `prep/`):

* `skill_prompt_data.md` — markdown:

```
# Skill Prep: Collected Facts

## User Goal
<short restatement>

## Discovered Resources
- **<key>**: <masked or non‑sensitive value>

## Tested Hypotheses
- ✅ <name>
- ❌ <name>

## Preparation Status
- ✅ Successful
# or
- ❌ Failed: <reason>

## Open Questions
- <optional bullets>
```

Mask secrets; do not include token‑bearing URLs.

### B) `skill.yaml.suggested`

Minimal valid YAML that passes AdaOS validation:

```yaml
name: "<skill_name>"
version: "0.1.0"
entry: "handlers/main.py"

runtime:
  python: "3.11"

description: ""
dependencies: []      # include "requests>=2.31" if imported in prepare.py
events: {}            # or { subscribe: [], publish: [] }
tools: []
exports: {}
```

Adjust:

* `name`: kebab/underscore form inferred from the user goal (e.g., `weather_skill`).
* Add dependencies needed by your `prepare.py`. If you import `requests`, include it here.

Do **not** invent handlers/tools here — this is prep stage only.

---

## Hints (only if relevant)

* For weather: default endpoint `https://api.openweathermap.org/data/2.5/weather`.
* Try non-interactive probes first; ask via `input()` only if nothing reasonable can be assumed.
* Normalize/strip user inputs; keep `prep_result.json` deterministic.
* Validate token with GET <https://api.telegram.org/bot><token>/getMe
* Optionally discover a chat_id via getUpdates if user left it blank (handle missing fields defensively).
* Only send a test message after explicit consent.
