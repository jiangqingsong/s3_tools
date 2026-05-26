import json
import os
from datetime import datetime, timezone


class Checkpoint:
    def __init__(self, task_id: str, file_path: str, file_size: int,
                 bucket: str, key: str, upload_id: str,
                 part_size: int, total_parts: int):
        self.task_id = task_id
        self.file_path = file_path
        self.file_size = file_size
        self.bucket = bucket
        self.key = key
        self.upload_id = upload_id
        self.part_size = part_size
        self.total_parts = total_parts
        self.completed_parts: list[dict] = []
        self.next_part_number = 1
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at

    def add_part(self, part_number: int, etag: str, size: int):
        self.completed_parts.append({"part_number": part_number, "etag": etag, "size": size})
        self.next_part_number = part_number + 1
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def is_complete(self) -> bool:
        return len(self.completed_parts) >= self.total_parts

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "bucket": self.bucket,
            "key": self.key,
            "upload_id": self.upload_id,
            "part_size": self.part_size,
            "total_parts": self.total_parts,
            "completed_parts": self.completed_parts,
            "next_part_number": self.next_part_number,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        cp = cls(
            task_id=data["task_id"],
            file_path=data["file_path"],
            file_size=data["file_size"],
            bucket=data["bucket"],
            key=data["key"],
            upload_id=data["upload_id"],
            part_size=data["part_size"],
            total_parts=data["total_parts"],
        )
        cp.completed_parts = data.get("completed_parts", [])
        cp.next_part_number = data.get("next_part_number", 1)
        cp.created_at = data.get("created_at", "")
        cp.updated_at = data.get("updated_at", "")
        return cp


def _checkpoint_dir() -> str:
    from config import settings
    d = os.path.join(settings.upload_temp_dir, "checkpoints")
    os.makedirs(d, exist_ok=True)
    return d


def _checkpoint_path(task_id: str) -> str:
    return os.path.join(_checkpoint_dir(), f"{task_id}.json")


def save_checkpoint(cp: Checkpoint):
    with open(_checkpoint_path(cp.task_id), "w") as f:
        json.dump(cp.to_dict(), f, indent=2)


def load_checkpoint(task_id: str) -> Checkpoint | None:
    path = _checkpoint_path(task_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return Checkpoint.from_dict(json.load(f))


def delete_checkpoint(task_id: str):
    path = _checkpoint_path(task_id)
    if os.path.exists(path):
        os.remove(path)


def list_checkpoints() -> list[str]:
    """返回所有 checkpoint 的 task_id 列表。"""
    d = _checkpoint_dir()
    if not os.path.exists(d):
        return []
    return [f[:-5] for f in os.listdir(d) if f.endswith(".json")]
