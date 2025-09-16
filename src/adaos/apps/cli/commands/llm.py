import typer
from pathlib import Path
from adaos.services.agent_context import get_ctx
from adaos.apps.cli.i18n import _

app = typer.Typer(help=_("cli.llm.help"))


@app.command("build-prep")
def build_prep(skill_name: str, user_request: str):
    """
    Build prep prompt for a skill based on user request and save it in <skills>/<skill_name>/prep/prep_prompt.md
    """
    ctx = get_ctx()
    base_prompt_path = Path(f"{ctx.paths.package_dir}/sdk/llm/prompts/prep_request.md")
    if not base_prompt_path.exists():
        typer.echo(f"[red]{_('cli.llm.prep.template_missing')}[/red]")
        raise typer.Exit(1)

    miniface_prompt_path = Path(f"{ctx.paths.package_dir}/sdk/llm/prompts/adaos_skills_miniface.md")
    if not miniface_prompt_path.exists():
        typer.echo(f"[red]{_('cli.llm.prep.template_miniface_missing')}[/red]")
        raise typer.Exit(1)

    # Подставляем запрос пользователя
    prompt = (
        base_prompt_path.read_text(encoding="utf-8").replace("<<<USER_REQUEST>>>", user_request).replace("<<<ADAOS_MINIFACE>>>", miniface_prompt_path.read_text(encoding="utf-8"))
    )

    # Путь к файлу навыка
    out_dir = ctx.paths.skills_dir() / skill_name / "prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prep_prompt.md"
    out_path.write_text(prompt, encoding="utf-8")

    typer.echo(f"[green]{_('cli.llm.prep.saved', skill_name=skill_name, out_path=out_path)}[/green]")


@app.command("build-skill")
def build_skill(skill_name: str, user_request: str):
    """
    Build skill prompt based on user request and existing prep_result.json
    """
    ctx = get_ctx()
    base_prompt_path = Path(f"{ctx.paths.package_dir}/sdk/llm/prompts/skill_request.md")
    if not base_prompt_path.exists():
        typer.echo(f"[red]{_('cli.llm.skill.template_missing')}[/red]")
        raise typer.Exit(1)

    # Загружаем prep_result.json
    prep_result_path = ctx.paths.skills_dir() / skill_name / "prep_result.json"
    if not prep_result_path.exists():
        print(prep_result_path)
        typer.echo(f"[red]{_('cli.llm.skill.prep_missing', skill_name=skill_name)}[/red]")
        raise typer.Exit(1)

    prep_result = prep_result_path.read_text(encoding="utf-8")

    # Подставляем user_request и prep_result.json в шаблон
    prompt_template = base_prompt_path.read_text(encoding="utf-8")
    prompt = prompt_template.replace("<<<USER_REQUEST>>>", user_request).replace("<<<PREP_RESULT_JSON>>>", prep_result).replace("<<<SKILL_NAME>>>", skill_name)

    # Сохраняем prompt
    out_dir = ctx.paths.skills_dir() / skill_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "skill_prompt.md"
    out_path.write_text(prompt, encoding="utf-8")

    typer.echo(f"[green]{_('cli.llm.skill.saved', skill_name=skill_name, out_path=out_path)}[/green]")
