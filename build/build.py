import os
import json
from pathlib import Path

PROFILE = os.environ.get("BUILD_PROFILE", "public")
CONFIG_PATH = Path(f"build/config.{PROFILE}.json")
TEMPLATE_PATH = Path("pyproject.toml")
OUTPUT_PATH = Path("pyproject.toml")

START_MARKER = "# EXTRA_MODULES_START"
END_MARKER = "# EXTRA_MODULES_END"

DEFAULT_PUBLIC_MODULES = ["adaos.agent", "adaos.sdk", "adaos.devportal", "adaos.integrations", "adaos.sdk.utils.common"]


def insert_between_markers(content: str, new_block: str, start_marker: str, end_marker: str) -> str:
    lines = content.splitlines()
    try:
        start_index = lines.index(start_marker)
        end_index = lines.index(end_marker)
    except ValueError:
        raise ValueError("Markers not found in template")

    return "\n".join(lines[: start_index + 1] + [new_block.strip()] + lines[end_index:])


def render(config):
    tpl = TEMPLATE_PATH.read_text()
    include = ",\n  ".join(f'"{m}"' for m in DEFAULT_PUBLIC_MODULES + config.get("include", []))
    exclude = ",\n  ".join(f'"{m}"' for m in config.get("exclude", []))
    new_block = f"include = [\n  {include}\n]\nexclude = [\n  {exclude}\n]"
    return insert_between_markers(tpl, new_block, START_MARKER, END_MARKER)


def main():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text())
    content = render(config)
    OUTPUT_PATH.write_text(content)
    print(f"[✓] Generated pyproject.gen.toml from profile: {PROFILE}")


if __name__ == "__main__":
    main()
