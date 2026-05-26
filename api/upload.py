from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from botocore.exceptions import ClientError as S3ClientError
from config import settings
from services.uploader import upload_file, FileTooLargeError
from schemas.responses import ApiResponse

router = APIRouter()

S3_ERROR_MAP = {
    "InvalidAccessKeyId": (40101, "S3 认证失败，Access Key 无效"),
    "SignatureDoesNotMatch": (40101, "S3 认证失败，Secret Key 错误"),
    "AccessDenied": (40301, "S3 权限不足"),
    "NoSuchBucket": (40402, "Bucket 不存在"),
    "NoSuchKey": (40401, "对象不存在"),
}


def _map_s3_error(error: S3ClientError) -> ApiResponse:
    code, msg = S3_ERROR_MAP.get(
        error.response["Error"]["Code"],
        (50001, f"S3 操作失败: {error}")
    )
    return ApiResponse(code=code, message=msg)


@router.post("/upload", response_model=ApiResponse)
async def upload(
    file: UploadFile = File(...),
    key: str = Form(...),
    bucket: str = Form(default=""),
    content_type: str = Form(default="", alias="content_type"),
    async_mode: bool = Form(default=False),
):
    if not key or not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    key = key.strip()
    if key.endswith("/"):
        filename = file.filename or "unknown"
        key = key + filename
    bucket = (bucket or settings.s3_bucket).strip()
    if not bucket:
        raise HTTPException(status_code=400, detail="bucket 未配置")

    content = await file.read()

    try:
        result = upload_file(content, file.filename or "unknown", key, bucket, content_type or None)
    except FileTooLargeError as e:
        return ApiResponse(code=40002, message=str(e))
    except S3ClientError as e:
        return _map_s3_error(e)
    except Exception as e:
        return ApiResponse(code=50001, message=str(e))

    if result["status"] == "completed":
        return ApiResponse(data=result)
    else:
        return ApiResponse(code=0, message="accepted", data=result)
