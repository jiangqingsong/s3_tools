from fastapi import FastAPI
from contextlib import asynccontextmanager
from api.router import router
from services.s3_client import get_s3_client
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：校验 S3 连通性
    try:
        s3 = get_s3_client()
        if settings.s3_bucket:
            s3.head_bucket(Bucket=settings.s3_bucket)
    except Exception as e:
        print(f"[WARN] S3 连通性检查失败: {e}")
        print("[WARN] 服务将继续启动，但 S3 操作可能不可用")
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
