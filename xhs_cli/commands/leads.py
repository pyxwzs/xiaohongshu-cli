"""Lead discovery and review commands."""

from __future__ import annotations

import json

import click
from apscheduler.schedulers.blocking import BlockingScheduler
from rich.table import Table

from ..formatter import console, maybe_print_structured, parse_note_reference, print_success
from ..leads.ai_client import LeadAnalyzer
from ..leads.config import create_default_config, load_config
from ..leads.filter import prefilter_reason
from ..leads.models import NormalizedPost
from ..leads.normalizer import merge_detail
from ..leads.pipeline import LeadPipeline
from ..leads.storage import LeadRepository
from ._common import run_client_action, structured_output_options


@click.group()
def leads():
    """Discover and review public technical-service leads."""


@leads.command("init")
@click.option("--path", type=click.Path(dir_okay=False), default=None)
@structured_output_options
def leads_init(path: str | None, as_json: bool, as_yaml: bool):
    """Create a safe, disabled configuration for the active account."""
    try:
        created = create_default_config(path)
    except FileExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    result = {"config_path": str(created), "enabled": False}
    if not maybe_print_structured(result, as_json=as_json, as_yaml=as_yaml):
        print_success(f"Created disabled lead configuration: {created}")


@leads.command("run")
@click.option("--once", is_flag=True, default=True, help="Run one discovery cycle")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--force", is_flag=True, help="Run even when lead_agent.enabled is false")
@structured_output_options
@click.pass_context
def leads_run(ctx, once: bool, config_path: str | None, force: bool, as_json: bool, as_yaml: bool):
    """Run one lead discovery cycle."""
    config = load_config(config_path)
    if not config.enabled and not force:
        raise click.ClickException("Lead agent is disabled. Enable it in config or pass --force.")

    def action(client):
        with LeadRepository() as repository:
            analyzer = LeadAnalyzer(config)
            try:
                return LeadPipeline(client, repository, config, analyzer).run()
            finally:
                analyzer.close()

    result = run_client_action(ctx, action)
    if not maybe_print_structured(result, as_json=as_json, as_yaml=as_yaml):
        console.print_json(json.dumps(result, ensure_ascii=False))


@leads.command("schedule")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def leads_schedule(ctx, config_path: str | None):
    """Run discovery on a non-overlapping interval until interrupted."""
    config = load_config(config_path)
    if not config.enabled:
        raise click.ClickException("Lead agent is disabled in configuration.")

    def scheduled_run() -> None:
        def action(client):
            with LeadRepository() as repository:
                analyzer = LeadAnalyzer(config)
                try:
                    return LeadPipeline(client, repository, config, analyzer).run()
                finally:
                    analyzer.close()

        result = run_client_action(ctx, action)
        notifications = result.get("notifications", [])
        if notifications:
            console.print(f"[bold green]{len(notifications)} 条新高分线索[/bold green]")
            console.print_json(json.dumps(notifications, ensure_ascii=False))
        elif result.get("errors"):
            console.print_json(json.dumps(result, ensure_ascii=False))

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        scheduled_run,
        "interval",
        minutes=config.schedule_minutes,
        max_instances=1,
        coalesce=True,
        id="xhs-lead-discovery",
    )
    console.print(f"线索调度已启动：每 {config.schedule_minutes} 分钟执行一次。按 Ctrl-C 停止。")
    scheduled_run()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("线索调度已停止。")


@leads.command("list")
@click.option("--status", default=None)
@click.option("--min-score", default=0, type=click.IntRange(0, 100))
@click.option("--limit", default=50, type=click.IntRange(1, 500))
@structured_output_options
def leads_list(status: str | None, min_score: int, limit: int, as_json: bool, as_yaml: bool):
    """List stored leads."""
    with LeadRepository() as repository:
        rows = repository.list_leads(status=status, min_score=min_score, limit=limit)
    if maybe_print_structured({"items": rows}, as_json=as_json, as_yaml=as_yaml):
        return
    table = Table(title="技术服务线索")
    table.add_column("ID")
    table.add_column("状态")
    table.add_column("分数")
    table.add_column("标题")
    table.add_column("评论")
    for row in rows:
        table.add_row(row["post_id"], row["status"], str(row.get("score") or ""),
                      row["title"][:36], "?" if row["comment_count"] is None else str(row["comment_count"]))
    console.print(table)


@leads.command("show")
@click.argument("post_id")
@structured_output_options
def leads_show(post_id: str, as_json: bool, as_yaml: bool):
    """Show one stored lead."""
    with LeadRepository() as repository:
        row = repository.get_lead(post_id)
    if row is None:
        raise click.ClickException(f"Unknown lead: {post_id}")
    if not maybe_print_structured(row, as_json=as_json, as_yaml=as_yaml):
        console.print_json(json.dumps(row, ensure_ascii=False))


