import os
import uuid
import threading
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from botocore.exceptions import ClientError as S3ClientError
from config import settings
from services.uploader import upload_file, execute_multipart_upload, FileTooLargeError
from services.task_manager import task_manager, TaskStatus
from services.s3_client import get_s3_client
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


def _start_multipart_in_thread(task_id: str, key: str, bucket: str,
                               file_path: str, file_size: int,
                               part_size: int, total_parts: int,
                               content_type: str | None):
    t = threading.Thread(
        target=execute_multipart_upload,
        args=(task_id, key, bucket, file_path, file_size,
              part_size, total_parts, content_type),
        daemon=True,
    )
    t.start()


async def _save_to_temp(file: UploadFile) -> tuple[str, str, int]:
    """流式写入临时文件，返回 (file_path, original_filename, file_size)。"""
    os.makedirs(settings.upload_temp_dir, exist_ok=True)
    task_id = uuid.uuid4().hex
    filename = file.filename or "unknown"
    file_path = os.path.join(settings.upload_temp_dir, f"{task_id}-{filename}")
    file_size = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(81920):
            f.write(chunk)
            file_size += len(chunk)
    return file_path, filename, file_size


def _remove_temp(file_path: str):
    if os.path.exists(file_path):
        os.remove(file_path)


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

    # 流式写入临时文件（不占内存）
    file_path, original_filename, file_size = await _save_to_temp(file)

    try:
        result = upload_file(file_path, original_filename, key, bucket, file_size, content_type or None)
    except FileTooLargeError as e:
        _remove_temp(file_path)
        return ApiResponse(code=40002, message=str(e))
    except S3ClientError as e:
        _remove_temp(file_path)
        return _map_s3_error(e)
    except Exception as e:
        _remove_temp(file_path)
        return ApiResponse(code=50001, message=str(e))

    if result["status"] == "completed":
        _remove_temp(file_path)
        return ApiResponse(data=result)
    else:
        # 大文件：后台线程接管 file_path 的生命周期
        _start_multipart_in_thread(
            task_id=result["task_id"],
            key=key,
            bucket=bucket,
            file_path=file_path,
            file_size=file_size,
            part_size=result["part_size"],
            total_parts=result["total_parts"],
            content_type=content_type or None,
        )
        return ApiResponse(code=0, message="accepted", data=result)


@router.get("/upload/status/{task_id}", response_model=ApiResponse)
def upload_status(task_id: str):
    task = task_manager.get(task_id)
    if task is None:
        return ApiResponse(code=40403, message=f"上传任务不存在: {task_id}")
    if task.status == TaskStatus.FAILED:
        return ApiResponse(data=task.to_dict_with_error())
    return ApiResponse(data=task.to_dict())


@router.post("/upload/cancel/{task_id}", response_model=ApiResponse)
def upload_cancel(task_id: str):
    task = task_manager.get(task_id)
    if task is None:
        return ApiResponse(code=40403, message=f"上传任务不存在: {task_id}")
    if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        return ApiResponse(code=40901, message=f"任务已结束: {task.status}")

    if task.upload_id:
        try:
            s3 = get_s3_client()
            s3.abort_multipart_upload(
                Bucket=task.bucket, Key=task.key, UploadId=task.upload_id,
            )
        except Exception:
            pass

    # 清理本地资源
    from services.checkpoint import delete_checkpoint
    delete_checkpoint(task_id)
    if task.file_path and os.path.exists(task.file_path):
        os.remove(task.file_path)

    task_manager.update(task_id, status=TaskStatus.CANCELLED)
    return ApiResponse(code=0, message="cancelled", data={"task_id": task_id, "status": "cancelled"})
