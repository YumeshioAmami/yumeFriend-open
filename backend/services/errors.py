"""业务层错误：API 层统一映射成 api.md §0.1 的错误格式。"""


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NotFoundError(ServiceError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status=404)


class PermissionError_(ServiceError):
    def __init__(self, message: str = "没有权限执行该操作") -> None:
        super().__init__("PERMISSION_DENIED", message, status=403)
