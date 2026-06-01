from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi.responses import StreamingResponse
from app.job_manager import job_manager
from app.runtime_settings import runtime_store
from app.schemas import OpenAIMessage, JobCreateRequest

DS_TOOL_CALL_START = "<｜tool▁call▁begin｜>"
DS_TOOL_CALL_END = "<｜tool▁call▁end｜>"


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
                else:
                    continue
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _collect_system(messages: list[OpenAIMessage]) -> str:
    parts: list[str] = []
    for msg in messages:
        if msg.role in {"system", "developer"}:
            text = normalize_content(msg.content).strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _last_user_content(messages: list[OpenAIMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            text = normalize_content(msg.content).strip()
            if text:
                return text
    for msg in reversed(messages):
        text = normalize_content(msg.content).strip()
        if text:
            return text
    return ""


def unwrap_current_user_message(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text

    text = re.sub(
        r"^\s*Sender \(untrusted metadata\):\s*```(?:json)?\s*.*?```\s*",
        "",
        text,
        flags=re.S | re.I,
    ).strip()

    if "Conversation context" in text:
        m = re.search(
            r"Conversation context.*?(?:\n#\d+[^\n]*)+\s*\n+(.+?)\s*$",
            text,
            flags=re.S | re.I,
        )
        if m and m.group(1).strip():
            return m.group(1).strip()

    for marker in ["Current message:", "Current user message:", "User message:", "用户消息：", "当前消息："]:
        if marker in text:
            tail = text.rsplit(marker, 1)[-1].strip()
            if tail:
                return tail

    return text


def _compact_history(messages: list[OpenAIMessage], turns: int) -> str:
    usable: list[OpenAIMessage] = [
        m for m in messages
        if m.role in {"user", "assistant"} and normalize_content(m.content).strip()
    ]
    if turns > 0:
        usable = usable[-turns * 2:]

    lines: list[str] = []
    for msg in usable:
        role = "用户" if msg.role == "user" else "助手"
        lines.append(f"{role}: {normalize_content(msg.content).strip()}")

    return "\n".join(lines)


def tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = fn.get("name") or tool.get("name")
        if name:
            names.append(str(name))
    return names


def _tool_function(tool: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    fn = tool.get("function")
    if isinstance(fn, dict):
        return fn
    return tool


def build_deepseek_native_tool_instruction(tools: list[dict[str, Any]] | None) -> str:
    names = tool_names(tools)
    return (
        "工具调用格式必须严格遵守：\n\n"
        f"{DS_TOOL_CALL_START}[{{\"name\":\"工具名\",\"arguments\":{{参数JSON}}}}]{DS_TOOL_CALL_END}\n\n"
        "规则：\n"
        "1. 决定调用工具时，第一个非空字符必须是工具调用开始标签。\n"
        "2. JSON 必须是数组，多个工具调用放在同一个数组里。\n"
        "3. 输出工具调用结束标签后必须立即停止，不要解释、不要 Markdown。\n"
        "4. 不要把工具调用放进代码块或思考内容里。\n"
        "5. 只使用这些工具名：" + (", ".join(names) if names else "无") + "\n"
    )


def _tool_specs_for_prompt(tools: list[dict[str, Any]] | None) -> str:
    slim: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = _tool_function(tool)
        name = fn.get("name")
        if not name:
            continue
        slim.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return json.dumps(slim, ensure_ascii=False, indent=2)


def has_tool_result(messages: list[OpenAIMessage]) -> bool:
    return any(m.role == "tool" for m in messages)


def _format_tool_results(messages: list[OpenAIMessage]) -> str:
    blocks: list[str] = []
    for msg in messages:
        if msg.role == "tool":
            content = normalize_content(msg.content).strip()
            if content:
                blocks.append(
                    f"tool_call_id: {msg.tool_call_id or ''}\n"
                    f"name: {msg.name or ''}\n"
                    f"result:\n{content}"
                )
    return "\n\n---\n\n".join(blocks)


def build_request_behavior_instruction(response_format: Any | None = None, web_search_options: Any | None = None, reasoning_effort: str | None = None) -> str:
    parts = []
    if isinstance(response_format, dict):
        ty = response_format.get("type")
        if ty == "json_object":
            parts.append("请直接输出合法 JSON 对象，不要包含 Markdown 代码块或解释文字。")
        elif ty == "json_schema":
            schema = response_format.get("json_schema")
            if schema:
                parts.append("请按以下 JSON Schema 输出合法 JSON：\n" + json.dumps(schema, ensure_ascii=False))
    if isinstance(web_search_options, dict) and web_search_options.get("search_context_size") == "none":
        parts.append("不要主动要求联网搜索；只能基于已有上下文回答，除非外部 Agent 明确提供 web_search 工具结果。")
    if reasoning_effort == "none":
        parts.append("请直接回答，减少推理展开。")
    return "\n".join(parts).strip()


def messages_to_prompt(messages: list[OpenAIMessage]) -> str:
    rt = runtime_store.get()
    mode = (rt.openaiPromptMode or "latest_user").strip().lower()
    system_text = _collect_system(messages)
    latest_user = unwrap_current_user_message(_last_user_content(messages))

    if not latest_user:
        return "你好"

    if mode == "full_transcript":
        history = _compact_history(messages, turns=10_000)
        prompt = ""
        if system_text:
            prompt += f"系统要求：\n{system_text}\n\n"
        prompt += (
            "下面是对话记录。请直接扮演助手自然回复最后一个用户消息，"
            "不要总结对话，不要说“根据对话内容”，不要复述这段记录。\n\n"
            f"{history}"
        )
    elif mode == "compact_history":
        history = _compact_history(messages, turns=max(1, rt.historyWindowTurns))
        prompt = ""
        if system_text:
            prompt += f"系统要求：\n{system_text}\n\n"
        prompt += (
            "请自然继续下面的对话，只回复用户最新问题，不要总结对话，不要复述记录。\n\n"
            f"{history}"
        )
    else:
        if system_text:
            prompt = f"请遵循以下要求回答用户消息。\n\n系统要求：\n{system_text}\n\n用户消息：\n{latest_user}"
        else:
            prompt = latest_user

    if len(prompt) > rt.maxPromptChars:
        prompt = prompt[-rt.maxPromptChars:]
    return prompt


def agent_messages_to_prompt(messages: list[OpenAIMessage], tools: list[dict[str, Any]] | None = None) -> str:
    rt = runtime_store.get()
    system_text = _collect_system(messages)
    latest_user = unwrap_current_user_message(_last_user_content(messages))
    tools_json = _tool_specs_for_prompt(tools)
    tool_results = _format_tool_results(messages)

    if tool_results:
        prompt = (
            "你正在作为 Agent 的推理模型。下面是工具执行结果，请基于结果直接回答用户，"
            "不要再要求用户自己执行命令。\n\n"
        )
        if system_text:
            prompt += f"系统要求：\n{system_text}\n\n"
        prompt += f"用户原始问题：\n{latest_user or '见上下文'}\n\n"
        prompt += f"工具结果：\n{tool_results}\n\n"
        prompt += "请给出简洁、准确、面向用户的最终回答。"
    elif tools:
        prompt = (
            "你正在作为 Agent 的工具规划模型。你可以选择调用工具，也可以直接回答。\n\n"
            + build_deepseek_native_tool_instruction(tools)
            + "\n如果不需要工具，则直接自然语言回答。\n\n"
            f"可用工具：\n{tools_json}\n\n"
        )
        if system_text:
            prompt += f"系统要求：\n{system_text}\n\n"
        prompt += f"用户消息：\n{latest_user or '你好'}"
    else:
        prompt = messages_to_prompt(messages)

    if len(prompt) > rt.maxPromptChars:
        prompt = prompt[-rt.maxPromptChars:]
    return prompt


def _make_tool_arguments(tool: dict[str, Any], preferred: dict[str, Any]) -> dict[str, Any]:
    fn = _tool_function(tool)
    params = fn.get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else {}
    props = props if isinstance(props, dict) else {}

    if not props:
        return preferred

    out: dict[str, Any] = {}
    for key in props:
        lk = key.lower()
        if lk in preferred:
            out[key] = preferred[lk]
        elif key in preferred:
            out[key] = preferred[key]

    command = preferred.get("command") or preferred.get("cmd") or preferred.get("input")
    if command:
        for name in ["command", "cmd", "input", "script", "code"]:
            if name in props and name not in out:
                out[name] = command
                break

    if not out and command:
        for key, spec in props.items():
            if isinstance(spec, dict) and spec.get("type") in {None, "string"}:
                out[key] = command
                break

    return out or preferred


def _openai_tool_call(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> dict[str, Any]:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call_id or f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def infer_direct_tool_call(messages: list[OpenAIMessage], tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not tools:
        return []

    latest_user = unwrap_current_user_message(_last_user_content(messages)).lower()
    if not latest_user:
        return []

    command_keywords = [
        "系统当前有什么任务", "当前有什么任务", "后台任务", "cron", "crontab",
        "systemctl", "进程", "后台进程", "list tasks", "running tasks",
        "scheduled tasks", "timers", "processes",
    ]
    if not any(k.lower() in latest_user for k in command_keywords):
        return []

    command_tool_names = [
        "terminal", "shell", "bash", "execute_command", "run_command", "command",
        "process", "exec", "run_shell", "local_shell",
    ]

    selected_tool = None
    selected_name = None
    for tool in tools:
        fn = _tool_function(tool)
        name = str(fn.get("name") or "")
        lname = name.lower()
        if any(key in lname for key in command_tool_names):
            selected_tool = tool
            selected_name = name
            break

    if not selected_tool or not selected_name:
        return []

    cmd = (
        "echo '--- crontab ---'; "
        "crontab -l 2>/dev/null || true; "
        "echo '--- systemd timers ---'; "
        "systemctl list-timers --all --no-pager 2>/dev/null || true; "
        "echo '--- user processes ---'; "
        "ps aux --sort=-%mem | head -30"
    )
    args = _make_tool_arguments(selected_tool, {"command": cmd, "cmd": cmd, "input": cmd})
    return [_openai_tool_call(selected_name, args)]


def _balanced_json_array_after(text: str, start_idx: int) -> str | None:
    i = text.find("[", start_idx)
    if i == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[i:j + 1]
    return None


def _extract_json_candidates(text: str) -> list[Any]:
    text = (text or "").strip()
    candidates: list[str] = []

    tag_pairs = [
        (DS_TOOL_CALL_START, DS_TOOL_CALL_END),
        ("<|tool_call_begin|>", "<|tool_call_end|>"),
        ("<tool_calls>", "</tool_calls>"),
        ("<tool_call>", "</tool_call>"),
    ]
    for start_tag, end_tag in tag_pairs:
        pos = 0
        while True:
            s = text.find(start_tag, pos)
            if s == -1:
                break
            e = text.find(end_tag, s + len(start_tag))
            if e == -1:
                arr = _balanced_json_array_after(text, s)
                if arr:
                    candidates.append(arr)
                break
            body = text[s + len(start_tag):e].strip()
            candidates.append(body)
            pos = e + len(end_tag)

    for m in re.finditer(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.S | re.I):
        candidates.append(m.group(1))

    for open_ch, close_ch in [("{", "}"), ("[", "]")]:
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start:end + 1])

    out: list[Any] = []
    for cand in candidates:
        cand = cand.strip()
        try:
            out.append(json.loads(cand))
        except Exception:
            if cand.startswith("{") and cand.endswith("}"):
                try:
                    out.append([json.loads(cand)])
                except Exception:
                    pass
    return out


def parse_tool_calls_from_answer(answer: str, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    valid_names = set(tool_names(tools))
    if not valid_names:
        return []

    parsed_candidates = _extract_json_candidates(answer)
    for obj in parsed_candidates:
        calls_data = None

        if isinstance(obj, list):
            calls_data = obj
        elif isinstance(obj, dict):
            if isinstance(obj.get("tool_calls"), list):
                calls_data = obj["tool_calls"]
            elif isinstance(obj.get("tool_call"), dict):
                calls_data = [obj["tool_call"]]
            elif obj.get("name") and (obj.get("arguments") is not None or obj.get("args") is not None):
                calls_data = [obj]

        if not calls_data:
            continue

        calls: list[dict[str, Any]] = []
        for item in calls_data:
            if not isinstance(item, dict):
                continue

            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            name = fn.get("name")
            args = fn.get("arguments", fn.get("args", {}))
            if not name:
                continue

            name_norm = str(name).replace("｜", "|").replace("▁", "_")
            match_name = None
            for valid in valid_names:
                if valid == name or valid.replace("_", "-") == name_norm.replace("_", "-") or valid.lower() == name_norm.lower():
                    match_name = valid
                    break
            if not match_name:
                continue

            if isinstance(args, str):
                try:
                    args_obj = json.loads(args)
                except Exception:
                    args_obj = {"input": args}
            elif isinstance(args, dict):
                args_obj = args
            else:
                args_obj = {"input": args}

            calls.append(_openai_tool_call(str(match_name), args_obj, call_id=item.get("id")))

        if calls:
            return calls

    return []

async def ask_bridge(
    message: str,
    *,
    system: str | None = None,
    account_id: str | None = None,
    session_id: str | None = None,
    answer_format: str | None = None,
    new_conversation: bool | None = None,
    timeout_ms: int | None = None,
):
    rt = runtime_store.get()
    job = await job_manager.create_job(
        JobCreateRequest(
            message=message,
            system=system,
            accountId=account_id,
            sessionId=session_id,
            answerFormat=answer_format,
            newConversation=new_conversation,
            timeoutMs=timeout_ms or rt.defaultAskTimeoutMs,
        )
    )
    wait_seconds = max((timeout_ms or rt.defaultAskTimeoutMs) / 1000 + 90, 180)
    final = await job_manager.wait_job(job.jobId, timeout=wait_seconds)
    if getattr(final, "answer", None):
        return final
    return final


def approximate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def openai_completion_payload(
    *,
    model: str,
    answer: str | None,
    prompt: str,
    completion_id: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
) -> dict:
    created = int(time.time())
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex}"
    answer_text = answer or ""
    prompt_tokens = approximate_tokens(prompt)
    completion_tokens = approximate_tokens(answer_text)

    message: dict[str, Any] = {"role": "assistant", "content": answer_text}
    if tool_calls:
        message["content"] = None
        message["tool_calls"] = tool_calls

    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def openai_stream_response(*, model: str, answer: str, completion_id: str | None = None) -> StreamingResponse:
    created = int(time.time())
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex}"

    async def gen() -> AsyncIterator[bytes]:
        first = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode()
        for i in range(0, len(answer), 800):
            chunk_text = answer[i:i + 800]
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": chunk_text}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(0)
        final = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def openai_live_stream_response(
    *,
    model: str,
    run: Callable[[], Awaitable[dict[str, Any]]],
    completion_id: str | None = None,
    heartbeat_seconds: float = 3.0,
) -> StreamingResponse:
    created = int(time.time())
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex}"

    async def gen() -> AsyncIterator[bytes]:
        first = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode()

        task = asyncio.create_task(run())

        while not task.done():
            # SSE comment keepalive plus an OpenAI-compatible empty delta.
            yield b": keepalive\n\n"
            keepalive_chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(keepalive_chunk, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(heartbeat_seconds)

        try:
            result = await task
        except Exception as exc:
            error_text = f"Bridge error: {type(exc).__name__}: {exc}"
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": error_text}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            final = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            return

        tool_calls = result.get("tool_calls") or []
        answer = result.get("answer") or ""

        if tool_calls:
            delta_calls = []
            for index, call in enumerate(tool_calls):
                fn = call.get("function") or {}
                delta_calls.append({
                    "index": index,
                    "id": call.get("id"),
                    "type": "function",
                    "function": {
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments", ""),
                    },
                })
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": delta_calls}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            finish = "tool_calls"
        else:
            for i in range(0, len(answer), 800):
                chunk_text = answer[i:i + 800]
                chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": chunk_text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                await asyncio.sleep(0)
            finish = "stop"

        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
