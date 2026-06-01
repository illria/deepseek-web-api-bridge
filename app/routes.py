from __future__ import annotations
import asyncio
import traceback
import time, uuid
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import JSONResponse
from app.account_store import AccountStore
from app.auth import require_bridge_auth
from app.bridge_service import ask_bridge, messages_to_prompt, agent_messages_to_prompt, build_request_behavior_instruction, infer_direct_tool_call, infer_skill_tool_call, detect_skill_request, parse_tool_calls_from_answer, fast_finalize_tool_results, should_fast_finalize_tool_results, tool_names, openai_completion_payload, openai_stream_response, openai_live_stream_response
from app.data_service import build_dataset_meta, build_text_to_sql_prompt, execute_select_query, list_metas, load_meta, parse_model_sql_answer, save_upload
from app.deepseek_browser import check_deepseek_login_state
from app.event_bus import events
from app.job_manager import job_manager
from app.runtime_settings import runtime_store
from app.schemas import *
from app.worker_pool import pool

router = APIRouter()
store = AccountStore()
LAST_OPENAI_REQUESTS: list[dict] = []


def remember_openai_debug(item: dict) -> None:
    LAST_OPENAI_REQUESTS.append(item)
    del LAST_OPENAI_REQUESTS[:-20]


def validate_state(state):
    """
    Validate imported DeepSeek login state before saving.

    V15 restores this function after V14 accidentally removed it.
    The check is intentionally tolerant:
    - pageUrl may exist directly or inside page.pageUrl.
    - cookies are the real requirement.
    - if pageUrl is missing, deepseek.com cookie domains are enough.
    """
    page_url = state.pageUrl or state.page.get("pageUrl") or state.page.get("url") or ""
    cookie_domains = [getattr(c, "domain", "") or "" for c in (state.cookies or [])]
    has_deepseek_cookie = any("deepseek.com" in d for d in cookie_domains)

    if "deepseek.com" not in page_url and not has_deepseek_cookie:
        raise HTTPException(
            status_code=400,
            detail="这份 JSON 看起来不是 DeepSeek 登录态：pageUrl 不包含 deepseek.com，cookies 里也没有 deepseek.com 域名。",
        )

    if not state.cookies:
        raise HTTPException(status_code=400, detail="这份 JSON 没有 cookies，无法恢复登录态。")

    if len(state.cookies) < 2:
        raise HTTPException(status_code=400, detail="cookies 数量过少，可能不是完整登录态。")


