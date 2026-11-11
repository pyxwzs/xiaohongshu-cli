"""Account commands: list, switch, and remove saved logins."""

import click
from rich.table import Table

from ..cookies import AccountNameError, get_active_account, list_account_records, remove_account, use_account
from ..formatter import console, emit_error, maybe_print_structured, print_error, print_success
from ._common import structured_output_options


def _emit_accounts(as_json: bool, as_yaml: bool) -> None:
    records = list_account_records()
    current = next((record["name"] for record in records if record["current"]), get_active_account())
    if maybe_print_structured(
        {"current": current, "accounts": records},
        as_json=as_json,
        as_yaml=as_yaml,
    ):
        return

    table = Table(title="账号", show_lines=True)
    table.add_column("", width=2)
    table.add_column("账号", width=16)
    table.add_column("昵称", width=16)
    table.add_column("小红书号", width=14)
    table.add_column("状态", width=8)
    for record in records:
        table.add_row(
            "*" if record["current"] else "",
            record["name"],
            record["nickname"],
            record["red_id"],
            "已登录" if record["authenticated"] else "未登录",
        )
    console.print(table)
    console.print("[dim]当前账号标有 *。临时使用其他账号：xhs --account 名称 …[/dim]")


def _fail(exc: AccountNameError, *, as_json: bool, as_yaml: bool) -> None:
    if emit_error("api_error", str(exc), as_json=as_json, as_yaml=as_yaml):
        raise SystemExit(1) from None
    print_error(str(exc))
    raise SystemExit(1) from None


@click.command("accounts")
@structured_output_options
def accounts(as_json: bool, as_yaml: bool):
    """List saved accounts."""
    _emit_accounts(as_json, as_yaml)


@click.group()
def account():
    """Manage saved accounts."""


@account.command("list")
@structured_output_options
def account_list(as_json: bool, as_yaml: bool):
    """List saved accounts."""
    _emit_accounts(as_json, as_yaml)


@account.command("use")
@click.argument("name")
@structured_output_options
def account_use(name: str, as_json: bool, as_yaml: bool):
    """Switch the current account."""
    try:
        current = use_account(name)
    except AccountNameError as exc:
        _fail(exc, as_json=as_json, as_yaml=as_yaml)
        return
    if not maybe_print_structured({"current": current}, as_json=as_json, as_yaml=as_yaml):
        print_success(f"Current account: {current}")


@account.command("remove")
@click.argument("name")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@structured_output_options
def account_remove(name: str, yes: bool, as_json: bool, as_yaml: bool):
    """Delete an account and its saved cookies."""
    if not yes:
        click.confirm(f"Remove account {name!r} and its saved cookies?", abort=True)
    try:
        removed = remove_account(name)
    except AccountNameError as exc:
        _fail(exc, as_json=as_json, as_yaml=as_yaml)
        return
    if not maybe_print_structured({"removed": removed}, as_json=as_json, as_yaml=as_yaml):
        print_success(f"Removed account: {removed}")
