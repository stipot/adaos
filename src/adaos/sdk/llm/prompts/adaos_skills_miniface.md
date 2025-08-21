- You receive `skill_path: Path`. Write artifacts to:
  - `skill_path / "prep" / "prep_result.json"`
  - `skill_path / "prep" / "prep.log"`
  - `skill_prompt_data.md` in the **skill root** (next-step prompt data)

- i18n:

```python
from adaos.sdk.skills.i18n import _
def lang_res() -> dict: return { "key": "English text", ... }
```