def _safe_session_id(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    import re
    value = re.sub(r"[^a-zA-Z0-9_.:@\\-]+", "-", value)
    return value[:120] or None


def infer_openai_session_id(
    req: OpenAIChatCompletionRequest, AnthropicMessageRequest, OpenAIMessage,
    x_bridge_session_id: str | None = None,
    x_channel_id: str | None = None,
    x_conversation_id: str | None = None,
) -> str:
    sid = _safe_session_id(x_bridge_session_id)
    if sid:
        return sid

    channel = _safe_session_id(x_channel_id)
    conv = _safe_session_id(x_conversation_id)
    if channel and conv:
        return f"{channel}:{conv}"
    if conv:
        return conv

    sid = _safe_session_id(getattr(req, "sessionId", None))
    if sid:
        return sid

    sid = _safe_session_id(getattr(req, "user", None))
    if sid:
        return sid

    meta = getattr(req, "metadata", None) or {}
    if isinstance(meta, dict):
        for key in ["session_id", "sessionId", "conversation_id", "conversationId", "chat_id", "chatId", "user_id", "userId"]:
            sid = _safe_session_id(meta.get(key))
            if sid:
                return sid

    import re
    joined = "\n".join([str(getattr(m, "content", "") or "") for m in getattr(req, "messages", [])])

    patterns = [
        r'["\\\']id["\\\']\s*:\s*["\\\']([^"\\\']+)["\\\']',
        r'["\\\']user_id["\\\']\s*:\s*["\\\']([^"\\\']+)["\\\']',
        r'["\\\']chat_id["\\\']\s*:\s*["\\\']([^"\\\']+)["\\\']',
        r'["\\\']username["\\\']\s*:\s*["\\\']([^"\\\']+)["\\\']',
    ]
    for pat in patterns:
        m = re.search(pat, joined)
        if m:
            sid = _safe_session_id(m.group(1))
            if sid:
                return f"sender:{sid}"

    m = re.search(r'Sender \(untrusted metadata\):.*?["\\\']name["\\\']\s*:\s*["\\\']([^"\\\']+)["\\\']', joined, flags=re.S)
    if m:
        sid = _safe_session_id(m.group(1))
        if sid:
            return f"sender-name:{sid}"

    return "openai-default"


@router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await events.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        events.disconnect(websocket)
    except Exception:
        events.disconnect(websocket)

@router.get("/api/health")
async def health():
    workers = await pool.fleet_status()
    jobs, total, pages = job_manager.list_jobs(page=1, page_size=100)
    return {
        "ok": True,
        "service": "deepseek-web-api-bridge-v21",
        "apiAuthEnabled": bool(__import__("app.config").config.settings.api_key_set),
        "accounts": len(store.list_accounts()),
        "workersRunning": sum(1 for w in workers if w.running),
        "workersBusy": sum(1 for w in workers if w.busy),
        "queuedJobs": sum(1 for j in jobs if j.status == "queued"),
        "runningJobs": sum(1 for j in jobs if j.status == "running"),
        "totalJobs": total,
    }

@router.get("/api/settings", response_model=RuntimeSettings)
async def get_settings():
    return runtime_store.get()

@router.put("/api/settings", response_model=RuntimeSettings)
async def put_settings(req: RuntimeSettings):
    value = runtime_store.save(req)
    await events.broadcast("settings.changed", value.model_dump())
    return value

async def _start_worker_background(account_id: str) -> None:
    try:
        await pool.start_worker(account_id)
        await events.broadcast("workers.changed", {"accountId": account_id, "source": "import_background_start"})
    except Exception as exc:
        await events.broadcast("workers.changed", {"accountId": account_id, "source": "import_background_start_failed", "error": str(exc)})


@router.post("/api/accounts/import", response_model=AccountImportResponse)
async def import_account(req: AccountImportRequest):
    """
    V14: account import must only persist the state and return.
    Do not start Playwright here. Any error is returned as JSON instead of raw 500.
    """
    try:
        validate_state(req.state)

        acc = store.save(
            req.accountId,
            req.displayName,
            req.notes,
            req.state,
            enabled=req.enabled,
            priority=req.priority,
            weight=req.weight,
        )

        await events.broadcast("accounts.changed", {"accountId": acc.accountId})
        return AccountImportResponse(ok=True, account=acc)

    except HTTPException:
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        try:
            await events.broadcast("accounts.import_failed", {"error": detail})
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=detail) from exc

@router.get("/api/accounts", response_model=AccountListResponse)
async def list_accounts():
    return AccountListResponse(ok=True, accounts=store.list_accounts())

@router.get("/api/accounts/{account_id}")
async def get_account(account_id: str):
    try:
        meta = store.load_meta(account_id)
        worker = await pool.status(account_id)
        return {"ok": True, "account": meta, "worker": worker}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.put("/api/accounts/{account_id}", response_model=AccountImportResponse)
async def update_account(account_id: str, req: AccountUpdateRequest):
    try:
        acc = store.update(account_id, req)
        await events.broadcast("accounts.changed", {"accountId": acc.accountId})
        return AccountImportResponse(ok=True, account=acc)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str):
    try: await pool.stop_worker(account_id)
    except Exception: pass
    store.delete(account_id)
    await events.broadcast("accounts.changed", {"accountId": account_id})
    return {"ok": True}

@router.post("/api/accounts/{account_id}/check", response_model=CheckStateResponse)
async def check_account(account_id: str):
    state, _ = store.load_state(account_id)
    return await check_deepseek_login_state(state)

@router.post("/api/accounts/{account_id}/worker/start", response_model=WorkerStatusResponse)
async def start_worker(account_id: str):
    st = await pool.start_worker(account_id)
    await events.broadcast("workers.changed", {"accountId": account_id})
    return st

@router.post("/api/accounts/{account_id}/worker/stop", response_model=WorkerStatusResponse)
async def stop_worker(account_id: str):
    st = await pool.stop_worker(account_id)
    await events.broadcast("workers.changed", {"accountId": account_id})
    return st

