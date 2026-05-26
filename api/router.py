from fastapi import APIRouter
from services.s3_client import get_s3_client
from config import settings

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health():
    try:
        s3 = get_s3_client()
        if settings.s3_bucket:
            s3.head_bucket(Bucket=settings.s3_bucket)
        s3_reachable = True
    except Exception:
        s3_reachable = False
    return {"status": "ok", "s3_reachable": s3_reachable}


@router.get("/config/check")
def config_check():
    def mask(s: str) -> str:
        if len(s) <= 4:
            return "****"
        return s[:4] + "****"

    return {
        "code": 0,
        "message": "success",
        "data": {
            "s3_endpoint": settings.s3_endpoint,
            "s3_region": settings.s3_region,
            "s3_bucket": settings.s3_bucket or "(未设置)",
            "s3_access_key": f"{mask(settings.s3_access_key)} (已设置)",
            "s3_secret_key": "**** (已设置)",
            "upload_temp_dir": settings.upload_temp_dir,
            "multipart_threshold": settings.multipart_threshold,
            "part_size": settings.part_size,
            "max_upload_size": settings.max_upload_size,
            "api_port": settings.api_port,
        },
    }
