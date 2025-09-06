import typer
from pathlib import Path
from adaos.sdk.skills.i18n import _
from adaos.sdk.context import SKILLS_DIR, PACKAGE_DIR

app = typer.Typer(help=_("cli.llm.help"))


@app.command("build-prep")
def build_prep(skill_name: str, user_request: str):
    """
    Build prep prompt for a skill based on user request and save it in <skills>/<skill_name>/prep/prep_prompt.md
    """
    base_prompt_path = Path(f"{PACKAGE_DIR}/sdk/llm/prompts/prep_request.md")
    if not base_prompt_path.exists():
        typer.echo(f"[red]{_('cli.llm.prep.template_missing')}[/red]")
        raise typer.Exit(1)

    miniface_prompt_path = Path(f"{PACKAGE_DIR}/sdk/llm/prompts/adaos_skills_miniface.md")
    if not miniface_prompt_path.exists():
        typer.echo(f"[red]{_('cli.llm.prep.template_miniface_missing')}[/red]")
        raise typer.Exit(1)

    # Подставляем запрос пользователя
    prompt = (
        base_prompt_path.read_text(encoding="utf-8").replace("<<<USER_REQUEST>>>", user_request).replace("<<<ADAOS_MINIFACE>>>", miniface_prompt_path.read_text(encoding="utf-8"))
    )

    # Путь к файлу навыка
    out_dir = Path(SKILLS_DIR) / skill_name / "prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prep_prompt.md"
    out_path.write_text(prompt, encoding="utf-8")

    typer.echo(f"[green]{_('cli.llm.prep.saved', skill_name=skill_name, out_path=out_path)}[/green]")


@app.command("build-skill")
def build_skill(skill_name: str, user_request: str):
    """
    Build skill prompt based on user request and existing prep_result.json
    """
    base_prompt_path = Path(f"{PACKAGE_DIR}/sdk/llm/prompts/skill_request.md")
    if not base_prompt_path.exists():
        typer.echo(f"[red]{_('cli.llm.skill.template_missing')}[/red]")
        raise typer.Exit(1)

    # Загружаем prep_result.json
    prep_result_path = Path(SKILLS_DIR) / skill_name / "prep_result.json"
    if not prep_result_path.exists():
        print(prep_result_path)
        typer.echo(f"[red]{_('cli.llm.skill.prep_missing', skill_name=skill_name)}[/red]")
        raise typer.Exit(1)

    prep_result = prep_result_path.read_text(encoding="utf-8")

    # Подставляем user_request и prep_result.json в шаблон
    prompt_template = base_prompt_path.read_text(encoding="utf-8")
    prompt = prompt_template.replace("<<<USER_REQUEST>>>", user_request).replace("<<<PREP_RESULT_JSON>>>", prep_result).replace("<<<SKILL_NAME>>>", skill_name)

    # Сохраняем prompt
    out_dir = Path(SKILLS_DIR) / skill_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "skill_prompt.md"
    out_path.write_text(prompt, encoding="utf-8")

    typer.echo(f"[green]{_('cli.llm.skill.saved', skill_name=skill_name, out_path=out_path)}[/green]")