@router.get("/api/accounts/{account_id}/worker/status", response_model=WorkerStatusResponse)
async def worker_status(account_id: str):
    return await pool.status(account_id)

@router.post("/api/accounts/{account_id}/worker/reset", response_model=WorkerStatusResponse)
async def reset_worker_conversation(account_id: str):
    st = await pool.reset_conversation(account_id)
    await events.broadcast("workers.changed", {"accountId": account_id, "action": "reset_conversation"})
    return st

@router.get("/api/workers/status", response_model=WorkerFleetResponse)
async def fleet_status():
    return WorkerFleetResponse(ok=True, workers=await pool.fleet_status())

@router.get("/api/accounts/{account_id}/worker/dom-debug")
async def worker_dom_debug(account_id: str):
    try:
        return await pool.dom_debug(account_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/api/accounts/{account_id}/worker/dom-probe")
async def worker_dom_probe(account_id: str, req: DomProbeRequest):
    try:
        return await pool.dom_probe(
            account_id,
            req.prompt,
            new_conversation=req.newConversation,
            timeout_ms=req.timeoutMs,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/api/jobs", response_model=JobCreateResponse)
async def create_job(req: JobCreateRequest):
    if req.accountId:
        store.load_meta(req.accountId)
    job = await job_manager.create_job(req)
    return JobCreateResponse(ok=True, job=job)

@router.get("/api/jobs", response_model=JobListResponse)
async def list_jobs(page: int = 1, pageSize: int = 20):
    items, total, pages = job_manager.list_jobs(page=page, page_size=pageSize)
    return JobListResponse(ok=True, jobs=items, total=total, page=max(1,page), pageSize=min(max(1,pageSize),100), pages=pages)

@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return {"ok": True, "job": job_manager.get_job(job_id)}

@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = await job_manager.cancel_job(job_id)
    return {"ok": True, "job": job}

@router.post("/api/bridge/chat", response_model=BridgeChatResponse, dependencies=[Depends(require_bridge_auth)])
async def bridge_chat(req: BridgeChatRequest):
    final = await ask_bridge(req.message, system=req.system, account_id=req.accountId, session_id=req.sessionId, answer_format=req.answerFormat, new_conversation=req.newConversation, timeout_ms=req.timeoutMs)
    status = await pool.status(final.accountId) if final.accountId else None
    return BridgeChatResponse(ok=final.status=="succeeded", answer=final.answer, message=final.error or ("已收到 DeepSeek 回复。" if final.status=="succeeded" else f"任务状态：{final.status}"), elapsedMs=final.elapsedMs or 0, accountId=final.accountId, jobId=final.jobId, status=status)

@router.get("/api/debug/last-openai-request")
async def debug_last_openai_request():
    return {"ok": True, "items": LAST_OPENAI_REQUESTS[-20:]}

@router.get("/v1/models", dependencies=[Depends(require_bridge_auth)])
async def openai_models():
    return {"object":"list","data":[{"id":"deepseek-web","object":"model","created":int(time.time()),"owned_by":"deepseek-web-api-bridge-v21"}]}

@router.post("/v1/chat/completions", dependencies=[Depends(require_bridge_auth)])
async def openai_chat_completions(
    req: OpenAIChatCompletionRequest,
    x_bridge_session_id: str | None = Header(default=None),
    x_channel_id: str | None = Header(default=None),
    x_conversation_id: str | None = Header(default=None),
    x_answer_format: str | None = Header(default=None),
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages is required.")

    session_id = infer_openai_session_id(req, x_bridge_session_id, x_channel_id, x_conversation_id)
    tools = req.tools or []
    has_tools = bool(tools)
    has_tool_msg = any(m.role == "tool" for m in req.messages)
    mode = "agent_tools" if (has_tools or has_tool_msg) else (runtime_store.get().openaiPromptMode or "latest_user")

    if has_tools or has_tool_msg or str(mode).lower() == "agent_tools":
        prompt = agent_messages_to_prompt(req.messages, tools)
    else:
        prompt = messages_to_prompt(req.messages)

    behavior_instruction = build_request_behavior_instruction(
        response_format=req.response_format,
        web_search_options=req.web_search_options,
        reasoning_effort=req.reasoning_effort,
    )
    if behavior_instruction:
        prompt = behavior_instruction + "\n\n" + prompt

    direct_tool_calls = []
    skill_info = detect_skill_request(req.messages)
    rt = runtime_store.get()
    tool_result_fast_final = bool(has_tool_msg and getattr(rt, "agentToolResultMode", "fast_final") == "fast_final")

    if has_tools and not has_tool_msg and (req.tool_choice in (None, "auto") or isinstance(req.tool_choice, dict)):
        direct_tool_calls = infer_direct_tool_call(req.messages, tools)
        if not direct_tool_calls:
            direct_tool_calls, skill_info = infer_skill_tool_call(req.messages, tools)

    debug_item = {
        "ts": int(time.time()),
        "sessionId": session_id,
        "model": req.model,
        "stream": req.stream,
        "mode": mode,
        "tools": tool_names(tools),
        "hasToolMessage": has_tool_msg,
        "skillDetected": skill_info,
        "directToolCall": bool(direct_tool_calls),
        "toolResultFastFinal": tool_result_fast_final,
        "toolCallPreview": direct_tool_calls[:2],
        "promptPreview": prompt[:1200],
    }
    remember_openai_debug(debug_item)

    async def run_agent_once() -> dict:
        if direct_tool_calls:
            return {"answer": None, "tool_calls": direct_tool_calls}

        if tool_result_fast_final and should_fast_finalize_tool_results(req.messages):
            answer = fast_finalize_tool_results(
                req.messages,
                max_chars=getattr(runtime_store.get(), "agentToolResultMaxChars", 6000),
            )
            if answer:
                return {"answer": answer, "tool_calls": []}

        if skill_info and not tools:
            skill_name = skill_info.get("skill")
            return {
                "answer": (
                    f"检测到你想运行 OpenClaw Skill：{skill_name}，但这次 provider 请求没有携带 tools。"
                    "Skill 本身是说明书，不是可直接执行的函数；请在 OpenClaw/Hermes provider 配置中启用工具传递，"
                    "或者使用 OpenClaw 的 slash/command-dispatch 路径。"
                ),
                "tool_calls": [],
            }

        final = await ask_bridge(
            prompt,
            session_id=session_id,
            answer_format=x_answer_format or "telegram_safe",
            new_conversation=runtime_store.get().newConversationPerRequest,
            timeout_ms=runtime_store.get().defaultAskTimeoutMs,
        )

        if final.status != "succeeded" or final.answer is None:
            if final.status in {"running", "queued"}:
                raise RuntimeError("DeepSeek 网页任务仍在运行或未完成；请提高 OpenClaw/Hermes provider timeout，或稍后重试。")
            raise RuntimeError(final.error or f"任务状态：{final.status}")

        parsed_tool_calls = []
        if has_tools and not has_tool_msg:
            parsed_tool_calls = parse_tool_calls_from_answer(final.answer or "", tools)

        return {"answer": final.answer or "", "tool_calls": parsed_tool_calls}

    cid = f"chatcmpl-{uuid.uuid4().hex}"

    if req.stream:
        return openai_live_stream_response(model=req.model, run=run_agent_once, completion_id=cid)

    try:
        result = await run_agent_once()
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"{type(exc).__name__}: {exc}", "type": "deepseek_web_bridge_error", "code": "bridge_error"}},
        )

    tool_calls = result.get("tool_calls") or []
    answer = result.get("answer") or ""

    return openai_completion_payload(
        model=req.model,
        answer=None if tool_calls else answer,
        prompt=prompt,
        completion_id=cid,
        tool_calls=tool_calls or None,
        finish_reason="tool_calls" if tool_calls else "stop",
    )

