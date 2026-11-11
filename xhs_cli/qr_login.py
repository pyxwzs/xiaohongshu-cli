"""QR code login for Xiaohongshu.

Supports two backends:
1. Local Chrome via Selenium.  The login page opens in the installed Chrome
   (with system fonts) and cookies are exported after the QR scan succeeds.
2. Legacy pure-HTTP login flow.  This remains as a fallback when Chrome or
   Selenium cannot be started.
"""

from __future__ import annotations

import json
import logging
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .client import XhsClient
from .cookies import get_config_dir, save_cookies
from .exceptions import NeedVerifyError, XhsApiError

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.xiaohongshu.com/login"
CHROMEDRIVER_INDEX_URL = (
    "https://cdn.npmmirror.com/binaries/chrome-for-testing/known-good-versions-with-downloads.json"
)
CHROMEDRIVER_MIRROR_TEMPLATES = (
    "https://cdn.npmmirror.com/binaries/chrome-for-testing/{version}/{platform}/{archive}",
    "https://registry.npmmirror.com/-/binary/chrome-for-testing/{version}/{platform}/{archive}",
)
GITHUB_PROXY_PREFIX = "https://v6.gh-proxy.org/"
QR_CREATE_ENDPOINT = "/api/sns/web/v1/login/qrcode/create"
QR_USERINFO_ENDPOINT = "/api/qrcode/userinfo"
QR_STATUS_ENDPOINT = "/api/sns/web/v1/login/qrcode/status"

# QR code status values
QR_WAITING = 0      # Waiting for scan
QR_SCANNED = 1      # Scanned, awaiting confirmation
QR_CONFIRMED = 2    # Login confirmed

# Poll config
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 240  # 4 minutes
BROWSER_EXPORT_COOKIE_NAMES = (
    "a1",
    "webId",
    "web_session",
    "web_session_sec",
    "id_token",
    "websectiga",
    "sec_poison_id",
    "xsecappid",
    "gid",
    "abRequestId",
    "webBuild",
    "loadts",
)


class BrowserQrLoginUnavailable(XhsApiError):
    """Raised when the browser-assisted QR backend cannot be started."""


def _emit_status(on_status: callable[[str], None] | None, msg: str) -> None:
    """Send a status message to the callback or stdout."""
    if on_status:
        on_status(msg)
    else:
        print(msg)


def _apply_session_cookies(client: XhsClient, payload: dict[str, Any]) -> None:
    """Persist any session cookies returned by the QR login endpoints."""
    login_info = payload.get("login_info", {})
    if not isinstance(login_info, dict):
        login_info = {}
    session = payload.get("session") or login_info.get("session", "")
    secure_session = payload.get("secure_session") or login_info.get("secure_session", "")
    if session:
        client.cookies["web_session"] = str(session)
    if secure_session:
        client.cookies["web_session_sec"] = str(secure_session)


def _build_saved_cookies(a1: str, webid: str, payload: dict[str, Any]) -> dict[str, str]:
    """Build the cookie payload persisted after QR login succeeds."""
    login_info = payload.get("login_info", {})
    if not isinstance(login_info, dict):
        login_info = {}
    session = payload.get("session") or login_info.get("session") or payload.get("web_session", "")
    secure_session = (
        payload.get("secure_session")
        or login_info.get("secure_session")
        or payload.get("web_session_sec", "")
    )
    cookies = {
        "a1": a1,
        "webId": webid,
    }
    if session:
        cookies["web_session"] = str(session)
    if secure_session:
        cookies["web_session_sec"] = str(secure_session)
    return cookies


