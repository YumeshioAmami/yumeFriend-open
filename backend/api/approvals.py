"""工具审批（v2.1 / T2.1-4 / D99）：前端点「允许 / 拒绝」打到这里。"""

from fastapi import APIRouter

from backend.services import approval_service

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.post("/{request_id}")
def resolve_approval(request_id: str, body: dict) -> dict:
    allow = bool(body.get("allow"))
    found = approval_service.resolve(request_id, allow=allow)
    return {"ok": found, "allow": allow}
