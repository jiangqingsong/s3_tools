import os
import uuid
from config import settings
from services.s3_client import get_s3_client


def upload_file(
    file_content: bytes,
    original_filename: str,
    key: str,
    bucket: str,
    content_type: str | None = None,
) -> dict:
    """上传文件到 S3。

    小文件（< multipart_threshold）走 put_object 同步上传。
    大文件返回 is_async=True，由调用方决定是否走异步分片流程。
    """
    file_size = len(file_content)

    if file_size > settings.max_upload_size:
        raise FileTooLargeError(file_size, settings.max_upload_size)

    if file_size < settings.multipart_threshold:
        return _upload_small(file_content, key, bucket, content_type)
    else:
        return _prepare_large_upload(file_content, original_filename, key, bucket, content_type)


def _upload_small(file_content: bytes, key: str, bucket: str, content_type: str | None) -> dict:
    s3 = get_s3_client()
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    resp = s3.put_object(Bucket=bucket, Key=key, Body=file_content, **extra_args)
    return {
        "status": "completed",
        "key": key,
        "bucket": bucket,
        "size": len(file_content),
        "etag": resp.get("ETag", ""),
    }


def _prepare_large_upload(
    file_content: bytes,
    original_filename: str,
    key: str,
    bucket: str,
    content_type: str | None,
) -> dict:
    """将大文件保存到临时目录，返回信息供异步分片上传使用。"""
    os.makedirs(settings.upload_temp_dir, exist_ok=True)
    task_id = uuid.uuid4().hex
    file_path = os.path.join(settings.upload_temp_dir, f"{task_id}-{original_filename}")
    with open(file_path, "wb") as f:
        f.write(file_content)
    part_size = settings.part_size
    total_parts = (len(file_content) + part_size - 1) // part_size
    return {
        "status": "processing",
        "task_id": task_id,
        "key": key,
        "bucket": bucket,
        "total_size": len(file_content),
        "part_size": part_size,
        "total_parts": total_parts,
    }


class FileTooLargeError(Exception):
    def __init__(self, file_size: int, max_size: int):
        self.file_size = file_size
        self.max_size = max_size
        super().__init__(f"文件大小 {file_size} 超出限制 {max_size}")