def _normalize_browser_cookies(raw_cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Convert Playwright cookies into the local persisted cookie shape."""
    cookies: dict[str, str] = {}
    for entry in raw_cookies:
        name = entry.get("name")
        value = entry.get("value")
        domain = entry.get("domain", "")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name not in BROWSER_EXPORT_COOKIE_NAMES:
            continue
        if not isinstance(domain, str) or "xiaohongshu.com" not in domain:
            continue
        cookies[name] = value
    return cookies


def _unwrap_browser_response_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the inner `data` payload when browser responses use the common envelope."""
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _browser_response_payload(response: Any) -> dict[str, Any]:
    """Decode a browser response body as JSON and unwrap the common envelope."""
    try:
        data = response.json()
    except Exception as exc:
        raise XhsApiError(f"Browser response from {response.url} was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise XhsApiError(f"Browser response from {response.url} returned unexpected payload: {data!r}")
    return _unwrap_browser_response_payload(data)


def _raise_for_browser_response(response: Any) -> None:
    """Map browser QR completion failures onto the same domain errors as the HTTP client."""
    status = getattr(response, "status", None)
    if status in (461, 471):
        raise NeedVerifyError(
            verify_type=response.headers.get("verifytype", "unknown"),
            verify_uuid=response.headers.get("verifyuuid", "unknown"),
        )
    if status and status >= 400:
        try:
            body = response.text()
        except Exception:
            body = "<unavailable>"
        raise XhsApiError(
            f"Browser-assisted QR completion failed: HTTP {status} body={body[:300]}"
        )


def _wait_for_browser_login_settled(page: Any) -> None:
    """Wait briefly for the browser session and post-login page state to stabilize."""
    try:
        page.wait_for_url("**/explore*", timeout=5_000)
    except Exception:
        logger.debug("Browser-assisted QR login did not navigate to /explore before timeout")

    try:
        response = page.wait_for_response(
            lambda resp: "/api/sns/web/v2/user/me" in resp.url and resp.request.method == "GET",
            timeout=5_000,
        )
    except Exception:
        logger.debug("Browser-assisted QR login did not observe a post-login user/me response before timeout")
        return

    try:
        payload = _browser_response_payload(response)
    except Exception as exc:
        logger.debug("Failed to parse browser user/me response after QR login: %s", exc)
        return

    if bool(payload.get("guest", False)):
        logger.debug("Browser-assisted QR login settled with guest=true in user/me payload: %s", payload)


def _resolved_user_id(info: dict[str, object]) -> str:
    """Extract the current user ID from activate/self payloads."""
    if not isinstance(info, dict):
        return ""
    login_info = info.get("login_info")
    if isinstance(login_info, dict) and login_info.get("user_id"):
        return str(login_info["user_id"])
    basic = info.get("basic_info", info)
    if isinstance(basic, dict) and basic.get("user_id"):
        return str(basic["user_id"])
    if info.get("user_id"):
        return str(info["user_id"])
    if info.get("userid"):
        return str(info["userid"])
    return ""


def _complete_confirmed_session(
    client: XhsClient,
    qr_id: str,
    code: str,
    confirmed_user_id: str,
    *,
    retries: int = 5,
    wait_s: float = 1.0,
) -> dict[str, object]:
    """Finalize QR login after confirmation until the session switches users."""
    last_data: dict[str, object] = {}
    last_self_info_user_id = ""
    for attempt in range(retries):
        completion_data = client.complete_qr_login(qr_id, code)
        _apply_session_cookies(client, completion_data)
        last_data = completion_data
        completed_user_id = _resolved_user_id(completion_data)
        self_info_user_id = ""
        try:
            self_info_user_id = _resolved_user_id(client.get_self_info())
            last_self_info_user_id = self_info_user_id
        except Exception as exc:
            logger.debug(
                "QR post-confirm self info check failed attempt=%d: %s",
                attempt + 1,
                exc,
            )
        logger.debug(
            "QR post-confirm completion attempt=%d confirmed_user_id=%s "
            "completion_user_id=%s self_info_user_id=%s cookies=%s data=%s",
            attempt + 1,
            confirmed_user_id,
            completed_user_id,
            self_info_user_id,
            {
                "web_session": client.cookies.get("web_session"),
                "web_session_sec": client.cookies.get("web_session_sec"),
            },
            completion_data,
        )
        if completed_user_id and completed_user_id == confirmed_user_id:
            return completion_data
        if self_info_user_id and self_info_user_id == confirmed_user_id:
            return completion_data
        if attempt + 1 < retries:
            time.sleep(wait_s)

    raise XhsApiError(
        "QR login confirmed, but completion never returned the confirmed user session. "
        f"expected={confirmed_user_id} "
        f"completion_user={_resolved_user_id(last_data) or 'unknown'} "
        f"self_info_user={last_self_info_user_id or 'unknown'} "
        f"completion_data={last_data}"
    )


def _generate_a1() -> str:
    """Generate a fresh a1 cookie value (52 hex chars with embedded timestamp)."""
    prefix = "".join(random.choices("0123456789abcdef", k=24))
    ts = str(int(time.time() * 1000))
    suffix = "".join(random.choices("0123456789abcdef", k=15))
    return prefix + ts + suffix


def _generate_webid() -> str:
    """Generate a webId cookie value (32 hex chars)."""
    return "".join(random.choices("0123456789abcdef", k=32))


def _render_qr_half_blocks(matrix: list[list[bool]]) -> str:
    """Render QR matrix using half-block characters (▀▄█ and space)."""
    if not matrix:
        return ""

    size = len(matrix)
    lines: list[str] = []

    for row_idx in range(0, size, 2):
        line = ""
        for col_idx in range(size):
            top = matrix[row_idx][col_idx]
            bot = matrix[row_idx + 1][col_idx] if row_idx + 1 < size else False

            if top and bot:
                line += "█"
            elif top and not bot:
                line += "▀"
            elif not top and bot:
                line += "▄"
            else:
                line += " "

        lines.append(line)

    return "\n".join(lines)


def _display_qr_in_terminal(data: str) -> bool:
    """Display *data* as a QR code in the terminal.  Returns True on success."""
    try:
        import qrcode  # type: ignore[import-untyped]
    except ImportError:
        return False

    qr = qrcode.QRCode(border=4)
    qr.add_data(data)
    qr.make(fit=True)

    modules = qr.get_matrix()
    print(_render_qr_half_blocks(modules))
    return True


def _github_proxy_url(url: str) -> str:
    """Route GitHub file downloads through the gh-proxy mirror."""
    if url.startswith(GITHUB_PROXY_PREFIX):
        return url
    if url.startswith("https://github.com/"):
        return GITHUB_PROXY_PREFIX + url
    return url


def _install_github_download_proxy() -> None:
    """Send Camoufox's GitHub package download through the mirror."""
    import requests
    from camoufox import pkgman

    if getattr(pkgman, "_xhs_gh_proxy", False):
        return

    original_request = requests.sessions.Session.request

    def request(self, method, url, *args, **kwargs):
        if not isinstance(url, str):
            return original_request(self, method, url, *args, **kwargs)
        return original_request(self, method, _github_proxy_url(url), *args, **kwargs)

    requests.sessions.Session.request = request  # type: ignore[method-assign]

    original_webdl = pkgman.webdl

    def webdl(url, *args, **kwargs):
        if isinstance(url, str):
            url = _github_proxy_url(url)
        return original_webdl(url, *args, **kwargs)

    def download_file(file, url):
        proxied = _github_proxy_url(url) if isinstance(url, str) else url
        pkgman.rprint(f"Downloading package: {proxied}")
        return original_webdl(proxied, buffer=file)

    pkgman.webdl = webdl
    pkgman.CamoufoxFetcher.download_file = staticmethod(download_file)
    pkgman._xhs_gh_proxy = True


def _ensure_camoufox_ready() -> None:
    """Validate that the Camoufox package and browser binary are available."""
    try:
        import camoufox  # noqa: F401
    except ImportError as exc:
        raise BrowserQrLoginUnavailable(
            "Browser-assisted QR login requires the `camoufox` package."
        ) from exc

    try:
        result = subprocess.run(
            [sys.executable, "-m", "camoufox", "path"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserQrLoginUnavailable(
            "Unable to validate the Camoufox browser installation."
        ) from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise BrowserQrLoginUnavailable(
            "Camoufox browser runtime is missing. Run `python -m camoufox fetch` first."
        )


def _browser_assisted_qrcode_login(
    *,
    on_status: callable[[str], None] | None = None,
    timeout_s: int = POLL_TIMEOUT_S,
) -> dict[str, str]:
    """Log in by letting a real browser complete the QR flow, then export cookies."""
    _install_github_download_proxy()
    _ensure_camoufox_ready()

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise BrowserQrLoginUnavailable(
            "Camoufox sync API is unavailable in the current environment."
        ) from exc

    state = {"last_status": -1}

    _emit_status(on_status, "🔑 Starting browser-assisted QR login...")

    with Camoufox(headless=False) as browser:
        page = browser.new_page()

        def _handle_response(response) -> None:
            url = response.url
            if QR_USERINFO_ENDPOINT not in url:
                return
            try:
                payload = _unwrap_browser_response_payload(response.json())
            except Exception as exc:
                logger.debug("Failed to parse browser QR poll response: %s", exc)
                return

            code_status = int(payload.get("codeStatus", -1))
            if code_status == state["last_status"]:
                return
            state["last_status"] = code_status

            if code_status == QR_SCANNED:
                _emit_status(on_status, "📲 Scanned! Waiting for confirmation...")
            elif code_status == QR_CONFIRMED:
                _emit_status(on_status, "✅ Login confirmed!")

        page.on("response", _handle_response)

        try:
            with page.expect_response(
                lambda response: QR_CREATE_ENDPOINT in response.url and response.request.method == "POST",
                timeout=20_000,
            ) as qr_response_info:
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:
            raise XhsApiError("Failed to load Xiaohongshu login page in Camoufox.") from exc

        qr_payload = _browser_response_payload(qr_response_info.value)
        qr_url = str(qr_payload.get("url", "")).strip()
        if not qr_url:
            raise XhsApiError(f"Browser-assisted QR login did not expose a QR URL: {qr_payload}")

        _emit_status(on_status, "\n📱 Scan the QR code below with the Xiaohongshu app:\n")
        if not _display_qr_in_terminal(qr_url):
            _emit_status(on_status, "⚠️  Install 'qrcode' for terminal rendering: pip install qrcode")
            _emit_status(on_status, f"QR URL: {qr_url}")
        _emit_status(on_status, "\n⏳ Waiting for QR code scan...")

        try:
            with page.expect_response(
                lambda response: QR_STATUS_ENDPOINT in response.url and response.request.method == "GET",
                timeout=timeout_s * 1000,
            ) as completion_info:
                pass
        except Exception as exc:
            raise XhsApiError("QR code login timed out while waiting for browser completion.") from exc

        _raise_for_browser_response(completion_info.value)
        completion_data = _browser_response_payload(completion_info.value)
        login_info = completion_data.get("login_info", {})
        if not isinstance(login_info, dict):
            login_info = {}

        _wait_for_browser_login_settled(page)

        cookies = _normalize_browser_cookies(page.context.cookies())
        session = login_info.get("session")
        secure_session = login_info.get("secure_session")
        if isinstance(session, str) and session:
            cookies["web_session"] = session
        if isinstance(secure_session, str) and secure_session:
            cookies["web_session_sec"] = secure_session

        required = ("a1", "webId", "web_session")
        missing = [name for name in required if not cookies.get(name)]
        if missing:
            raise XhsApiError(
                "Browser-assisted QR login succeeded, but exported cookies were incomplete: "
                f"missing={', '.join(missing)} completion_data={completion_data}"
            )

        save_cookies(cookies)

        user_id = str(login_info.get("user_id", "")).strip() or _resolved_user_id(completion_data)
        if user_id:
            _emit_status(on_status, f"👤 User ID: {user_id}")

        return cookies


def _http_qrcode_login(
    *,
    on_status: callable[[str], None] | None = None,
    timeout_s: int = POLL_TIMEOUT_S,
) -> dict[str, str]:
    """Run the legacy pure-HTTP QR login flow."""
    a1 = _generate_a1()
    webid = _generate_webid()
    tmp_cookies = {"a1": a1, "webId": webid}

    _emit_status(on_status, "🔑 Starting QR code login...")

    with XhsClient(tmp_cookies, request_delay=0) as client:
        try:
            activate_data = client.login_activate()
            _apply_session_cookies(client, activate_data)
            guest_session = activate_data.get("session", "")
            logger.debug(
                "Initial activate: session=%s user_id=%s",
                guest_session, activate_data.get("user_id"),
            )
        except Exception as exc:
            logger.debug("Initial activate failed (non-fatal): %s", exc)

        qr_data = client.create_qr_login()
        qr_id = qr_data["qr_id"]
        code = qr_data["code"]
        qr_url = qr_data["url"]

        logger.debug("QR created: qr_id=%s, code=%s", qr_id, code)

        _emit_status(on_status, "\n📱 Scan the QR code below with the Xiaohongshu app:\n")
        if not _display_qr_in_terminal(qr_url):
            _emit_status(on_status, "⚠️  Install 'qrcode' for terminal rendering: pip install qrcode")
            _emit_status(on_status, f"QR URL: {qr_url}")
        _emit_status(on_status, "\n⏳ Waiting for QR code scan...")

        start = time.time()
        last_status = -1
        consecutive_errors = 0

        while (time.time() - start) < timeout_s:
            time.sleep(POLL_INTERVAL_S)

            try:
                status_data = client.check_qr_status(qr_id, code)
            except Exception as exc:
                logger.debug("QR status check error: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise XhsApiError(f"QR status polling failed repeatedly: {exc}") from exc
                continue
            else:
                consecutive_errors = 0

            code_status = status_data.get("codeStatus", -1)
            logger.debug("QR poll: codeStatus=%s data=%s", code_status, status_data)

            if code_status != last_status:
                last_status = code_status
                if code_status == QR_SCANNED:
                    _emit_status(on_status, "📲 Scanned! Waiting for confirmation...")
                elif code_status == QR_CONFIRMED:
                    _emit_status(on_status, "✅ Login confirmed!")

            if code_status == QR_CONFIRMED:
                confirmed_user_id = status_data.get("userId", "")
                if not confirmed_user_id:
                    raise XhsApiError("QR login confirmed but no confirmed userId was returned.")

                completion_data = _complete_confirmed_session(
                    client,
                    qr_id,
                    code,
                    confirmed_user_id,
                )
                user_id = _resolved_user_id(completion_data) or confirmed_user_id
                cookies = _build_saved_cookies(a1, webid, client.cookies)
                save_cookies(cookies)
                _emit_status(on_status, f"👤 User ID: {user_id}")
                return cookies

            elapsed = time.time() - start
            if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                _emit_status(on_status, "  Still waiting...")

    raise XhsApiError("QR code login timed out after 4 minutes")


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def _chromedriver_platform() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if sys.platform.startswith("linux"):
        return "linux64"
    if sys.platform == "win32":
        return "win64" if machine in {"amd64", "x86_64", "arm64", "aarch64"} else "win32"
    raise BrowserQrLoginUnavailable(f"当前系统不支持自动准备 ChromeDriver: {sys.platform}")


def _chrome_binary() -> Path:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise BrowserQrLoginUnavailable("没有找到本机 Chrome。请先安装 Google Chrome。")


def _chrome_version(binary: Path) -> str:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserQrLoginUnavailable(f"无法读取 Chrome 版本: {exc}") from exc
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", completed.stdout)
    if not match:
        raise BrowserQrLoginUnavailable(f"无法解析 Chrome 版本: {completed.stdout.strip()}")
    return match.group(1)


def _select_chromedriver_version(chrome_version: str, available: list[str]) -> str:
    """Pick the newest ChromeDriver that is not newer than the installed Chrome."""
    chrome_key = _version_key(chrome_version)
    same_major = [version for version in available if _version_key(version)[:1] == chrome_key[:1]]
    not_newer = [version for version in same_major if _version_key(version) <= chrome_key]
    pool = not_newer or same_major
    if not pool:
        raise BrowserQrLoginUnavailable(f"国内镜像没有 Chrome {chrome_version} 对应的 ChromeDriver。")
    return max(pool, key=_version_key)


def _chromedriver_versions(platform_name: str) -> list[str]:
    request = urllib.request.Request(CHROMEDRIVER_INDEX_URL, headers={"User-Agent": "xiaohongshu-cli"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise BrowserQrLoginUnavailable(f"无法从国内镜像读取 ChromeDriver 版本列表: {exc}") from exc
    versions: list[str] = []
    for item in payload.get("versions", []):
        downloads = item.get("downloads", {}).get("chromedriver", [])
        if any(platform_name in entry.get("url", "") for entry in downloads):
            version = str(item.get("version", ""))
            if version:
                versions.append(version)
    return versions


def _download_chromedriver(version: str, platform_name: str, destination: Path) -> None:
    archive_name = f"chromedriver-{platform_name}.zip"
    binary_name = "chromedriver.exe" if platform_name.startswith("win") else "chromedriver"
    destination.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for template in CHROMEDRIVER_MIRROR_TEMPLATES:
        url = template.format(version=version, platform=platform_name, archive=archive_name)
        tmp_zip = destination.parent / f"{archive_name}.partial"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "xiaohongshu-cli"})
            with urllib.request.urlopen(request, timeout=120) as response, tmp_zip.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            with zipfile.ZipFile(tmp_zip) as archive:
                member = next(
                    (name for name in archive.namelist() if Path(name).name == binary_name),
                    None,
                )
                if member is None:
                    raise BrowserQrLoginUnavailable(f"压缩包里没有 {binary_name}")
                extracted = destination.with_suffix(".download")
                extracted.write_bytes(archive.read(member))
                extracted.chmod(0o755)
                extracted.replace(destination)
            return
        except (OSError, urllib.error.URLError, zipfile.BadZipFile, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")
        finally:
            tmp_zip.unlink(missing_ok=True)
    detail = "; ".join(errors) or "未知错误"
    raise BrowserQrLoginUnavailable(f"ChromeDriver 下载失败: {detail}")


def _ensure_chromedriver(
    *,
    on_status: callable[[str], None] | None = None,
) -> tuple[Path, Path]:
    """Return the local Chrome binary and a matching ChromeDriver from the CN mirror."""
    binary = _chrome_binary()
    chrome_version = _chrome_version(binary)
    platform_name = _chromedriver_platform()
    binary_name = "chromedriver.exe" if platform_name.startswith("win") else "chromedriver"
    cached_root = get_config_dir() / "drivers"
    cached = sorted(cached_root.glob(f"chromedriver-*-{platform_name}/{binary_name}"))
    cached_versions = [
        path.parent.name.removeprefix("chromedriver-").removesuffix(f"-{platform_name}")
        for path in cached
    ]
    compatible = [
        version
        for version in cached_versions
        if _version_key(version)[:1] == _version_key(chrome_version)[:1]
        and _version_key(version) <= _version_key(chrome_version)
    ]
    if compatible:
        chosen = max(compatible, key=_version_key)
        return binary, cached_root / f"chromedriver-{chosen}-{platform_name}" / binary_name

    _emit_status(on_status, f"正在从国内镜像匹配 Chrome {chrome_version} 的驱动...")
    chosen = _select_chromedriver_version(chrome_version, _chromedriver_versions(platform_name))
    driver_path = cached_root / f"chromedriver-{chosen}-{platform_name}" / binary_name
    if not driver_path.is_file():
        _emit_status(on_status, f"正在下载 ChromeDriver {chosen}...")
        _download_chromedriver(chosen, platform_name, driver_path)
    return binary, driver_path


def _cookies_from_selenium(raw_cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Keep Xiaohongshu cookies exported by Selenium."""
    return _normalize_browser_cookies(raw_cookies)


def _selenium_qrcode_login(
    *,
    on_status: callable[[str], None] | None = None,
    timeout_s: int = POLL_TIMEOUT_S,
) -> dict[str, str]:
    """Open the login page in the installed Chrome and export cookies after scanning."""
    try:
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:
        raise BrowserQrLoginUnavailable(
            "扫码登录需要 selenium。请先执行: pip install selenium"
        ) from exc

    chrome_binary, driver_path = _ensure_chromedriver(on_status=on_status)
    options = Options()
    options.binary_location = str(chrome_binary)
    options.add_argument("--lang=zh-CN")
    options.add_experimental_option("prefs", {"intl.accept_languages": "zh-CN,zh"})
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    service = Service(executable_path=str(driver_path))

    _emit_status(on_status, "正在启动本机 Chrome...")
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except WebDriverException as exc:
        raise BrowserQrLoginUnavailable(f"无法启动本机 Chrome: {exc}") from exc

    try:
        _emit_status(on_status, "正在用本机 Chrome 打开小红书登录页，请在窗口里用 App 扫码。")
        driver.get(LOGIN_URL)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            cookies = _cookies_from_selenium(driver.get_cookies())
            current_url = driver.current_url or ""
            logged_in = bool(cookies.get("a1") and cookies.get("web_session")) and (
                "/login" not in current_url or bool(cookies.get("id_token"))
            )
            if logged_in:
                save_cookies(cookies)
                _emit_status(on_status, "登录成功，Cookie 已保存。")
                return cookies
            time.sleep(POLL_INTERVAL_S)
    finally:
        driver.quit()

    raise XhsApiError("扫码登录超时。请重新执行 xhs login --qrcode")


def qrcode_login(
    *,
    on_status: callable[[str], None] | None = None,
    timeout_s: int = POLL_TIMEOUT_S,
    prefer_browser_assisted: bool = False,
) -> dict[str, str]:
    """Run the QR code login flow."""
    if prefer_browser_assisted:
        try:
            return _selenium_qrcode_login(on_status=on_status, timeout_s=timeout_s)
        except BrowserQrLoginUnavailable as exc:
            logger.info("Selenium Chrome login unavailable, falling back to HTTP flow: %s", exc)

    return _http_qrcode_login(on_status=on_status, timeout_s=timeout_s)
