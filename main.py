import os
import threading
import time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from contextlib import asynccontextmanager
from api.router import router
from services.s3_client import get_s3_client
from services.checkpoint import load_checkpoint, delete_checkpoint, list_checkpoints
from services.uploader import resume_multipart_upload
from services.task_manager import task_manager
from config import settings


def check_s3_on_startup():
    """在后台线程中校验 S3 连通性，不阻塞启动。"""
    try:
        s3 = get_s3_client()
        if settings.s3_bucket:
            s3.head_bucket(Bucket=settings.s3_bucket)
        print("[INFO] S3 连通性检查通过")
    except Exception as e:
        print(f"[WARN] S3 连通性检查失败: {e}")
        print("[WARN] 服务已启动，但 S3 操作可能不可用")


def recover_incomplete_uploads():
    """扫描 checkpoint 目录，恢复未完成的分片上传任务。"""
    task_ids = list_checkpoints()
    if not task_ids:
        return
    print(f"[INFO] 发现 {len(task_ids)} 个未完成的 checkpoint，开始恢复...")
    expired_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    for task_id in task_ids:
        cp = load_checkpoint(task_id)
        if cp is None:
            continue
        try:
            created = datetime.fromisoformat(cp.created_at)
            if created < expired_cutoff:
                print(f"[INFO] Checkpoint {task_id} 已超过 24h，清理本地残留")
                delete_checkpoint(task_id)
                if os.path.exists(cp.file_path):
                    os.remove(cp.file_path)
                continue

            s3 = get_s3_client()
            try:
                s3.list_parts(Bucket=cp.bucket, Key=cp.key, UploadId=cp.upload_id)
            except Exception:
                print(f"[WARN] Checkpoint {task_id} upload_id 已失效，清理本地残留")
                delete_checkpoint(task_id)
                if os.path.exists(cp.file_path):
                    os.remove(cp.file_path)
                continue

            task_manager.recover_from_checkpoint(cp, cp.upload_id)
            print(f"[INFO] Checkpoint {task_id} 恢复成功，继续上传 ({cp.next_part_number - 1}/{cp.total_parts})")
            t = threading.Thread(
                target=resume_multipart_upload,
                args=(task_id, None),
                daemon=True,
            )
            t.start()
        except Exception as e:
            print(f"[WARN] Checkpoint {task_id} 恢复失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=check_s3_on_startup, daemon=True).start()
    time.sleep(0.5)
    threading.Thread(target=recover_incomplete_uploads, daemon=True).start()
    yield


app = FastAPI(
    title="S3 Tools API",
    version="1.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
    )
