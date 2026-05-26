from urllib.parse import quote
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from botocore.exceptions import ClientError as S3ClientError
from config import settings
from services.downloader import get_object_info, download_object

router = APIRouter()

S3_ERROR_MAP = {
    "InvalidAccessKeyId": (40101, "S3 认证失败"),
    "AccessDenied": (40301, "S3 权限不足"),
    "NoSuchBucket": (40402, "Bucket 不存在"),
    "NoSuchKey": (40401, "对象不存在"),
}


def _map_s3_error(error: S3ClientError) -> tuple[int, str]:
    return S3_ERROR_MAP.get(
        error.response["Error"]["Code"],
        (50001, f"S3 操作失败: {error}")
    )


@router.get("/download")
def download(
    key: str,
    bucket: str = "",
    inline: bool = False,
    request: Request = None,
):
    bucket = (bucket or settings.s3_bucket).strip()
    if not key or not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    if not bucket:
        raise HTTPException(status_code=400, detail="bucket 未配置")

    range_header = request.headers.get("Range") if request else None

    try:
        result = download_object(bucket, key, range_header)
    except S3ClientError as e:
        code, msg = _map_s3_error(e)
        raise HTTPException(status_code=500, detail=msg)

    filename = key.rsplit("/", 1)[-1] if "/" in key else key
    disposition = "inline" if inline else "attachment"

    headers = {
        "Content-Disposition": f'{disposition}; filename="{quote(filename)}"',
        "Accept-Ranges": "bytes",
        "ETag": result["etag"],
        "Content-Type": result["content_type"],
    }

    status_code = 200
    if result["content_range"]:
        status_code = 206
        headers["Content-Range"] = result["content_range"]

    return StreamingResponse(
        result["body"].iter_chunks(),
        status_code=status_code,
        headers=headers,
        media_type=result["content_type"],
    )


@router.head("/download")
def download_head(
    key: str,
    bucket: str = "",
):
    bucket = (bucket or settings.s3_bucket).strip()
    if not key or not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    if not bucket:
        raise HTTPException(status_code=400, detail="bucket 未配置")

    try:
        info = get_object_info(bucket, key)
    except S3ClientError as e:
        code, msg = _map_s3_error(e)
        raise HTTPException(status_code=500, detail=msg)

    filename = key.rsplit("/", 1)[-1] if "/" in key else key
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(info["size"]),
            "Content-Type": info["content_type"],
            "ETag": info["etag"],
            "Last-Modified": info["last_modified"],
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
        },
    )
