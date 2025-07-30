import typer
from pathlib import Path
from adaos.commands.skill_service import SKILLS_DIR

app = typer.Typer(help="LLM prompt generation utilities")


@app.command("build-prep")
def build_prep(skill_name: str, user_request: str):
    """Build prep prompt for a skill based on user request."""
    base_prompt_path = Path("adaos/runtime/LLM/prompts/prep_request.md")
    if not base_prompt_path.exists():
        typer.echo("[red]Base prompt not found[/red]")
        raise typer.Exit(1)

    prompt = base_prompt_path.read_text(encoding="utf-8").replace("<<<USER_REQUEST>>>", user_request)

    out_dir = Path(SKILLS_DIR) / skill_name / "prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prep_prompt.md"
    out_path.write_text(prompt, encoding="utf-8")

    typer.echo(f"[green]Prep prompt for skill {skill_name} saved to {out_path}[/green]")
