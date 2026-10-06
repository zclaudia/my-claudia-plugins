#!/usr/bin/env python3
"""Log in to https://my.telegram.org/apps and create or read a Telegram API app.

The confirmation code is delivered inside the Telegram app, not by SMS.
Telegram allows one API application per phone number. If one already exists,
this script prints that app's api_id and api_hash instead of creating another.

Two steps:

  python3 scripts/register_telegram_app.py --phone +8613800138000
  python3 scripts/register_telegram_app.py --code 12345

Session cookies and api_hash are written only to gitignored files in the
repository root.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://my.telegram.org"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION = REPO_ROOT / ".telegram-myorg-session.json"
DEFAULT_CREDENTIALS = REPO_ROOT / ".telegram-app-credentials.json"
PLATFORMS = ("android", "ios", "wp", "bb", "desktop", "web", "ubp", "other")

API_ID_RE = re.compile(
    r"App api_id:\s*</label>.*?<strong>\s*(\d+)\s*</strong>",
    re.IGNORECASE | re.DOTALL,
)
API_HASH_RE = re.compile(
    r"App api_hash:\s*</label>.*?<span[^>]*>\s*([0-9a-fA-F]{32})\s*</span>",
    re.IGNORECASE | re.DOTALL,
)
HASH_INPUT_RES = (
    re.compile(
        r"""<input[^>]*name=["']hash["'][^>]*value=["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<input[^>]*value=["']([^"']+)["'][^>]*name=["']hash["']""",
        re.IGNORECASE,
    ),
)
SHORTNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{4,31}$")


class TelegramOrgError(RuntimeError):
    pass


def normalize_phone(phone: str) -> str:
    compact = re.sub(r"[\s\-()]", "", phone.strip())
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact and compact[0].isdigit():
        compact = "+" + compact
    if not re.fullmatch(r"\+\d{8,15}", compact):
        raise TelegramOrgError(
            "手机号需要是国际格式，例如 +8613800138000。"
        )
    return compact


def cookie_jar_from_state(cookies: list[dict]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for item in cookies:
        domain = item.get("domain") or "my.telegram.org"
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=item["name"],
                value=item["value"],
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=item.get("path") or "/",
                path_specified=True,
                secure=bool(item.get("secure", True)),
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def cookies_from_jar(jar: http.cookiejar.CookieJar) -> list[dict]:
    exported = []
    for cookie in jar:
        exported.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
            }
        )
    return exported


def build_opener(jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=context),
    )


def request(
    opener: urllib.request.OpenerDirector,
    path: str,
    data: dict[str, str] | None = None,
    referer: str = f"{BASE}/auth",
    accept: str = "application/json, text/javascript, */*; q=0.01",
) -> tuple[int, str, dict[str, str]]:
    body = None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE,
        "Referer": referer,
    }
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    try:
        with opener.open(req, timeout=30) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return response.status, payload, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        return exc.code, payload, dict(exc.headers.items())


def save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def load_session(path: Path) -> dict:
    if not path.exists():
        raise TelegramOrgError(
            f"没有找到登录会话 {path.name}。请先运行 --phone 发送验证码。"
        )
    return json.loads(path.read_text())


