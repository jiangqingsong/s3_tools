import threading
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from services.checkpoint import Checkpoint, load_checkpoint


class TaskStatus:
    PROCESSING = "processing"
    UPLOADING = "uploading"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class UploadTask:
    task_id: str
    key: str
    bucket: str
    file_path: str
    total_size: int
    part_size: int
    total_parts: int
    status: str = TaskStatus.PROCESSING
    completed_parts: int = 0
    uploaded_bytes: int = 0
    error_message: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    upload_id: str = ""         # S3 multipart upload_id
    parts: list = field(default_factory=list)  # [(part_number, etag), ...]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "key": self.key,
            "progress": self.completed_parts / self.total_parts if self.total_parts > 0 else 0,
            "completed_parts": self.completed_parts,
            "total_parts": self.total_parts,
            "uploaded_bytes": self.uploaded_bytes,
            "total_bytes": self.total_size,
            "started_at": self.started_at,
        }

    def to_dict_with_error(self) -> dict:
        d = self.to_dict()
        d["error_message"] = self.error_message
        return d


class TaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, UploadTask] = {}

    def create(self, task_id: str, key: str, bucket: str, file_path: str,
               total_size: int, part_size: int, total_parts: int,
               upload_id: str = "") -> UploadTask:
        task = UploadTask(
            task_id=task_id, key=key, bucket=bucket,
            file_path=file_path, total_size=total_size,
            part_size=part_size, total_parts=total_parts,
            upload_id=upload_id,
        )
        with self._lock:
            self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> UploadTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                for k, v in kwargs.items():
                    setattr(task, k, v)

    def add_part(self, task_id: str, part_number: int, etag: str, part_size: int):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.parts.append((part_number, etag))
                task.completed_parts = len(task.parts)
                task.uploaded_bytes += part_size
                if task.status == TaskStatus.PROCESSING:
                    task.status = TaskStatus.UPLOADING

    def remove(self, task_id: str):
        with self._lock:
            self._tasks.pop(task_id, None)

    def list_all(self) -> list[UploadTask]:
        with self._lock:
            return list(self._tasks.values())

    def recover_from_checkpoint(self, cp: Checkpoint, upload_id: str):
        """从 checkpoint 恢复一个任务。"""
        task = UploadTask(
            task_id=cp.task_id,
            key=cp.key,
            bucket=cp.bucket,
            file_path=cp.file_path,
            total_size=cp.file_size,
            part_size=cp.part_size,
            total_parts=cp.total_parts,
            upload_id=upload_id,
            status=TaskStatus.PROCESSING,
        )
        for part in cp.completed_parts:
            task.parts.append((part["part_number"], part["etag"]))
            task.completed_parts = len(task.parts)
            task.uploaded_bytes += part.get("size", cp.part_size)
        if task.completed_parts > 0:
            task.status = TaskStatus.UPLOADING
        with self._lock:
            self._tasks[cp.task_id] = task
        return task


task_manager = TaskManager()
