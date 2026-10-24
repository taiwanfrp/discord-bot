class TicketError(Exception):
    """要讓使用者看到的錯誤（Discord 回覆訊息、API 回傳 status_code）。"""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
