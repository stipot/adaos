# Адаптеры SDK

SDK включает вспомогательные адаптеры, используемые при разработке и тестировании.

## Примеры

- **FsSkillRepository** — файловая реализация репозитория навыков (используется в тестах).  
- **MonoSkillRepository** — реализация для монорепозитория (runtime).  

---

## Пример использования FsSkillRepository

```python
from adaos.adapters.skills.fs_repo import FsSkillRepository
from pathlib import Path

repo = FsSkillRepository(Path("./skills"))
skills = repo.list()
print(skills)
```
