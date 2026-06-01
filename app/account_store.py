from __future__ import annotations

import json
from pathlib import Path
from app.config import settings
from app.schemas import DeepSeekState, AccountSummary, AccountUpdateRequest
from app.security import encrypt_bytes, decrypt_bytes
from app.utils import now_iso, safe_id


class AccountStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or settings.accounts_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _dir(self, account_id: str) -> Path:
        return self.base_dir / safe_id(account_id)

    def _state_path(self, account_id: str) -> Path:
        return self._dir(account_id) / "state.enc"

    def _meta_path(self, account_id: str) -> Path:
        return self._dir(account_id) / "meta.json"

    def exists(self, account_id: str) -> bool:
        return self._state_path(account_id).exists()

    def save(
        self,
        account_id: str,
        display_name: str,
        notes: str | None,
        state: DeepSeekState,
        *,
        enabled: bool = True,
        priority: int = 100,
        weight: int = 100,
    ) -> AccountSummary:
        account_id = safe_id(account_id)
        folder = self._dir(account_id)
        folder.mkdir(parents=True, exist_ok=True)

        created_at = now_iso()
        if self._meta_path(account_id).exists():
            try:
                old = json.loads(self._meta_path(account_id).read_text(encoding="utf-8"))
                created_at = old.get("createdAt") or created_at
            except Exception:
                pass

        raw_payload = {"savedAt": now_iso(), "state": state.model_dump(mode="json")}
        raw = json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        enc, encrypted = encrypt_bytes(raw)
        self._state_path(account_id).write_bytes(enc)

        summary = AccountSummary(
            accountId=account_id,
            displayName=display_name,
            createdAt=created_at,
            updatedAt=now_iso(),
            capturedAt=state.capturedAt,
            pageUrl=state.pageUrl,
            schemaVersion=state.schemaVersion,
            cookieCount=len(state.cookies),
            encrypted=encrypted,
            notes=notes,
            enabled=enabled,
            priority=priority,
            weight=weight,
        )
        self._meta_path(account_id).write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    def update(self, account_id: str, req: AccountUpdateRequest) -> AccountSummary:
        meta = self.load_meta(account_id)
        data = meta.model_dump()
        for key in ["displayName", "notes", "enabled", "priority", "weight"]:
            value = getattr(req, key, None)
            if value is not None:
                data[key] = value
        data["updatedAt"] = now_iso()
        updated = AccountSummary.model_validate(data)
        self._meta_path(account_id).write_text(updated.model_dump_json(indent=2), encoding="utf-8")
        return updated

    def load_state(self, account_id: str) -> tuple[DeepSeekState, bool]:
        path = self._state_path(account_id)
        if not path.exists():
            raise FileNotFoundError("账号登录态不存在。")
        raw = path.read_bytes()
        plain, encrypted = decrypt_bytes(raw)
        payload = json.loads(plain.decode("utf-8"))
        state = DeepSeekState.model_validate(payload["state"])
        return state, encrypted

    def load_meta(self, account_id: str) -> AccountSummary:
        path = self._meta_path(account_id)
        if not path.exists():
            raise FileNotFoundError("账号不存在。")
        return AccountSummary.model_validate_json(path.read_text(encoding="utf-8"))

    def list_accounts(self) -> list[AccountSummary]:
        items: list[AccountSummary] = []
        for folder in sorted(self.base_dir.glob("*")):
            if folder.is_dir():
                meta = folder / "meta.json"
                if meta.exists():
                    try:
                        items.append(AccountSummary.model_validate_json(meta.read_text(encoding="utf-8")))
                    except Exception:
                        continue
        items.sort(key=lambda x: (x.enabled, x.priority, x.weight, x.updatedAt), reverse=True)
        return items

    def delete(self, account_id: str) -> None:
        folder = self._dir(account_id)
        if folder.exists():
            for p in folder.rglob("*"):
                if p.is_file():
                    p.unlink()
            for p in sorted(folder.rglob("*"), reverse=True):
                if p.is_dir():
                    p.rmdir()
            folder.rmdir()
