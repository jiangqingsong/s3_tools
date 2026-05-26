import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import settings
from services.s3_client import get_s3_client
from services.checkpoint import Checkpoint, save_checkpoint, delete_checkpoint
from services.task_manager import task_manager, TaskStatus


def upload_file(
    file_path: str,
    original_filename: str,
    key: str,
    bucket: str,
    file_size: int,
    content_type: str | None = None,
) -> dict:
    if file_size > settings.max_upload_size:
        raise FileTooLargeError(file_size, settings.max_upload_size)

    if file_size < settings.multipart_threshold:
        return _upload_small(file_path, key, bucket, content_type)
    else:
        return _prepare_large_upload(file_path, original_filename, key, bucket, file_size, content_type)


def _upload_small(file_path: str, key: str, bucket: str, content_type: str | None) -> dict:
    s3 = get_s3_client()
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    with open(file_path, "rb") as f:
        resp = s3.put_object(Bucket=bucket, Key=key, Body=f, **extra_args)
    file_size = os.path.getsize(file_path)
    return {
        "status": "completed",
        "key": key,
        "bucket": bucket,
        "size": file_size,
        "etag": resp.get("ETag", ""),
    }


def _prepare_large_upload(
    file_path: str,
    original_filename: str,
    key: str,
    bucket: str,
    file_size: int,
    content_type: str | None,
) -> dict:
    task_id = os.path.basename(file_path).split("-", 1)[0]
    part_size = settings.part_size
    total_parts = (file_size + part_size - 1) // part_size
    return {
        "status": "processing",
        "task_id": task_id,
        "key": key,
        "bucket": bucket,
        "total_size": file_size,
        "part_size": part_size,
        "total_parts": total_parts,
    }


def execute_multipart_upload(task_id: str, key: str, bucket: str,
                             file_path: str, file_size: int,
                             part_size: int, total_parts: int,
                             content_type: str | None = None):
    """后台执行分片上传。由 API 层在 BackgroundTasks 中调用。"""
    s3 = get_s3_client()
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type

    try:
        # 1. Initiate multipart upload
        resp = s3.create_multipart_upload(Bucket=bucket, Key=key, **extra_args)
        upload_id = resp["UploadId"]

        # 2. Create checkpoint & task
        cp = Checkpoint(
            task_id=task_id, file_path=file_path, file_size=file_size,
            bucket=bucket, key=key, upload_id=upload_id,
            part_size=part_size, total_parts=total_parts,
        )
        save_checkpoint(cp)
        task_manager.create(
            task_id=task_id, key=key, bucket=bucket,
            file_path=file_path, total_size=file_size,
            part_size=part_size, total_parts=total_parts,
            upload_id=upload_id,
        )

        # 3. Upload parts concurrently
        _upload_all_parts(s3, cp, upload_id, bucket, key, file_path,
                          file_size, part_size, total_parts)

        # 4. Complete
        task_manager.update(task_id, status=TaskStatus.COMPLETING)
        parts_sorted = sorted(cp.completed_parts, key=lambda p: p["part_number"])
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": [
                {"PartNumber": p["part_number"], "ETag": p["etag"]}
                for p in parts_sorted
            ]},
        )

        # 5. Cleanup
        delete_checkpoint(task_id)
        _cleanup_temp_file(file_path)
        task_manager.update(task_id, status=TaskStatus.COMPLETED)

    except Exception as e:
        task_manager.update(task_id, status=TaskStatus.FAILED, error_message=str(e))


def resume_multipart_upload(task_id: str, content_type: str | None = None):
    """从 checkpoint 恢复未完成的分片上传。"""
    cp = task_manager.get(task_id)
    if cp is None:
        return
    # 从 task_manager 拿到 upload_id 和 file_path 重新执行
    task = task_manager.get(task_id)
    if task is None or not task.upload_id:
        return

    # 重新加载 checkpoint
    from services.checkpoint import load_checkpoint
    cp = load_checkpoint(task_id)
    if cp is None:
        return

    s3 = get_s3_client()
    try:
        _upload_all_parts(s3, cp, task.upload_id, task.bucket, task.key,
                          task.file_path, task.total_size,
                          task.part_size, task.total_parts)

        task_manager.update(task_id, status=TaskStatus.COMPLETING)
        parts_sorted = sorted(cp.completed_parts, key=lambda p: p["part_number"])
        s3.complete_multipart_upload(
            Bucket=task.bucket, Key=task.key, UploadId=task.upload_id,
            MultipartUpload={"Parts": [
                {"PartNumber": p["part_number"], "ETag": p["etag"]}
                for p in parts_sorted
            ]},
        )
        delete_checkpoint(task_id)
        _cleanup_temp_file(task.file_path)
        task_manager.update(task_id, status=TaskStatus.COMPLETED)
    except Exception as e:
        task_manager.update(task_id, status=TaskStatus.FAILED, error_message=str(e))


def _upload_all_parts(s3, cp: Checkpoint, upload_id: str, bucket: str,
                       key: str, file_path: str, file_size: int,
                       part_size: int, total_parts: int):
    """上传所有未完成的分片（跳过 checkpoint 中已完成的）。"""
    completed_set = {p["part_number"] for p in cp.completed_parts}

    with ThreadPoolExecutor(max_workers=settings.upload_concurrency) as executor:
        futures = {}
        for part_num in range(1, total_parts + 1):
            if part_num in completed_set:
                continue
            start = (part_num - 1) * part_size
            end = min(start + part_size, file_size)
            fut = executor.submit(
                _upload_single_part, s3, file_path, bucket, key,
                upload_id, part_num, start, end
            )
            futures[fut] = part_num

        for fut in as_completed(futures):
            part_num = futures[fut]
            try:
                etag = fut.result()
                actual_size = min(part_size, file_size - (part_num - 1) * part_size)
                cp.add_part(part_num, etag, actual_size)
                save_checkpoint(cp)
                task_manager.add_part(cp.task_id, part_num, etag, actual_size)
            except Exception:
                raise


def _upload_single_part(s3, file_path: str, bucket: str, key: str,
                         upload_id: str, part_number: int,
                         start: int, end: int) -> str:
    """上传单个分片，返回 ETag。"""
    with open(file_path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    resp = s3.upload_part(
        Bucket=bucket, Key=key, UploadId=upload_id,
        PartNumber=part_number, Body=data,
    )
    return resp["ETag"]


def _cleanup_temp_file(file_path: str):
    if os.path.exists(file_path):
        os.remove(file_path)


class FileTooLargeError(Exception):
    def __init__(self, file_size: int, max_size: int):
        self.file_size = file_size
        self.max_size = max_size
        super().__init__(f"文件大小 {file_size} 超出限制 {max_size}")
