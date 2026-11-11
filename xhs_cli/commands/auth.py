"""Authentication commands: login, status, logout."""

import time

import click

from ..client import XhsClient
from ..command_normalizers import normalize_xhs_user_payload
from ..cookies import (
    clear_cookies,
    get_active_account,
    get_cookies,
    save_account_profile,
    set_current_account,
)
from ..exceptions import XhsApiError
from ..formatter import (
    console,
    maybe_print_structured,
    print_success,
    render_user_info,
    success_payload,
)
from ._common import handle_errors, run_client_action, structured_output_options


def _emit_payload(data: dict[str, object], *, as_json: bool, as_yaml: bool) -> bool:
    """Emit a structured success payload when requested."""
    return maybe_print_structured(success_payload(data), as_json=as_json, as_yaml=as_yaml)


def _is_valid_login(user: dict[str, object]) -> bool:
    """Check whether the normalized user payload represents a real logged-in session."""
    if user.get("guest"):
        return False
    nickname = user.get("nickname", "")
    return bool(nickname and nickname != "Unknown")


def _save_profile(user: dict[str, object]) -> str:
    """Save display metadata for the active account."""
    account = get_active_account()
    save_account_profile({
        "nickname": str(user.get("nickname") or ""),
        "red_id": str(user.get("red_id") or ""),
        "user_id": str(user.get("id") or ""),
    })
    return account


def _remember_login(user: dict[str, object]) -> str:
    """Save profile metadata and make this login the current account."""
    account = _save_profile(user)
    set_current_account(account)
    return account


def _print_login_success(user: dict[str, object], account: str) -> None:
    """Print a concise login success message."""
    print_success(f"Logged in as: {user['nickname']} (ID: {user['red_id']}) [account: {account}]")


def _print_status_summary(user: dict[str, object]) -> None:
    """Render a short authenticated-user summary."""
    console.print("[bold green]✓ Logged in[/bold green]")
    console.print(f"  账号: [bold]{get_active_account()}[/bold]")
    console.print(f"  昵称: [bold]{user['nickname']}[/bold]")
    if user["red_id"]:
        console.print(f"  小红书号: {user['red_id']}")
    if user["ip_location"]:
        console.print(f"  IP 属地: {user['ip_location']}")
    if user["desc"]:
        console.print(f"  简介: {user['desc']}")

@click.command()
@click.option(
    "--cookie-source",
    type=str,
    default=None,
    help="Browser to read cookies from (default: auto-detect all installed browsers)",
)
@structured_output_options
@click.option("--qrcode", "use_qrcode", is_flag=True, default=False,
              help="Open the login page in local Chrome and scan the QR code")
@click.pass_context
def login(ctx, cookie_source: str | None, as_json: bool, as_yaml: bool, use_qrcode: bool):
    """Log in by extracting cookies from browser, or via QR code."""

    if use_qrcode:
        def _login_with_qrcode() -> None:
            from ..qr_login import qrcode_login

            cookies = qrcode_login(prefer_browser_assisted=True)

            # Verify by fetching user info (may return guest=true briefly)
            import time
            time.sleep(1)  # brief delay for session propagation
            with XhsClient(cookies) as client:
                info = client.get_self_info()
            user = normalize_xhs_user_payload(info)

            account = _remember_login(user)
            if user["guest"]:
                # Session not yet propagated; still valid
                if not _emit_payload(
                    {"authenticated": True, "account": account, "user": {"id": user["id"]}},
                    as_json=as_json,
                    as_yaml=as_yaml,
                ):
                    print_success(f"Logged in (session saved) [account: {account}]")
            else:
                if not _emit_payload(
                    {"authenticated": True, "account": account, "user": user},
                    as_json=as_json,
                    as_yaml=as_yaml,
                ):
                    _print_login_success(user, account)

        handle_errors(
            _login_with_qrcode,
            as_json=as_json,
            as_yaml=as_yaml,
            prefix="QR login failed",
        )
        return

    # Browser cookie extraction (default)
    if cookie_source is None:
        cookie_source = ctx.obj.get("cookie_source", "auto") if ctx.obj else "auto"

    def _login_with_browser() -> None:
        browser, cookies = get_cookies(cookie_source, force_refresh=True)
        print_success(f"Cookies extracted from {browser}")

        # Verify by fetching user info, retry once if session not yet propagated
        with XhsClient(cookies) as client:
            info = client.get_self_info()
        user = normalize_xhs_user_payload(info)

        if not _is_valid_login(user):
            time.sleep(2.5)
            with XhsClient(cookies) as client:
                info = client.get_self_info()
            user = normalize_xhs_user_payload(info)

        if not _is_valid_login(user):
            raise XhsApiError(
                "Browser cookies were extracted, but the session appears invalid "
                "(guest or incomplete profile). Try: xhs login --qrcode"
            )

        account = _remember_login(user)
        if not _emit_payload(
            {"authenticated": True, "account": account, "user": user},
            as_json=as_json,
            as_yaml=as_yaml,
        ):
            _print_login_success(user, account)

    handle_errors(
        _login_with_browser,
        as_json=as_json,
        as_yaml=as_yaml,
        prefix="Login verification failed",
    )


@click.command()
@structured_output_options
@click.pass_context
def status(ctx, as_json: bool, as_yaml: bool):
    """Check current login status and user info."""
    def _show_status() -> None:
        info = run_client_action(ctx, lambda client: client.get_self_info())
        user = normalize_xhs_user_payload(info)
        account = _save_profile(user)

        if not _emit_payload(
            {"authenticated": True, "account": account, "user": user},
            as_json=as_json,
            as_yaml=as_yaml,
        ):
            _print_status_summary(user)

    handle_errors(_show_status, as_json=as_json, as_yaml=as_yaml, prefix="Status check failed")


@click.command()
@structured_output_options
@click.pass_context
def logout(ctx, as_json: bool, as_yaml: bool):
    """Clear saved cookies for the active account and log out."""
    account = get_active_account()
    clear_cookies()
    if not _emit_payload({"logged_out": True, "account": account}, as_json=as_json, as_yaml=as_yaml):
        print_success(f"Logged out — cookies cleared [account: {account}]")


@click.command()
@structured_output_options
@click.pass_context
def whoami(ctx, as_json: bool, as_yaml: bool):
    """Show detailed profile of current user (level, fans, likes)."""
    def _show_profile() -> None:
        info = run_client_action(ctx, lambda client: client.get_self_info())
        user = normalize_xhs_user_payload(info)
        account = _save_profile(user)

        if not _emit_payload({"account": account, "user": user}, as_json=as_json, as_yaml=as_yaml):
            render_user_info(info)

    handle_errors(_show_profile, as_json=as_json, as_yaml=as_yaml, prefix="Failed to get profile")
