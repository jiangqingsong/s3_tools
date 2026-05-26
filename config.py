from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # S3 连接（必填）
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str

    # S3 连接（选填）
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_use_ssl: bool = True
    s3_path_style: bool = False
    s3_signature_version: str = "s3v4"

    # 上传配置
    upload_temp_dir: str = "/tmp/s3-tools"
    multipart_threshold: int = 8 * 1024 * 1024      # 8MB
    part_size: int = 16 * 1024 * 1024                # 16MB
    max_upload_size: int = 50 * 1024 * 1024 * 1024   # 50GB

    # 服务配置
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    upload_concurrency: int = 4

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
