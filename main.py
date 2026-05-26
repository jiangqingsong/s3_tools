import threading
from fastapi import FastAPI
from contextlib import asynccontextmanager
from api.router import router
from services.s3_client import get_s3_client
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=check_s3_on_startup, daemon=True).start()
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
