from __future__ import annotations

import asyncio
import time
import re
import html
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
from app.config import settings
from app.deepseek_browser import DEEPSEEK_URL, context_options_from_state, cookie_for_playwright, storage_init_script
from app.runtime_settings import runtime_store
from app.schemas import AskResponse, DeepSeekState, WorkerStatusResponse
from app.utils import now_iso


class DeepSeekWorker:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._busy = False
        self._reserved = False
        self._last_started_at: str | None = None
        self._last_ask_at: str | None = None
        self._last_error: str | None = None
        self._restart_count = 0
        self._consecutive_failures = 0
        self._last_recovered_at: str | None = None
        self._session_urls: dict[str, str] = {}
        self._session_pages: dict[str, Page] = {}
        self._active_session_id: str | None = None

    def reserve(self) -> bool:
        if self._busy or self._reserved:
            return False
        self._reserved = True
        return True

    def release(self) -> None:
        self._reserved = False

    async def status(self) -> WorkerStatusResponse:
        final_url = None
        title = None
        if self._page and not self._page.is_closed():
            try:
                final_url = self._page.url
                title = await self._page.title()
            except Exception:
                pass
        return WorkerStatusResponse(
            accountId=self.account_id,
            running=bool(self._browser and self._page and not self._page.is_closed()),
            busy=self._busy,
            reserved=self._reserved,
            finalUrl=final_url,
            title=title,
            lastStartedAt=self._last_started_at,
            lastAskAt=self._last_ask_at,
            lastError=self._last_error,
            restartCount=self._restart_count,
            consecutiveFailures=self._consecutive_failures,
            lastRecoveredAt=self._last_recovered_at,
        )

    async def start(self, state: DeepSeekState) -> WorkerStatusResponse:
        async with self._lock:
            await self._start_unlocked(state)
            return await self.status()

    async def restart(self, state: DeepSeekState) -> WorkerStatusResponse:
        async with self._lock:
            await self._start_unlocked(state, force_restart=True)
            self._restart_count += 1
            self._last_recovered_at = now_iso()
            return await self.status()

    async def _start_unlocked(self, state: DeepSeekState, force_restart: bool = False) -> None:
        if force_restart:
            await self._close_unlocked()
        if self._browser and self._page and not self._page.is_closed():
            return
        launch_args: list[str] = []
        if settings.browser_no_sandbox:
            launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=settings.browser_headless, args=launch_args)
        self._context = await self._browser.new_context(**context_options_from_state(state))
        self._context.set_default_timeout(settings.browser_timeout_ms)
        await self._context.grant_permissions(["clipboard-read", "clipboard-write"], origin=DEEPSEEK_URL)
        await self._context.add_cookies([cookie_for_playwright(c) for c in state.cookies])
        await self._context.add_init_script(storage_init_script(state))
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.browser_timeout_ms)
        await self._page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
        await self._page.wait_for_timeout(3000)
        self._last_started_at = now_iso()
        self._last_error = None

    async def stop(self) -> WorkerStatusResponse:
        async with self._lock:
            await self._close_unlocked()
            return await self.status()

    async def reset_conversation(self, state: DeepSeekState) -> WorkerStatusResponse:
        async with self._lock:
            await self._start_unlocked(state)
            if not self._page or self._page.is_closed():
                raise RuntimeError("DeepSeek 页面不存在或已关闭。")
            await self._try_new_conversation(self._page)
            await self._page.wait_for_timeout(1500)
            return await self.status()

    async def _close_unlocked(self) -> None:
        page, context, browser, pw = self._page, self._context, self._browser, self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        for obj in [page, context, browser]:
            if obj:
                try:
                    await obj.close()
                except Exception:
                    pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass

    async def _switch_session(self, page: Page, state: DeepSeekState, session_id: str | None, *, force_new: bool = False) -> None:
        if not session_id:
            return

        session_id = str(session_id).strip() or "default"
        known_url = self._session_urls.get(session_id)

        if force_new or not known_url:
            # Important: when a new session appears, create a new DeepSeek conversation
            # so Telegram / Web / different agents do not mix in one browser chat.
            await self._try_new_conversation(page)
            await page.wait_for_timeout(1500)
            self._active_session_id = session_id
            return

        if self._active_session_id == session_id and page.url == known_url:
            return

        try:
            await page.goto(known_url, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
            await page.wait_for_timeout(1500)
            self._active_session_id = session_id
        except Exception:
            # If DeepSeek changed URL or the old chat is gone, start a clean session.
            await self._try_new_conversation(page)
            await page.wait_for_timeout(1500)
            self._active_session_id = session_id

    async def _prepare_page_for_session(self, state: DeepSeekState, session_id: str | None, *, force_new: bool = False) -> Page:
        """
        V12: use an independent Playwright Page per logical sessionId.
        This prevents Telegram/Web/OpenClaw sessions from sharing one DeepSeek chat.
        """
        if not self._context:
            await self._start_unlocked(state)

        if not self._context:
            raise RuntimeError("浏览器上下文不存在。")

        if not session_id:
            if not self._page or self._page.is_closed():
                self._page = await self._context.new_page()
                self._page.set_default_timeout(settings.browser_timeout_ms)
                await self._page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
                await self._page.wait_for_timeout(1500)
            if force_new:
                await self._try_new_conversation(self._page)
                await self._page.wait_for_timeout(1200)
            return self._page

        key = str(session_id).strip() or "default"
        page = self._session_pages.get(key)
        if page and page.is_closed():
            self._session_pages.pop(key, None)
            page = None

        if page is None:
            page = await self._context.new_page()
            page.set_default_timeout(settings.browser_timeout_ms)
            known_url = self._session_urls.get(key)
            try:
                await page.goto(known_url or DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
            except Exception:
                await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
            await page.wait_for_timeout(1800)

            if force_new or not known_url:
                await self._try_new_conversation(page)
                await page.wait_for_timeout(700)

            self._session_pages[key] = page

        elif force_new:
            await self._try_new_conversation(page)
            await page.wait_for_timeout(700)

        self._page = page
        self._active_session_id = key
        return page

    def _remember_session_url(self, page: Page, session_id: str | None) -> None:
        if not session_id:
            return
        url = page.url or ""
        if "deepseek.com" in url:
            self._session_urls[str(session_id)] = url
            self._active_session_id = str(session_id)

    def _postprocess_answer(self, text: str, answer_format: str | None = None) -> str:
        """
        Clean text extracted from DeepSeek DOM.

        V11 fixes:
        - V9/V10 used a wrongly escaped replacement r"\\1" when stripping markdown bold,
          which could turn normal text into repeated literal "\\1".
        - Citation footnotes from the web UI are removed only when they are standalone lines.
        - Normal newlines, paragraphs and lists are preserved for Telegram/OpenClaw.
        """
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return text

        # Hard safety: if a previous bad regex already produced literal \1 spam,
        # do not return it as a model answer.
        if re.fullmatch(r"(?:\\1|\s|。|\.|,|，|、)+", text):
            return ""

        lines = text.split("\n")
        cleaned: list[str] = []
        skip_next_dot = False

        for raw in lines:
            line = raw.strip()

            # Remove standalone citation markers extracted from DeepSeek web footnotes:
            # 1 / 2 / 4 / [1] / ① / 1 2 4
            if re.fullmatch(r"(?:\[?\d{1,2}\]?|[①②③④⑤⑥⑦⑧⑨⑩]|\d{1,2}(?:\s+\d{1,2})+)", line):
                skip_next_dot = True
                continue

            # Sometimes citation markers leave an isolated punctuation line.
            if skip_next_dot and line in {"。", ".", "，", ",", "、"}:
                skip_next_dot = False
                continue

            skip_next_dot = False
            cleaned.append(raw.rstrip())

        text = "\n".join(cleaned).strip()

        # Strip markdown links but keep visible text: [text](url) -> text
        text = re.sub(r"\[([^\]]{1,200})\]\((?:https?://|/)[^)]+\)", r"\1", text)

        # Telegram-safe/plain mode: avoid markdown formatting surprises while preserving text.
        fmt = (answer_format or "").lower()
        if fmt in {"plain", "telegram", "telegram_safe"}:
            text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
            text = re.sub(r"__(.*?)__", r"\1", text)
            text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]{1,500})(?<!\*)\*(?!\*)", r"\1", text)

            looks_like_html_code = bool(re.search(r"</?(html|head|body|div|canvas|script|style|button|span|p|h1|h2)\b", text, flags=re.I))
            if fmt in {"telegram", "telegram_safe"} and looks_like_html_code:
                text = html.escape(text, quote=False)

        # Collapse excessive blank lines.
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        # Remove repeated consecutive paragraphs caused by nested DOM candidates.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        deduped: list[str] = []
        for p in paragraphs:
            if deduped and p == deduped[-1]:
                continue

            # If one paragraph fully contains the previous one, keep the longer version.
            if deduped and (p in deduped[-1] or deduped[-1] in p) and min(len(p), len(deduped[-1])) > 30:
                if len(p) > len(deduped[-1]):
                    deduped[-1] = p
                continue

            deduped.append(p)

        return "\n\n".join(deduped).strip()

    async def ask(
        self,
        state: DeepSeekState,
        prompt: str,
        *,
        new_conversation: bool = True,
        timeout_ms: int = 120_000,
        session_id: str | None = None,
        answer_format: str | None = None,
    ) -> AskResponse:
        rt = runtime_store.get()
        if len(prompt) > rt.maxPromptChars:
            prompt = prompt[:rt.maxPromptChars] + "\\n\\n[内容过长，已截断]"
        started = time.monotonic()
        last_error = None
        recovery_retries = rt.workerRecoveryRetries

        for attempt in range(recovery_retries + 1):
            async with self._lock:
                self._busy = True
                self._reserved = False
                self._last_ask_at = now_iso()
                try:
                    await self._start_unlocked(state)
                    page = await self._prepare_page_for_session(state, session_id, force_new=new_conversation)
                    if not page or page.is_closed():
                        raise RuntimeError("DeepSeek 页面不存在或已关闭。")
                    if "deepseek.com" not in page.url:
                        await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
                        await page.wait_for_timeout(2500)
                    if not await self._find_composer(page):
                        raise RuntimeError("没有找到 DeepSeek 输入框。")

                    before = await self._extract_answer_snapshot(page)
                    await self._fill_prompt(page, prompt)
                    await self._send_prompt(page)
                    answer = await self._wait_for_answer(page, before, prompt, timeout_ms=timeout_ms)

                    if rt.contextAutoResetEnabled and rt.contextFullRetryOnce and await self._answer_indicates_context_full(answer):
                        await self._try_new_conversation(page)
                        await page.wait_for_timeout(1500)
                        before = await self._extract_answer_snapshot(page)
                        await self._fill_prompt(page, prompt)
                        await self._send_prompt(page)
                        answer = await self._wait_for_answer(page, before, prompt, timeout_ms=timeout_ms)

                    raw_answer_before_postprocess = answer
                    answer = self._postprocess_answer(answer, answer_format)
                    if not answer and raw_answer_before_postprocess:
                        answer = str(raw_answer_before_postprocess).replace('\\1', '').strip() or '抱歉，回复内容提取异常，请重试。'
                    self._remember_session_url(page, session_id)

                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    self._last_error = None
                    self._consecutive_failures = 0
                    return AskResponse(ok=True, answer=answer, elapsedMs=elapsed_ms, message="已收到 DeepSeek 回复。", status=await self.status())
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    self._last_error = last_error
                    self._consecutive_failures += 1
                    if attempt < recovery_retries:
                        try:
                            await self._start_unlocked(state, force_restart=True)
                            self._restart_count += 1
                            self._last_recovered_at = now_iso()
                        except Exception:
                            pass
                    elif self._consecutive_failures >= rt.maxConsecutiveFailuresBeforeRestart:
                        try:
                            await self._start_unlocked(state, force_restart=True)
                            self._restart_count += 1
                            self._last_recovered_at = now_iso()
                        except Exception:
                            pass
                finally:
                    self._busy = False

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AskResponse(ok=False, answer=None, elapsedMs=elapsed_ms, message=f"发送失败：{last_error}", status=await self.status())

    async def _find_composer(self, page: Page):
        selectors = ['textarea[placeholder*="DeepSeek"]','textarea[placeholder*="发送"]','textarea[placeholder*="Send"]',"textarea",'[contenteditable="true"]']
        for selector in selectors:
            loc = page.locator(selector).last
            try:
                if await loc.count() > 0 and await loc.is_visible(timeout=1200):
                    return loc
            except Exception:
                continue
        return None

    async def _fill_prompt(self, page: Page, prompt: str) -> None:
        composer = await self._find_composer(page)
        if not composer:
            raise RuntimeError("没有找到输入框。")
        await composer.click()
        tag = await composer.evaluate("(el) => el.tagName.toLowerCase()")
        if tag in {"textarea", "input"}:
            await composer.fill(prompt)
        else:
            await composer.evaluate("""(el, text) => {
                el.focus(); el.textContent = text;
                el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
            }""", prompt)

    async def _send_prompt(self, page: Page) -> None:
        selectors = ['button[aria-label*="发送"]','button[aria-label*="Send"]','[role="button"][aria-label*="发送"]','[role="button"][aria-label*="Send"]','button:has-text("发送")','button:has-text("Send")']
        for selector in selectors:
            try:
                loc = page.locator(selector).last
                if await loc.count() > 0 and await loc.is_visible(timeout=700):
                    disabled = await loc.evaluate("(el) => Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')")
                    if not disabled:
                        await loc.click()
                        await page.wait_for_timeout(700)
                        return
            except Exception:
                continue
        composer = await self._find_composer(page)
        if not composer:
            raise RuntimeError("没有输入框。")
        await composer.press("Enter")
        await page.wait_for_timeout(700)

    async def _try_new_conversation(self, page: Page) -> None:
        selectors = ['span:has-text("开启新对话")','div:has-text("开启新对话")','button:has-text("开启新对话")','a:has-text("开启新对话")','span:has-text("New chat")','div:has-text("New chat")','button:has-text("New chat")','a:has-text("New chat")']
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible(timeout=700):
                    await loc.click()
                    return
            except Exception:
                continue
        try:
            await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
        except Exception:
            pass

    async def _extract_message_candidates(self, page: Page) -> list[dict]:
        return await page.evaluate(
            """
() => {
  const out = [];
  const seen = new Set();

  const orderMap = new WeakMap();
  let orderIndex = 0;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walker.nextNode())) {
    orderMap.set(node, orderIndex++);
  }

  function domOrder(el) {
    return orderMap.has(el) ? orderMap.get(el) : -1;
  }

  function textOf(el) {
    return (el && (el.innerText || el.textContent) || "").trim();
  }

  function cleanText(text) {
    return (text || "")
      .replace(/\\n{3,}/g, "\\n\\n")
      .replace(/[ \\t]+\\n/g, "\\n")
      .replace(/^(复制|重新生成|编辑|删除|分享|点赞|点踩|Copy|Regenerate|Edit|Delete)\\s*$/gm, "")
      .trim();
  }

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  }

  function isPageLevel(el) {
    const cls = (el.getAttribute("class") || "").toLowerCase();
    const id = (el.getAttribute("id") || "").toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === "body" || tag === "main") return true;
    const s = `${cls} ${id}`;
    return (
      s.includes("conversation-list") ||
      s.includes("chat-list") ||
      s.includes("scroll") ||
      s.includes("layout") ||
      s.includes("container") ||
      s.includes("app")
    );
  }

  function isUserLike(el) {
    const t = [
      el.getAttribute("data-message-author-role") || "",
      el.getAttribute("data-role") || "",
      el.getAttribute("class") || "",
      el.getAttribute("aria-label") || ""
    ].join(" ").toLowerCase();

    if (t.includes("user") || t.includes("human")) return true;

    const txt = cleanText(textOf(el));
    const r = el.getBoundingClientRect();

    // User bubbles in DeepSeek web are usually short right-side bubbles.
    if (txt.length < 260 && r.left > window.innerWidth * 0.45) return true;

    return false;
  }

  function add(selector, el, source) {
    if (!el || !visible(el) || isUserLike(el) || isPageLevel(el)) return;

    let text = cleanText(textOf(el));
    if (!text || text.length < 2) return;

    const key = `${domOrder(el)}:${text.slice(0, 300)}`;
    if (seen.has(key)) return;
    seen.add(key);

    const r = el.getBoundingClientRect();
    out.push({
      selector,
      source,
      text,
      length: text.length,
      order: domOrder(el),
      top: r.top,
      left: r.left,
      width: r.width,
      height: r.height
    });
  }

  // 1. Explicit assistant containers. Use these when available because they are most likely
  // to contain the full answer.
  const explicitSelectors = [
    "[data-message-author-role='assistant']",
    "[data-role='assistant']",
    "[data-testid*='assistant']",
    "[aria-label*='assistant' i]",
    "[class*='assistant' i]"
  ];

  for (const selector of explicitSelectors) {
    for (const el of Array.from(document.querySelectorAll(selector))) {
      add(selector, el, "explicit-assistant");
    }
  }

  // 2. Markdown/content blocks. Do NOT aggregate the whole page here.
  // V7 aggregation happens later in Python, using only nodes that appeared after the send snapshot.
  const contentSelectors = [
    ".ds-markdown",
    "[class*='ds-markdown']",
    "[class*='markdown']",
    "[class*='Markdown']",
    ".markdown-body",
    "article"
  ];

  for (const selector of contentSelectors) {
    for (const el of Array.from(document.querySelectorAll(selector))) {
      add(selector, el, "markdown-node");
    }
  }

  out.sort((a, b) => a.order - b.order);
  return out.slice(-80);
}
"""
        )

    async def _extract_answer_text(self, page: Page) -> str:
        candidates = await self._extract_message_candidates(page)
        if not candidates:
            return ""
        max_order = max(int(c.get("order") or 0) for c in candidates)
        near_latest = [c for c in candidates if max_order - int(c.get("order") or 0) < 20]
        chosen = max(near_latest or candidates, key=lambda c: int(c.get("length") or len(str(c.get("text") or ""))))
        return str(chosen.get("text") or "").strip()

    async def _extract_answer_snapshot(self, page: Page) -> dict:
        candidates = await self._extract_message_candidates(page)
        if not candidates:
            return {"maxOrder": -1, "text": ""}
        max_order = max(int(c.get("order") or 0) for c in candidates)
        text = await self._extract_answer_text(page)
        return {"maxOrder": max_order, "text": text}

    async def dom_debug(self, state: DeepSeekState | None = None) -> dict:
        if state is not None:
            async with self._lock:
                await self._start_unlocked(state)

        page = self._page
        if not page or page.is_closed():
            return {
                "ok": False,
                "message": "Worker 未启动或页面已关闭。",
                "status": (await self.status()).model_dump(),
            }

        try:
            candidates = await self._extract_message_candidates(page)
            snapshot = await self._extract_answer_snapshot(page)
            page_info = await page.evaluate(
                """
() => {
  const bodyText = document.body ? document.body.innerText : "";
  return {
    url: location.href,
    title: document.title || "",
    textareaCount: document.querySelectorAll("textarea").length,
    editableCount: document.querySelectorAll("[contenteditable='true']").length,
    buttonCount: document.querySelectorAll("button,[role='button']").length,
    bodySample: bodyText.slice(0, 1200)
  };
}
"""
            )
            composer = await self._debug_composer(page)
            send_buttons = await self._debug_send_buttons(page)
            return {
                "ok": True,
                "message": "DOM 调试快照已生成。",
                "status": (await self.status()).model_dump(),
                "page": page_info,
                "composer": composer,
                "sendButtons": send_buttons,
                "maxOrder": snapshot.get("maxOrder"),
                "selected": {
                    "text": snapshot.get("text", ""),
                    "length": len(snapshot.get("text", "") or ""),
                    "preview": (snapshot.get("text", "") or "")[:800],
                },
                "candidates": self._trim_candidates_for_debug(candidates),
            }
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return {
                "ok": False,
                "message": f"DOM 调试失败：{self._last_error}",
                "status": (await self.status()).model_dump(),
            }

    async def dom_probe(
        self,
        state: DeepSeekState,
        prompt: str,
        *,
        new_conversation: bool = False,
        timeout_ms: int = 120_000,
    ) -> dict:
        async with self._lock:
            self._busy = True
            self._reserved = False
            self._last_ask_at = now_iso()

            try:
                await self._start_unlocked(state)
                page = self._page
                if not page or page.is_closed():
                    raise RuntimeError("DeepSeek 页面不存在或已关闭。")

                if "deepseek.com" not in page.url:
                    await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
                    await page.wait_for_timeout(2500)

                if new_conversation:
                    await self._try_new_conversation(page)
                    await page.wait_for_timeout(1500)

                before_candidates = await self._extract_message_candidates(page)
                before_snapshot = await self._extract_answer_snapshot(page)

                await self._fill_prompt(page, prompt)
                await self._send_prompt(page)
                selected_answer = await self._wait_for_answer(page, before_snapshot, prompt, timeout_ms=timeout_ms)

                after_candidates = await self._extract_message_candidates(page)
                after_snapshot = await self._extract_answer_snapshot(page)

                before_order = int(before_snapshot.get("maxOrder", -1))
                new_candidates = [
                    c for c in after_candidates
                    if int(c.get("order") or -1) > before_order
                ]

                self._last_error = None
                self._consecutive_failures = 0

                return {
                    "ok": True,
                    "message": "DOM 探针完成。",
                    "status": (await self.status()).model_dump(),
                    "prompt": prompt,
                    "before": {
                        "maxOrder": before_snapshot.get("maxOrder"),
                        "selectedLength": len(before_snapshot.get("text", "") or ""),
                        "selectedPreview": (before_snapshot.get("text", "") or "")[:500],
                        "candidateCount": len(before_candidates),
                        "candidates": self._trim_candidates_for_debug(before_candidates),
                    },
                    "after": {
                        "maxOrder": after_snapshot.get("maxOrder"),
                        "selectedLength": len(after_snapshot.get("text", "") or ""),
                        "selectedPreview": (after_snapshot.get("text", "") or "")[:500],
                        "candidateCount": len(after_candidates),
                        "candidates": self._trim_candidates_for_debug(after_candidates),
                    },
                    "newCandidates": self._trim_candidates_for_debug(new_candidates),
                    "selectedAnswer": selected_answer,
                    "selectedAnswerLength": len(selected_answer or ""),
                    "selectionStrategy": {
                        "beforeMaxOrder": before_order,
                        "rule": "只聚合 order > beforeMaxOrder 的本次新增候选节点；优先 explicit-assistant，其次按 DOM order 聚合 markdown-node。",
                    },
                }

            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {
                    "ok": False,
                    "message": f"DOM 探针失败：{self._last_error}",
                    "status": (await self.status()).model_dump(),
                }
            finally:
                self._busy = False

    def _trim_candidates_for_debug(self, candidates: list[dict], limit: int = 80) -> list[dict]:
        out = []
        for c in candidates[-limit:]:
            text = str(c.get("text") or "")
            out.append({
                "selector": c.get("selector"),
                "source": c.get("source"),
                "order": c.get("order"),
                "length": c.get("length"),
                "top": c.get("top"),
                "left": c.get("left"),
                "width": c.get("width"),
                "height": c.get("height"),
                "preview": text[:1000],
            })
        return out

    async def _debug_composer(self, page: Page) -> dict:
        return await page.evaluate(
            """
() => {
  const selectors = [
    'textarea[placeholder*="DeepSeek"]',
    'textarea[placeholder*="发送"]',
    'textarea[placeholder*="Send"]',
    'textarea',
    '[contenteditable="true"]'
  ];

  const results = [];
  for (const selector of selectors) {
    const nodes = Array.from(document.querySelectorAll(selector));
    for (const el of nodes) {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      results.push({
        selector,
        tag: el.tagName.toLowerCase(),
        visible: r.width > 0 && r.height > 0 && style.display !== "none" && style.visibility !== "hidden",
        placeholder: el.getAttribute("placeholder") || "",
        ariaLabel: el.getAttribute("aria-label") || "",
        textPreview: (el.innerText || el.textContent || el.value || "").slice(0, 300),
        rect: { left: r.left, top: r.top, width: r.width, height: r.height }
      });
    }
  }

  return {
    found: results.length > 0,
    count: results.length,
    candidates: results.slice(-20)
  };
}
"""
        )

    async def _debug_send_buttons(self, page: Page) -> list[dict]:
        return await page.evaluate(
            """
() => {
  const buttons = Array.from(document.querySelectorAll("button,[role='button']"));
  return buttons.map((el, index) => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const text = [
      el.innerText || "",
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || ""
    ].join(" ").trim();
    return {
      index,
      tag: el.tagName.toLowerCase(),
      textPreview: text.slice(0, 200),
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
      visible: r.width > 0 && r.height > 0 && style.display !== "none" && style.visibility !== "hidden",
      rect: { left: r.left, top: r.top, width: r.width, height: r.height }
    };
  }).filter((x) => x.visible).slice(-60);
}
"""
        )

    async def _answer_indicates_context_full(self, text: str) -> bool:
        low = (text or "").lower()
        patterns = [
            "context length",
            "maximum context",
            "token limit",
            "too long",
            "conversation is too long",
            "上下文",
            "上下文长度",
            "长度限制",
            "对话过长",
            "超出限制",
            "达到上限",
            "重新开始",
        ]
        return any(p in low or p in text for p in patterns)

    async def _is_generating(self, page: Page) -> bool:
        """Best-effort DeepSeek generation detector."""
        try:
            return bool(await page.evaluate(
                """
() => {
  const nodes = Array.from(document.querySelectorAll("button,[role='button']"));
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!(r.width > 0 && r.height > 0) || style.display === "none" || style.visibility === "hidden") continue;
    const text = [
      el.innerText || "",
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || "",
      el.getAttribute("class") || ""
    ].join(" ").toLowerCase();

    if (
      text.includes("stop") ||
      text.includes("停止") ||
      text.includes("cancel") ||
      text.includes("中止") ||
      text.includes("pause")
    ) return true;
  }
  return false;
}
"""
            ))
        except Exception:
            return False

    async def _wait_for_answer(self, page: Page, before: dict, prompt: str, *, timeout_ms: int) -> str:
        deadline = time.monotonic() + timeout_ms / 1000
        last_text = ""
        last_change = time.monotonic()

        before_order = int((before or {}).get("maxOrder", -1)) if isinstance(before, dict) else -1
        before_text = str((before or {}).get("text", "")) if isinstance(before, dict) else str(before or "")
        prompt_text = prompt.strip()

        def normalize_piece(text: str) -> str:
            return (text or "").strip()

        def aggregate_new_candidates(candidates: list[dict]) -> str:
            new_items = []
            for c in candidates:
                text = normalize_piece(str(c.get("text") or ""))
                if not text:
                    continue
                if text == prompt_text or text == before_text:
                    continue

                order = int(c.get("order") or -1)
                if order <= before_order:
                    continue

                new_items.append(c)

            if not new_items:
                return ""

            # If a full assistant container exists after the snapshot, prefer the longest one.
            explicit = [c for c in new_items if c.get("source") == "explicit-assistant"]
            if explicit:
                chosen = max(explicit, key=lambda c: int(c.get("length") or len(str(c.get("text") or ""))))
                return normalize_piece(str(chosen.get("text") or ""))

            # Otherwise aggregate only the new markdown nodes created for this request.
            new_items.sort(key=lambda c: int(c.get("order") or 0))
            pieces: list[str] = []
            for c in new_items:
                text = normalize_piece(str(c.get("text") or ""))
                if not text:
                    continue

                # Avoid nested/duplicate fragments.
                duplicate = False
                for p in pieces:
                    if text == p or text in p:
                        duplicate = True
                        break
                if duplicate:
                    continue

                # If a later, larger piece contains an earlier small piece, replace the earlier one.
                pieces = [p for p in pieces if p not in text]
                pieces.append(text)

            return "\\n\\n".join(pieces).strip()

        while time.monotonic() < deadline:
            await page.wait_for_timeout(700)
            candidates = await self._extract_message_candidates(page)
            current = aggregate_new_candidates(candidates)

            if current and current != before_text and current != prompt_text:
                if current != last_text:
                    last_text = current
                    last_change = time.monotonic()
                elif time.monotonic() - last_change >= 1.8:
                    if not await self._is_generating(page):
                        return current
                    if time.monotonic() - last_change >= 5.0:
                        return current

        if last_text:
            return last_text

        raise TimeoutError("等待回复超时。")


def _looks_context_full_text(text: str) -> bool:
    low = (text or "").lower()
    patterns = [
        "context length",
        "maximum context",
        "token limit",
        "too long",
        "conversation is too long",
        "上下文",
        "长度限制",
        "对话过长",
        "超出限制",
        "达到上限",
        "重新开始",
    ]
    return any(p in low or p in text for p in patterns)
