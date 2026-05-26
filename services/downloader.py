from services.s3_client import get_s3_client


def get_object_info(bucket: str, key: str) -> dict:
    """获取对象元信息（不下载内容）。"""
    s3 = get_s3_client()
    resp = s3.head_object(Bucket=bucket, Key=key)
    return {
        "size": resp["ContentLength"],
        "content_type": resp.get("ContentType", "application/octet-stream"),
        "etag": resp.get("ETag", ""),
        "last_modified": resp["LastModified"].isoformat(),
    }


def download_object(bucket: str, key: str, range_header: str | None = None):
    """返回 S3 对象的内容流和元信息。
    如果传入 Range，S3 返回对应区间的数据。
    """
    s3 = get_s3_client()
    kwargs = {"Bucket": bucket, "Key": key}
    if range_header:
        kwargs["Range"] = range_header
    resp = s3.get_object(**kwargs)
    return {
        "body": resp["Body"],          # StreamingBody
        "size": resp["ContentLength"],
        "content_type": resp.get("ContentType", "application/octet-stream"),
        "etag": resp.get("ETag", ""),
        "content_range": resp.get("ContentRange", ""),
    }
