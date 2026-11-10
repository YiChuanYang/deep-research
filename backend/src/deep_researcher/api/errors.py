"""可以安全返回给 HTTP 客户端的错误。"""

from __future__ import annotations


class ApiError(Exception):
    """携带 HTTP 状态码和业务错误码。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        error_id: str | None = None,
    ) -> None:
        super().__init__(message)

        self.status_code = status_code
        self.code = code
        self.message = message
        self.run_id = run_id
        self.error_id = error_id