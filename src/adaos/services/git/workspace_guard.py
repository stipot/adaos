from __future__ import annotations
from typing import Iterable, Sequence


def ensure_clean(git, root: str, subpaths: Sequence[str]) -> None:
    """
    Бросает RuntimeError, если в рабочем дереве есть незакоммиченные изменения
    внутри любого из указанных подпутей.
    """
    changed = git.changed_files(root)  # относительные пути от root
    if not changed:
        return
    dirty_under: list[str] = []
    norm = [s.strip("/\\") for s in subpaths if s]
    for f in changed:
        f = f.replace("\\", "/")
        for sp in norm:
            spn = sp.replace("\\", "/")
            if f == spn or f.startswith(spn + "/"):
                dirty_under.append(f)
                break
    if dirty_under:
        raise RuntimeError(
            "Working tree has uncommitted changes under managed paths:\n  - "
            + "\n  - ".join(sorted(set(dirty_under)))
            + "\n\nCommit or stash these changes before running install/remove/sync.\n"
            'Tip: adaos skill push <name> -m "msg"  или  git -C <skills_dir> stash'
        )
