from typing import Any
from urllib.parse import urlencode

from botocore.exceptions import ClientError


def client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, operation)


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    async def __aenter__(self) -> "FakeBody":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def read(self, amt: int | None = None) -> bytes:
        end = len(self.data) if amt is None else self.position + amt
        chunk = self.data[self.position : end]
        self.position += len(chunk)
        return chunk


class FakeS3:
    """只實作用到的 S3 API，存在記憶體裡。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        # upload id → 每一塊的內容
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.aborted: list[str] = []
        self.deleted: list[str] = []

    async def create_multipart_upload(self, *, Key: str, **_: Any) -> dict[str, str]:
        upload_id = f"{Key}#{len(self.uploads) + len(self.aborted) + 1}"
        self.uploads[upload_id] = {}
        return {"UploadId": upload_id}

    async def upload_part(
        self, *, UploadId: str, PartNumber: int, Body: bytes, **_: Any
    ) -> dict[str, str]:
        self.uploads[UploadId][PartNumber] = Body
        return {"ETag": f'"{PartNumber}"'}

    async def list_parts(
        self, *, UploadId: str, PartNumberMarker: int = 0, **_: Any
    ) -> dict[str, Any]:
        if UploadId not in self.uploads:
            raise client_error("NoSuchUpload", "ListParts")
        parts = [
            {"PartNumber": number, "Size": len(body), "ETag": f'"{number}"'}
            for number, body in sorted(self.uploads[UploadId].items())
            if number > PartNumberMarker
        ]
        return {"Parts": parts, "IsTruncated": False}

    async def complete_multipart_upload(
        self, *, Key: str, UploadId: str, MultipartUpload: dict[str, Any], **_: Any
    ) -> None:
        parts = self.uploads.pop(UploadId)
        self.objects[Key] = b"".join(parts[part["PartNumber"]] for part in MultipartUpload["Parts"])

    async def abort_multipart_upload(self, *, Key: str, UploadId: str, **_: Any) -> None:
        self.aborted.append(Key)
        self.uploads.pop(UploadId, None)

    async def head_object(self, *, Key: str, **_: Any) -> dict[str, int]:
        if Key not in self.objects:
            raise client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    async def get_object(self, *, Key: str, Range: str | None = None, **_: Any) -> dict[str, Any]:
        if Key not in self.objects:
            raise client_error("NoSuchKey", "GetObject")
        data = self.objects[Key]
        if Range is not None:
            start, end = Range.removeprefix("bytes=").split("-")
            data = data[int(start) : int(end) + 1]
        return {"Body": FakeBody(data)}

    async def put_object(self, *, Key: str, Body: bytes, **_: Any) -> None:
        self.objects[Key] = Body

    async def delete_object(self, *, Key: str, **_: Any) -> None:
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    async def generate_presigned_url(
        self, operation: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        # 把參數放進網址，測試可以檢查簽了什麼
        query = {k: v for k, v in Params.items() if k not in ("Bucket", "Key")}
        return f"https://s3.test/{Params['Key']}?{urlencode({'op': operation, **query})}"
