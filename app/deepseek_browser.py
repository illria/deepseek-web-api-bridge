from __future__ import annotations

import json
from typing import Any
from playwright.async_api import async_playwright
from app.config import settings
from app.schemas import DeepSeekState, CookieModel, CheckStateResponse

DEEPSEEK_URL = "https://chat.deepseek.com/"


def same_site_for_playwright(value: str | None) -> str | None:
    if not value:
        return None
    v = value.lower()
    if v in {"no_restriction", "none"}:
        return "None"
    if v == "lax":
        return "Lax"
    if v == "strict":
        return "Strict"
    return None


def cookie_for_playwright(cookie: CookieModel) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "secure": bool(cookie.secure),
        "httpOnly": bool(cookie.httpOnly),
    }
    if cookie.expires and not cookie.session:
        item["expires"] = float(cookie.expires)
    same_site = same_site_for_playwright(cookie.sameSite)
    if same_site:
        item["sameSite"] = same_site
    return item


def context_options_from_state(state: DeepSeekState) -> dict[str, Any]:
    env = state.env or {}
    navigator = env.get("navigator") or {}
    intl = env.get("intl") or {}
    screen = env.get("screen") or {}
    options: dict[str, Any] = {}
    if navigator.get("userAgent"):
        options["user_agent"] = navigator.get("userAgent")
    locale = intl.get("locale") or navigator.get("language")
    if locale:
        options["locale"] = locale
    if intl.get("timeZone"):
        options["timezone_id"] = intl.get("timeZone")
    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        options["viewport"] = {"width": width, "height": height}
    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)) and dpr > 0:
        options["device_scale_factor"] = float(dpr)
    return options


def storage_init_script(state: DeepSeekState) -> str:
    local = state.storage.local or {}
    session = state.storage.session or {}
    return f"""
(() => {{
  const allowed = location.hostname === "chat.deepseek.com" || location.hostname.endsWith(".deepseek.com");
  if (!allowed) return;
  const localItems = {json.dumps(local, ensure_ascii=False)};
  const sessionItems = {json.dumps(session, ensure_ascii=False)};
  for (const [key, value] of Object.entries(localItems)) {{
    try {{ localStorage.setItem(key, String(value)); }} catch (e) {{}}
  }}
  for (const [key, value] of Object.entries(sessionItems)) {{
    try {{ sessionStorage.setItem(key, String(value)); }} catch (e) {{}}
  }}
}})();
"""


async def check_deepseek_login_state(state: DeepSeekState) -> CheckStateResponse:
    launch_args: list[str] = []
    if settings.browser_no_sandbox:
        launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.browser_headless, args=launch_args)
            context = await browser.new_context(**context_options_from_state(state))
            await context.add_cookies([cookie_for_playwright(c) for c in state.cookies])
            await context.add_init_script(storage_init_script(state))
            page = await context.new_page()
            page.set_default_timeout(settings.browser_timeout_ms)
            await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
            await page.wait_for_timeout(3500)
            probe = await page.evaluate("""
() => {
  const bodyText = document.body ? document.body.innerText : "";
  const title = document.title || "";
  const textLower = bodyText.toLowerCase();
  const textareaCount = document.querySelectorAll("textarea").length;
  const editableCount = document.querySelectorAll("[contenteditable='true']").length;
  const loginPatterns = ["log in","login","sign in","sign up","登录","注册","手机号","验证码"];
  const captchaPatterns = ["captcha","verify you are human","验证你是真人","安全验证","滑块","验证码"];
  const loginTextDetected = loginPatterns.some((p) => textLower.includes(p.toLowerCase()) || bodyText.includes(p));
  const captchaTextDetected = captchaPatterns.some((p) => textLower.includes(p.toLowerCase()) || bodyText.includes(p));
  return {title, href: location.href, hasTextarea: textareaCount > 0, hasEditable: editableCount > 0, loginTextDetected, captchaTextDetected};
}
""")
            has_textarea = bool(probe.get("hasTextarea"))
            has_editable = bool(probe.get("hasEditable"))
            login_text = bool(probe.get("loginTextDetected"))
            captcha_text = bool(probe.get("captchaTextDetected"))
            logged_in_guess = bool((has_textarea or has_editable) and not login_text and not captcha_text)
            message = "登录态大概率可用：已打开 DeepSeek，并检测到可输入区域。" if logged_in_guess else "页面可打开，但无法确认登录态可用。"
            if captcha_text:
                message = "检测到验证码/安全验证，可能需要重新导入或人工验证。"
            elif login_text:
                message = "检测到登录相关内容，登录态可能已失效。"
            await context.close()
            await browser.close()
            browser = None
            return CheckStateResponse(
                ok=True,
                loggedInGuess=logged_in_guess,
                finalUrl=probe.get("href"),
                title=probe.get("title"),
                hasTextarea=has_textarea,
                hasEditable=has_editable,
                loginTextDetected=login_text,
                captchaTextDetected=captcha_text,
                message=message,
            )
    except Exception as exc:
        if browser:
            await browser.close()
        return CheckStateResponse(ok=False, loggedInGuess=False, message=f"检查失败：{type(exc).__name__}: {exc}")