def send_code(phone: str, session_path: Path) -> None:
    jar = http.cookiejar.CookieJar()
    opener = build_opener(jar)
    status, payload, _headers = request(opener, "/auth/send_password", {"phone": phone})
    if status != 200:
        raise TelegramOrgError(f"发送验证码失败（HTTP {status}）：{payload.strip()[:500]}")
    try:
        random_hash = json.loads(payload)["random_hash"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TelegramOrgError(f"发送验证码的响应无法解析：{payload.strip()[:500]}") from exc
    save_json(
        session_path,
        {
            "phone": phone,
            "random_hash": random_hash,
            "cookies": cookies_from_jar(jar),
        },
    )
    print("验证码已发到你的 Telegram 客户端（不是短信）。")
    print("请把验证码发过来，或在本机运行：")
    print(f"  python3 scripts/register_telegram_app.py --code <验证码>")


def parse_existing_app(html: str) -> dict[str, str] | None:
    api_id = API_ID_RE.search(html)
    api_hash = API_HASH_RE.search(html)
    if not api_id or not api_hash:
        return None
    return {"api_id": api_id.group(1), "api_hash": api_hash.group(1)}


def parse_create_hash(html: str) -> str | None:
    for pattern in HASH_INPUT_RES:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def fetch_apps(opener: urllib.request.OpenerDirector) -> str:
    status, html, _headers = request(
        opener,
        "/apps",
        data=None,
        referer=f"{BASE}/auth",
        accept="text/html,application/xhtml+xml",
    )
    if status != 200:
        raise TelegramOrgError(f"打开 /apps 失败（HTTP {status}）：{html.strip()[:500]}")
    if 'id="my_login_phone"' in html and "app_title" not in html and "App api_id" not in html:
        raise TelegramOrgError("登录没有成功，页面仍停留在手机号登录。")
    return html


def login(opener: urllib.request.OpenerDirector, phone: str, random_hash: str, code: str) -> None:
    status, payload, _headers = request(
        opener,
        "/auth/login",
        {
            "phone": phone,
            "random_hash": random_hash,
            "password": code.strip(),
            "remember": "1",
        },
    )
    if status != 200:
        raise TelegramOrgError(f"登录失败（HTTP {status}）：{payload.strip()[:500]}")
    text = payload.strip()
    if text and text not in {"true", "1"}:
        raise TelegramOrgError(f"登录被拒绝：{text[:500]}")


def create_app(
    opener: urllib.request.OpenerDirector,
    page_hash: str,
    title: str,
    shortname: str,
    url: str,
    platform: str,
    description: str,
) -> str:
    status, payload, _headers = request(
        opener,
        "/apps/create",
        {
            "hash": page_hash,
            "app_title": title,
            "app_shortname": shortname,
            "app_url": url,
            "app_platform": platform,
            "app_desc": description,
        },
        referer=f"{BASE}/apps",
    )
    if status != 200 or payload.strip().upper() == "ERROR":
        raise TelegramOrgError(
            "创建应用失败。"
            f" HTTP {status}，响应：{payload.strip()[:500] or '（空）'}。"
            " 可以换一个 --shortname 后，用同一个 --code 再试一次"
            "（验证码只能用几分钟，过期需要重新 --phone）。"
        )
    return payload


def store_credentials(path: Path, phone: str, app: dict[str, str], created: bool) -> None:
    save_json(
        path,
        {
            "phone": phone,
            "api_id": app["api_id"],
            "api_hash": app["api_hash"],
            "created": created,
        },
    )


def finish_login(
    code: str,
    session_path: Path,
    credentials_path: Path,
    title: str,
    shortname: str,
    url: str,
    platform: str,
    description: str,
    phone_arg: str | None,
) -> None:
    if not SHORTNAME_RE.fullmatch(shortname):
        raise TelegramOrgError(
            "shortname 需要以字母开头，并且只包含 5 到 32 位字母或数字。"
        )
    state = load_session(session_path)
    phone = state["phone"]
    if phone_arg and normalize_phone(phone_arg) != phone:
        raise TelegramOrgError(
            f"验证码会话属于 {phone}，与本次 --phone 不一致。请重新发送验证码。"
        )
    jar = cookie_jar_from_state(state.get("cookies") or [])
    opener = build_opener(jar)
    login(opener, phone, state["random_hash"], code)
    save_json(
        session_path,
        {
            "phone": phone,
            "random_hash": state["random_hash"],
            "cookies": cookies_from_jar(jar),
        },
    )

    html = fetch_apps(opener)
    existing = parse_existing_app(html)
    created = False
    if existing is None:
        page_hash = parse_create_hash(html)
        if not page_hash:
            debug_path = Path("/tmp/telegram-apps-page.html")
            debug_path.write_text(html)
            debug_path.chmod(0o600)
            raise TelegramOrgError(
                "登录后的页面里既没有已有应用，也没有创建表单。"
                f" 页面已保存到 {debug_path}。"
            )
        create_app(opener, page_hash, title, shortname, url, platform, description)
        created = True
        html = fetch_apps(opener)
        existing = parse_existing_app(html)
        if existing is None:
            debug_path = Path("/tmp/telegram-apps-page.html")
            debug_path.write_text(html)
            debug_path.chmod(0o600)
            raise TelegramOrgError(
                f"应用创建请求已提交，但没有读到 api_id。页面已保存到 {debug_path}。"
            )

    store_credentials(credentials_path, phone, existing, created)
    action = "已创建新应用" if created else "这个号码已经有应用，Telegram 每个号码只能有一个"
    print(action)
    print(f"api_id: {existing['api_id']}")
    print(f"api_hash: {existing['api_hash']}")
    print(f"凭证已写入 {credentials_path.name}（该文件已被 git 忽略）")


def doctor() -> None:
    jar = http.cookiejar.CookieJar()
    opener = build_opener(jar)
    status, html, _headers = request(
        opener,
        "/auth",
        data=None,
        accept="text/html,application/xhtml+xml",
    )
    if status != 200 or "/auth/send_password" not in html:
        raise TelegramOrgError(
            f"打不开 my.telegram.org 登录页（HTTP {status}）。当前网络可能被 Telegram 拦截。"
        )
    print("my.telegram.org 登录页可以访问，发送验证码的接口还在页面里。")


def self_test() -> None:
    existing_html = """
    <label for="app_id">App api_id:</label>
    <div><span class="form-control uneditable-input"><strong>1234567</strong></span></div>
    <label for="app_hash">App api_hash:</label>
    <div><span class="form-control uneditable-input" onclick="this.select();">0123456789abcdef0123456789abcdef</span></div>
    """
    create_html = """
    <form id="app_create_form" action="/apps/create">
      <input type="hidden" name="hash" value="abc123hash" />
      <input name="app_title" />
    </form>
    """
    login_html = '<input id="my_login_phone" />'
    parsed = parse_existing_app(existing_html)
    assert parsed == {
        "api_id": "1234567",
        "api_hash": "0123456789abcdef0123456789abcdef",
    }
    assert parse_create_hash(create_html) == "abc123hash"
    assert parse_existing_app(create_html) is None
    assert parse_create_hash(existing_html) is None
    assert normalize_phone("+1 222-333-4455") == "+12223334455"
    assert normalize_phone("8613800138000") == "+8613800138000"
    try:
        normalize_phone("123")
    except TelegramOrgError:
        pass
    else:
        raise AssertionError("short phone should fail")
    assert 'id="my_login_phone"' in login_html
    print("self-test ok")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在 my.telegram.org/apps 注册或读取 Telegram API 应用")
    parser.add_argument("--phone", help="国际格式手机号，例如 +8613800138000")
    parser.add_argument("--code", help="Telegram 客户端里收到的登录验证码")
    parser.add_argument("--title", default="Claudia", help="应用标题，默认 Claudia")
    parser.add_argument("--shortname", default="claudia", help="5-32 位字母数字，默认 claudia")
    parser.add_argument("--url", default="", help="应用网址，可以为空")
    parser.add_argument("--platform", default="desktop", choices=PLATFORMS)
    parser.add_argument(
        "--description",
        default="Personal Telegram API application",
        help="应用描述",
    )
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--doctor", action="store_true", help="只检查登录页能否打开")
    parser.add_argument("--self-test", action="store_true", help="运行本地解析测试")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.self_test:
            self_test()
            return 0
        if args.doctor:
            doctor()
            return 0
        if args.code:
            finish_login(
                code=args.code,
                session_path=args.session,
                credentials_path=args.credentials,
                title=args.title.strip(),
                shortname=args.shortname.strip(),
                url=args.url.strip(),
                platform=args.platform,
                description=args.description.strip(),
                phone_arg=args.phone,
            )
            return 0
        if args.phone:
            send_code(normalize_phone(args.phone), args.session)
            return 0
    except TelegramOrgError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(
        "还不能注册：需要你的手机号才能让 Telegram 发送登录验证码。\n"
        "验证码会出现在 Telegram 客户端里。拿到后用 --code 继续。\n"
        "示例：\n"
        "  python3 scripts/register_telegram_app.py --phone +8613800138000\n"
        "  python3 scripts/register_telegram_app.py --code 12345\n"
        "默认会创建标题为 Claudia、shortname 为 claudia、平台为 desktop 的应用。\n"
        "如果这个号码已经创建过应用，脚本会读取现有的 api_id 和 api_hash。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
