import boto3
from botocore.config import Config as BotoConfig
from config import settings

_client = None


def get_s3_client() -> boto3.client:
    """返回 boto3 S3 client 单例，首次调用时创建。"""
    global _client
    if _client is None:
        boto_config = BotoConfig(
            signature_version=settings.s3_signature_version,
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path" if settings.s3_path_style else "virtual"},
        )
        session = boto3.Session(
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
        _client = session.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            use_ssl=settings.s3_use_ssl,
            config=boto_config,
        )
    return _client
