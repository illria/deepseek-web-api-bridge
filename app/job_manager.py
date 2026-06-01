from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from app.account_store import AccountStore
from app.config import settings
from app.event_bus import events
from app.runtime_settings import runtime_store
from app.schemas import JobRecord, JobCreateRequest
from app.utils import now_iso
from app.worker_pool import pool


class JobManager:
    def __init__(self) -> None:
        self.store = AccountStore()
        self.jobs_dir = settings.jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, JobRecord] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.max_queue_size)
        self._dispatcher: asyncio.Task | None = None
        self._load_jobs()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _load_jobs(self) -> None:
        for p in sorted(self.jobs_dir.glob("*.json")):
            try:
                job = JobRecord.model_validate_json(p.read_text(encoding="utf-8"))
                if job.status in {"queued", "running"}:
                    job.status = "failed"
                    job.error = "服务重启后任务被标记失败。"
                    job.finishedAt = now_iso()
                self.jobs[job.jobId] = job
            except Exception:
                continue

    async def _save_job(self, job: JobRecord) -> None:
        self.jobs[job.jobId] = job
        self._job_path(job.jobId).write_text(job.model_dump_json(indent=2), encoding="utf-8")
        await events.broadcast("jobs.changed", {"jobId": job.jobId, "status": job.status})

    async def ensure_started(self) -> None:
        if self._dispatcher and not self._dispatcher.done():
            return
        self._dispatcher = asyncio.create_task(self._dispatch_loop())

    def list_jobs(self, page: int = 1, page_size: int = 20) -> tuple[list[JobRecord], int, int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        items = list(self.jobs.values())
        items.sort(key=lambda x: x.createdAt, reverse=True)
        total = len(items)
        pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        return items[start:start + page_size], total, pages

    def get_job(self, job_id: str) -> JobRecord:
        if job_id not in self.jobs:
            raise FileNotFoundError("任务不存在。")
        return self.jobs[job_id]

    async def create_job(self, req: JobCreateRequest) -> JobRecord:
        await self.ensure_started()
        rt = runtime_store.get()
        if self.queue.qsize() >= rt.maxQueueSize:
            raise RuntimeError("队列已满。")
        job_id = uuid.uuid4().hex
        job = JobRecord(
            jobId=job_id,
            sessionId=req.sessionId,
            answerFormat=req.answerFormat,
            status="queued",
            createdAt=now_iso(),
            message=req.message,
            system=req.system,
            accountId=req.accountId,
            timeoutMs=req.timeoutMs or rt.defaultAskTimeoutMs,
            newConversation=rt.newConversationPerRequest if req.newConversation is None else req.newConversation,
            queuePosition=self.queue.qsize() + 1,
            retryAttempts=rt.jobRetryAttempts if req.retryAttempts is None else req.retryAttempts,
            attempt=0,
        )
        await self._save_job(job)
        self.events[job_id] = asyncio.Event()
        await self.queue.put(job_id)
        return job

    async def wait_job(self, job_id: str, timeout: float | None = None) -> JobRecord:
        if job_id not in self.events:
            self.events[job_id] = asyncio.Event()
        try:
            if timeout is not None:
                await asyncio.wait_for(self.events[job_id].wait(), timeout=timeout)
            else:
                await self.events[job_id].wait()
        except asyncio.TimeoutError:
            pass
        return self.get_job(job_id)

    async def cancel_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job.status == "queued":
            job.status = "cancelled"
            job.finishedAt = now_iso()
            job.error = "用户取消。"
            await self._save_job(job)
            if job_id in self.events:
                self.events[job_id].set()
        return job

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                job_id = await self.queue.get()
                asyncio.create_task(self._process_job(job_id))
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    async def _process_job(self, job_id: str) -> None:
        account_id = None
        try:
            job = self.get_job(job_id)
            if job.status == "cancelled":
                return
            start_ts = time.monotonic()

            account_id = await pool.pick_account(job.accountId)
            job.accountId = account_id
            job.status = "running"
            job.startedAt = now_iso()
            job.queuePosition = None
            job.attempt += 1
            await self._save_job(job)

            state, _ = self.store.load_state(account_id)
            prompt = job.message if not job.system else f"系统要求：\n{job.system}\n\n用户问题：\n{job.message}"
            worker = pool.get_or_create(account_id)
            rt = runtime_store.get()
            hard_timeout = max((job.timeoutMs or rt.defaultAskTimeoutMs) / 1000 + 10, rt.workerHardTimeoutMs / 1000)

            try:
                result = await asyncio.wait_for(
                    worker.ask(
                        state,
                        prompt,
                        new_conversation=bool(job.newConversation),
                        timeout_ms=job.timeoutMs or rt.defaultAskTimeoutMs,
                        session_id=job.sessionId,
                        answer_format=job.answerFormat,
                    ),
                    timeout=hard_timeout,
                )
            except asyncio.TimeoutError:
                job.status = "timeout"
                job.error = "任务执行超时，已触发 Worker 重启。"
                job.finishedAt = now_iso()
                job.elapsedMs = int((time.monotonic() - start_ts) * 1000)
                await self._save_job(job)
                try:
                    await pool.restart_worker(account_id)
                except Exception:
                    pass
                await self._maybe_retry(job)
                return

            job.answer = result.answer
            job.elapsedMs = result.elapsedMs
            job.finishedAt = now_iso()
            if result.ok:
                job.status = "succeeded"
            else:
                job.status = "failed"
                job.error = result.message
                try:
                    await pool.restart_worker(account_id)
                except Exception:
                    pass
            await self._save_job(job)

            if job.status != "succeeded":
                await self._maybe_retry(job)

        except Exception as exc:
            try:
                job = self.get_job(job_id)
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finishedAt = now_iso()
                await self._save_job(job)
                await self._maybe_retry(job)
            except Exception:
                pass
        finally:
            pool.release_account(account_id)
            final = self.jobs.get(job_id)
            if final and final.status in {"succeeded", "failed", "timeout", "cancelled"}:
                if job_id in self.events:
                    self.events[job_id].set()

    async def _maybe_retry(self, job: JobRecord) -> None:
        if job.status in {"succeeded", "cancelled"}:
            return
        if job.attempt <= job.retryAttempts:
            job.status = "queued"
            job.error = f"自动重试中：第 {job.attempt}/{job.retryAttempts} 次失败"
            job.finishedAt = None
            job.startedAt = None
            job.queuePosition = self.queue.qsize() + 1
            await self._save_job(job)
            await self.queue.put(job.jobId)


job_manager = JobManager()