@leads.command("import")
@click.argument("note_url")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@structured_output_options
@click.pass_context
def leads_import(ctx, note_url: str, config_path: str | None, as_json: bool, as_yaml: bool):
    """Import and analyze one public note URL."""
    post_id, token, source = parse_note_reference(note_url)
    if not post_id:
        raise click.ClickException("Could not parse note ID from URL.")
    config = load_config(config_path)

    def action(client):
        detail = client.get_note_detail(post_id, xsec_token=token, xsec_source=source or "pc_feed")
        post = merge_detail(NormalizedPost(
            post_id=post_id,
            url=note_url,
            xsec_token=token,
            xsec_source=source or "pc_feed",
            keyword="manual_import",
        ), detail)
        analyzer = LeadAnalyzer(config)
        try:
            with LeadRepository() as repository:
                pipeline = LeadPipeline(client, repository, config, analyzer)
                post = pipeline.verify_post(post)
                run_id = repository.start_run("manual_import")
                is_new = repository.upsert_post(post, run_id)
                repository.finish_run(run_id, fetched=1, new=int(is_new), qualified=0)
                if post.comment_count is None or not post.comment_count_reliable:
                    repository.set_status(post_id, "needs_verification")
                elif config.require_zero_comments and post.comment_count > 0:
                    repository.set_status(post_id, "filtered", "comment_count > 0")
                elif (reason := prefilter_reason(post)) is not None:
                    repository.set_status(post_id, "filtered", reason)
                elif not analyzer.configured:
                    repository.set_status(post_id, "pending_ai", "AI environment variables are not configured")
                else:
                    analysis = analyzer.analyze(post)
                    repository.save_analysis(post_id, analysis, analyzer.model)
                    status = (
                        "pending_review"
                        if analysis.is_help_request and analysis.skill_match and analysis.score >= config.min_score
                        else "filtered"
                    )
                    repository.set_status(post_id, status, analysis.reason)
                return repository.get_lead(post_id)
        finally:
            analyzer.close()

    result = run_client_action(ctx, action)
    if not maybe_print_structured(result, as_json=as_json, as_yaml=as_yaml):
        console.print_json(json.dumps(result, ensure_ascii=False))


@leads.command("verify")
@click.argument("post_id")
@structured_output_options
@click.pass_context
def leads_verify(ctx, post_id: str, as_json: bool, as_yaml: bool):
    """Recheck a stored lead's current public comment count."""
    def action(client):
        with LeadRepository() as repository:
            post = repository.load_post(post_id)
            if post is None:
                raise click.ClickException(f"Unknown lead: {post_id}")
            config = load_config()
            analyzer = LeadAnalyzer(config)
            try:
                checked = LeadPipeline(client, repository, config, analyzer).verify_post(
                    post.model_copy(update={
                        "comment_count": None,
                        "comment_count_source": "unknown",
                        "comment_count_reliable": False,
                    })
                )
            finally:
                analyzer.close()
            repository.update_verification(checked)
            return {
                "post_id": post_id,
                "comment_count": checked.comment_count,
                "comment_count_source": checked.comment_count_source,
                "comment_count_reliable": checked.comment_count_reliable,
                "still_zero": checked.comment_count == 0 and checked.comment_count_reliable,
            }

    result = run_client_action(ctx, action)
    if not maybe_print_structured(result, as_json=as_json, as_yaml=as_yaml):
        console.print_json(json.dumps(result, ensure_ascii=False))


@leads.command("mark")
@click.argument("post_id")
@click.option("--status", required=True, type=click.Choice([
    "pending_review", "approved", "skipped", "contacted", "quoted", "won", "lost"
]))
@click.option("--note", default="")
@structured_output_options
def leads_mark(post_id: str, status: str, note: str, as_json: bool, as_yaml: bool):
    """Update a lead's human-review or sales status."""
    with LeadRepository() as repository:
        if repository.get_lead(post_id) is None:
            raise click.ClickException(f"Unknown lead: {post_id}")
        repository.set_status(post_id, status, note)
    result = {"post_id": post_id, "status": status}
    if not maybe_print_structured(result, as_json=as_json, as_yaml=as_yaml):
        print_success(f"Lead {post_id} marked {status}")


@leads.command("stats")
@click.option("--days", default=7, type=click.IntRange(1, 365))
@structured_output_options
def leads_stats(days: int, as_json: bool, as_yaml: bool):
    """Show lead funnel counts."""
    with LeadRepository() as repository:
        result = repository.stats(days)
    if not maybe_print_structured(result, as_json=as_json, as_yaml=as_yaml):
        console.print_json(json.dumps(result, ensure_ascii=False))