def _anthropic_content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                ty = block.get("type")
                if ty == "text":
                    parts.append(str(block.get("text") or ""))
                elif ty == "tool_result":
                    parts.append(str(block.get("content") or ""))
                elif ty in {"image", "document"}:
                    parts.append(f"[{ty} content omitted by browser bridge]")
                else:
                    parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _anthropic_to_openai(req: AnthropicMessageRequest) -> OpenAIChatCompletionRequest:
    messages = []
    if req.system:
        messages.append(OpenAIMessage(role="system", content=_anthropic_content_to_text(req.system)))
    for msg in req.messages:
        role = "assistant" if msg.role == "assistant" else "user"
        messages.append(OpenAIMessage(role=role, content=_anthropic_content_to_text(msg.content)))

    tools = None
    if req.tools:
        tools = []
        for t in req.tools:
            tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            })

    return OpenAIChatCompletionRequest(
        model=req.model or "deepseek-web",
        messages=messages,
        tools=tools,
        tool_choice=req.tool_choice,
        max_tokens=req.max_tokens,
        stream=False,
        metadata=req.metadata,
    )


@router.get("/anthropic/v1/models", dependencies=[Depends(require_bridge_auth)])
async def anthropic_models():
    return {
        "data": [
            {"id": "deepseek-web", "type": "model", "display_name": "DeepSeek Web"},
            {"id": "deepseek-default", "type": "model", "display_name": "DeepSeek Default"},
            {"id": "deepseek-expert", "type": "model", "display_name": "DeepSeek Expert"},
            {"id": "deepseek-vision", "type": "model", "display_name": "DeepSeek Vision"},
        ],
        "has_more": False,
    }


