import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    r"\bopen\(",
    r"\bshutil\.rmtree\(",
    r"\bos\.remove\(",
    r"\bos\.rmdir\(",
    r"\bPath\(.+?\)\.unlink\(",
    r"\bPath\(.+?\)\.write_text\(",
    r"\bPath\(.+?\)\.write_bytes\(",
]


def main():
    files = list((ROOT / "src").rglob("*.py"))
    rx = re.compile("|".join(PATTERNS))
    for f in files:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for m in rx.finditer(txt):
            print(f"check: {f.relative_to(ROOT)} : {m.group(0)}")


if __name__ == "__main__":
    main()
