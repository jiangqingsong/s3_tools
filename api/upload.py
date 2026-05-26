import os
import threading
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
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
    """在新线程中执行分片上传，避免阻塞 FastAPI 事件循环。"""
    t = threading.Thread(
        target=execute_multipart_upload,
        args=(task_id, key, bucket, file_path, file_size,
              part_size, total_parts, content_type),
        daemon=True,
    )
    t.start()


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
        # 大文件：启动后台线程执行分片上传
        _start_multipart_in_thread(
            task_id=result["task_id"],
            key=key,
            bucket=bucket,
            file_path=os.path.join(settings.upload_temp_dir,
                                   f"{result['task_id']}-{file.filename or 'unknown'}"),
            file_size=result["total_size"],
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