@router.post("/anthropic/v1/messages", dependencies=[Depends(require_bridge_auth)])
async def anthropic_messages(
    req: AnthropicMessageRequest,
    x_bridge_session_id: str | None = Header(default=None),
    x_channel_id: str | None = Header(default=None),
    x_conversation_id: str | None = Header(default=None),
):
    oai_req = _anthropic_to_openai(req)
    result = await openai_chat_completions(
        oai_req,
        x_bridge_session_id=x_bridge_session_id,
        x_channel_id=x_channel_id,
        x_conversation_id=x_conversation_id,
        x_answer_format="telegram_safe",
    )
    if isinstance(result, JSONResponse):
        return result
    choice = result.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content") or ""
    if msg.get("tool_calls"):
        blocks = []
        for call in msg["tool_calls"]:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"input": fn.get("arguments") or ""}
            blocks.append({"type": "tool_use", "id": call.get("id"), "name": fn.get("name"), "input": args})
        stop_reason = "tool_use"
    else:
        blocks = [{"type": "text", "text": content}]
        stop_reason = "end_turn"

    return {
        "id": result.get("id", "msg_bridge"),
        "type": "message",
        "role": "assistant",
        "model": req.model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": result.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": result.get("usage", {}).get("completion_tokens", 0),
        },
    }


@router.post("/api/data/upload", response_model=DatasetUploadResponse)
async def upload_dataset(file: UploadFile = File(...)):
    stored = await save_upload(file)
    meta = build_dataset_meta(stored, file.filename or stored.name)
    await events.broadcast("data.changed", {"datasetId": meta.datasetId})
    return DatasetUploadResponse(ok=True, dataset=meta)

@router.get("/api/data/datasets", response_model=DatasetListResponse)
async def datasets():
    return DatasetListResponse(ok=True, datasets=list_metas())

@router.get("/api/data/datasets/{dataset_id}", response_model=DatasetMeta)
async def dataset_detail(dataset_id: str):
    return load_meta(dataset_id)

@router.post("/api/data/query", response_model=DataQueryResponse)
async def data_query(req: DataQueryRequest):
    meta = load_meta(req.datasetId)
    prompt = build_text_to_sql_prompt(meta, req.question, req.limit)
    final = await ask_bridge(prompt, account_id=req.accountId, new_conversation=True, timeout_ms=120000)
    if final.status != "succeeded" or not final.answer:
        return DataQueryResponse(ok=False, rawModelAnswer=final.answer, jobId=final.jobId, accountId=final.accountId, message=f"DeepSeek 生成 SQL 失败：{final.error or final.status}")
    try:
        sql, explain = parse_model_sql_answer(final.answer)
        columns, rows = execute_select_query(meta, sql, req.limit)
        return DataQueryResponse(ok=True, sql=sql, explain=explain, rows=rows, columns=columns, rowCount=len(rows), rawModelAnswer=final.answer, jobId=final.jobId, accountId=final.accountId, message="查询成功。")
    except Exception as exc:
        return DataQueryResponse(ok=False, rawModelAnswer=final.answer, jobId=final.jobId, accountId=final.accountId, message=f"SQL 解析或执行失败：{type(exc).__name__}: {exc}")
