You generate the **discovery/prep** artifacts for an AdaOS skill.

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

From the user's goal below, produce:

1) `prep/prepare.py` — a single-file Python script that runs the preparation stage.
2) `skill_prompt_data.md` — a concise markdown summary of collected facts for the *next* LLM step (skill coding).
3) `skill.yaml.suggested` — a **minimal, valid** AdaOS manifest that passes validation and includes any runtime **dependencies** required by `prepare.py` (e.g., `requests>=2.31`).

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
    1) discover env/config
    2) test prerequisites (small timeouts)
    3) write artifacts into skill_path / 'prep'
    4) return the prep_result dict
    """
    ...
    return prep_result

def lang_res() -> Dict[str, str]:
    # return English defaults for all i18n keys used in prompts/logs
    return {...}
````

Rules:

* All user-facing text (input/print/log) goes through i18n `_('key')`. Keep keys stable and short.
* Create `prep_dir = skill_path / "prep"`; ensure it exists.
* Log into `prep_dir / "prep.log"`.
* Network: use small timeouts (≤ 5s), `requests` allowed, otherwise stdlib only.
* Interactivity (`input`) only if necessary and after trying sensible defaults.

Write **both** files at the end:

`prep_result.json` (to `prep_dir`) — strict shape:

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

Notes: `resources` may be `{}`, `tested_hypotheses` may be `[]`.

`skill_prompt_data.md` (root, **NOT** in `prep/`) — markdown for the next LLM step:

```
# Skill Prep: Collected Facts

## User Goal
<short restatement>

## Discovered Resources
- **<key>**: <value>

## Tested Hypotheses
- ✅ <name>
- ❌ <name>

## Open Questions
- <optional bullets for next step>
```

### B) `skill.yaml.suggested`

Provide a **minimal, valid** YAML that passes AdaOS validator (empty skill is OK):

```yaml
name: "<skill_name>"
version: "0.1.0"
entry: "handlers/main.py"

runtime:
  python: "3.11"

description: ""
dependencies: []      # include "requests>=2.31" if used in prepare.py
events: {}            # or events: { subscribe: [], publish: [] }
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
