# S3 Tools API 服务 设计方案

## 1. 项目概述

将 S3 对象存储的能力封装为 RESTful API 服务，面向用户提供简洁的文件上传/下载接口。用户无需关心底层分片细节，只需提交文件、查询进度即可。所有 S3 连接信息通过环境变量注入，代码中不出现任何硬编码的敏感信息。

## 2. 技术选型

| 维度 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.12+ | 项目已有 `.venv`，生态成熟 |
| Web 框架 | **FastAPI** | 异步支持好、自动生成 OpenAPI 文档、类型校验完善、性能优异 |
| S3 SDK | `boto3` | AWS 官方 SDK，功能完整，兼容所有 S3 兼容存储（MinIO、Ceph 等） |
| 后台任务 | FastAPI `BackgroundTasks` | 大文件上传转为后台异步处理，避免 HTTP 超时 |
| 进度追踪 | 服务端内存 + Checkpoint 文件 | 上传进度可查询，Checkpoint 文件支持进程重启后恢复 |
| 配置管理 | `pydantic-settings` | 自动从环境变量加载，类型校验 |

## 3. 架构设计

### 3.1 整体架构

```
┌──────────────┐     HTTP/REST       ┌──────────────────┐      S3 Protocol      ┌──────────────┐
│   用户/客户端  │  ◄──────────────►  │  S3 Tools API    │  ◄─────────────────►  │   S3 存储    │
│              │                    │  (FastAPI)        │                       │  (MinIO/     │
│              │                    │  端口: 8080       │                       │   AWS/等)    │
└──────────────┘                    └──────────────────┘                       └──────────────┘
                                           │
                                    ┌──────┴──────┐
                                    │  临时文件存储  │
                                    │ /tmp/s3-tools/ │
                                    │   Checkpoint  │
                                    └──────────────┘

分层架构：

  api/          ← 对外接口层（用户可见的 REST 端点）
  services/     ← 业务逻辑层（上传/下载逻辑、分片策略、Checkpoint 管理）
  client.py     ← S3 客户端层（boto3 封装，所有 S3 操作经此层）
  config.py     ← 配置层（环境变量加载与校验）
```

**关键设计原则：分片逻辑是内部实现细节，不暴露给用户。**

### 3.2 上传流程（用户视角 vs 内部实现）

```
用户视角（只调一个接口）：

    POST /api/v1/upload  (上传文件 + key)
    └── 返回 {task_id, status: "processing"}
    
    GET /api/v1/upload/status/{task_id}  (轮询进度)
    └── 返回 {progress: 75%, completed_parts: 30/40}

服务端内部实现（用户无感知）：

    ┌─ 小文件 (< 8MB) ──────────────────────────┐
    │  temp_file → S3 put_object → 完成          │
    └────────────────────────────────────────────┘
    
    ┌─ 大文件 (≥ 8MB) ──────────────────────────┐
    │  temp_file                                  │
    │  → S3 CreateMultipartUpload                │
    │  → 逐片 UploadPart (并发)                   │
    │     ├─ 每片完成 → 更新 Checkpoint           │
    │     └─ 若中断 → 下次从 Checkpoint 恢复      │
    │  → S3 CompleteMultipartUpload              │
    │  → 删除 temp_file + Checkpoint → 完成      │
    └────────────────────────────────────────────┘
```

### 3.3 项目目录结构

```
s3_tools/
├── main.py                    # FastAPI 应用入口，路由注册，BackgroundTasks 管理
├── config.py                  # 配置管理：pydantic-settings 加载环境变量
├── api/
│   ├── __init__.py
│   ├── router.py              # 顶层路由聚合
│   ├── upload.py              # 上传接口（用户可见的简单端点）
│   ├── download.py            # 下载接口
│   └── objects.py             # 对象管理接口（list/delete/presign/cleanup）
├── services/
│   ├── __init__.py
│   ├── s3_client.py           # S3 客户端封装（boto3 统一管理，单例）
│   ├── uploader.py            # 上传业务逻辑（内部：判断大小、普通上传/分片上传）
│   ├── downloader.py          # 下载业务逻辑
│   ├── checkpoint.py          # 服务端 Checkpoint 文件读写
│   └── task_manager.py        # 上传任务状态管理（内存字典 + Checkpoint 持久化）
├── schemas/
│   ├── __init__.py
│   └── responses.py           # Pydantic 响应模型
├── requirements.txt           # fastapi[standard], boto3, pydantic-settings, python-multipart
├── .env.example               # 环境变量模板
├── .gitignore
└── DESIGN.md                  # 本方案文档
```

## 4. 配置设计

### 4.1 环境变量

所有 S3 连接信息通过环境变量获取，代码中不硬编码任何凭证：

```bash
# === S3 连接（必填）===
S3_ENDPOINT          # S3 服务地址，如 https://oss.example.com
S3_ACCESS_KEY        # 访问密钥/公钥
S3_SECRET_KEY        # 私钥

# === S3 连接（选填）===
S3_REGION            # 区域，默认 us-east-1
S3_BUCKET            # 默认 Bucket（可通过请求参数覆盖）
S3_USE_SSL           # 是否使用 SSL，默认 true
S3_VERIFY_SSL        # 是否校验 SSL 证书，默认 true。自签名证书需设为 false
S3_PATH_STYLE        # 是否强制 Path-Style 访问，默认 false。S3 compatible storage 通常需设为 true
S3_SIGNATURE_VERSION # 签名版本，默认 s3v4

# === 上传配置（选填）===
UPLOAD_TEMP_DIR      # 临时文件目录，默认 /tmp/s3-tools
MULTIPART_THRESHOLD  # 分片上传阈值（字节），默认 8388608（8MB）
PART_SIZE            # 分片大小（字节），默认 16777216（16MB）
MAX_UPLOAD_SIZE      # 单文件最大上传大小（字节），默认 53687091200（50GB）

# === 服务配置（选填）===
API_HOST             # 监听地址，默认 0.0.0.0
API_PORT             # 监听端口，默认 8080
UPLOAD_CONCURRENCY   # 分片上传并发数，默认 4
```

### 4.2 安全约束

- `.env` 文件加入 `.gitignore`
- `config.py` 中通过 `pydantic-settings` 校验必填项，启动时若缺失则报错退出
- `GET /api/v1/config/check` 返回配置状态（已脱敏）

### 4.3 Config 模块示例逻辑

```python
# config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_use_ssl: bool = True
    s3_path_style: bool = False
    s3_signature_version: str = "s3v4"
    upload_temp_dir: str = "/tmp/s3-tools"
    multipart_threshold: int = 8 * 1024 * 1024      # 8MB
    part_size: int = 16 * 1024 * 1024                # 16MB
    max_upload_size: int = 50 * 1024 * 1024 * 1024   # 50GB
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    upload_concurrency: int = 4

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
```

## 5. 大文件断点续传方案

### 5.1 核心机制：服务端 Checkpoint

断点续传解决的是"API 服务 → S3"这段的上传中断问题。用户在"用户 → API"阶段如果中断，需要重新上传文件到 API（这是 HTTP 上传本身的限制）。

流程：

1. 用户 POST 文件到 `/api/v1/upload`，文件先完整存储到 API 的临时目录
2. API 后台任务开始上传到 S3
3. 如果使用分片上传，每完成一个分片就写入 Checkpoint 文件
4. 如果 API 进程崩溃/重启，后台任务扫描 Checkpoint 文件，恢复未完成的上传任务
5. 全部分片完成后，调用 S3 CompleteMultipartUpload，删除 Checkpoint 和临时文件

### 5.2 中断恢复矩阵

| 中断场景 | 恢复方式 | 说明 |
|----------|----------|------|
| 用户→API 上传中断 | 用户重新调用 POST /upload | HTTP 请求无状态，需重新传输 |
| API→S3 单个分片失败 | boto3 自动重试（3次），仍失败则标记该分片待重试 | SDK 层面处理 |
| API 进程崩溃/重启 | 服务启动时扫描 checkpoint 目录，恢复未完成任务 | Checkpoint 持久化在磁盘 |
| 机器断电/重启 | 同上，Checkpoint 在磁盘上 | 前提是临时文件也在持久化磁盘上 |
| S3 upload_id 过期（24h） | 废弃旧 upload_id，重新 CreateMultipartUpload | 已上传分片被自动清理 |
| 用户上传过程中源文件变更 | API 计算文件 hash，上传完成后校验 ETag 一致性 | 确保文件完整性 |

### 5.3 Checkpoint 文件格式

存放位置：`{UPLOAD_TEMP_DIR}/checkpoints/{task_id}.json`

```json
{
  "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "file_path": "/tmp/s3-tools/uploads/a1b2c3d4-data.tar.gz",
  "file_size": 10737418240,
  "bucket": "my-bucket",
  "key": "backups/data.tar.gz",
  "upload_id": "2~zQrpFxkEPlM7S8B9hT0q3jVkW1xL4mN6pYy",
  "part_size": 16777216,
  "total_parts": 640,
  "completed_parts": [
    {"part_number": 1, "etag": "\"abc123...\"", "size": 16777216},
    {"part_number": 2, "etag": "\"def456...\"", "size": 16777216}
  ],
  "next_part_number": 3,
  "created_at": "2026-05-26T10:30:00Z",
  "updated_at": "2026-05-26T10:35:00Z"
}
```

### 5.4 进程重启恢复

服务启动时执行一次恢复扫描：

```
for each checkpoint file in {UPLOAD_TEMP_DIR}/checkpoints/:
    if upload_id 仍有效（未超过 24h）:
        将任务重新注册到 task_manager，恢复上传
    else:
        删除过期的 checkpoint（对应的 temp 文件也一并清理）
```

### 5.5 未完成 Upload 清理

提供 `POST /api/v1/objects/cleanup` 接口，清理 Bucket 中超过指定天数的未完成分片上传，避免产生存储费用。同时也会清理本地的过期 Checkpoint 和临时文件。

## 6. API 接口文档

服务根路径：`/api/v1`

### 6.1 上传相关

---

#### 6.1.1 上传文件

**`POST /api/v1/upload`**

用户上传文件。服务端接收文件后，内部自动判断大小并选择上传策略：
- 小文件（< `MULTIPART_THRESHOLD`）→ 同步调用 S3 `put_object`
- 大文件（≥ `MULTIPART_THRESHOLD`）→ 后台异步分片上传，返回 `task_id` 供进度查询

**请求**

- Content-Type: `multipart/form-data`
- Body:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file(binary) | 是 | 要上传的文件 |
| `key` | string | 是 | S3 对象 Key（路径），如 `backups/data.tar.gz` |
| `bucket` | string | 否 | Bucket 名称，不传则使用环境变量默认值 |
| `content_type` | string | 否 | 文件 MIME 类型，不传则自动检测 |
| `async_mode` | boolean | 否 | 是否强制异步模式。小文件默认同步，大文件默认异步 |

**同步响应 `200 OK`**（小文件）

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "task_id": "a1b2c3d4-...",
    "status": "completed",
    "key": "backups/data.tar.gz",
    "bucket": "my-bucket",
    "size": 5242880,
    "etag": "\"d41d8cd98f00b204e9800998ecf8427e\""
  }
}
```

**异步响应 `202 Accepted`**（大文件）

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "status": "processing",
    "key": "backups/large.iso",
    "bucket": "my-bucket",
    "total_size": 10737418240,
    "part_size": 16777216,
    "total_parts": 640,
    "progress_url": "/api/v1/upload/status/a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  }
}
```

---

#### 6.1.2 查询上传进度

**`GET /api/v1/upload/status/{task_id}`**

查询异步上传任务的实时进度。

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "status": "uploading",
    "key": "backups/large.iso",
    "progress": 0.45,
    "completed_parts": 288,
    "total_parts": 640,
    "uploaded_bytes": 4831838208,
    "total_bytes": 10737418240,
    "started_at": "2026-05-26T10:30:00Z"
  }
}
```

`status` 枚举值：

| status | 说明 |
|--------|------|
| `processing` | 已接收文件，排队等待上传 |
| `uploading` | 正在上传分片到 S3 |
| `completing` | 全部分片已上传，正在调用 S3 CompleteMultipartUpload |
| `completed` | 上传成功 |
| `failed` | 上传失败（含错误信息） |

---

#### 6.1.3 取消上传任务

**`POST /api/v1/upload/cancel/{task_id}`**

取消正在进行的异步上传任务。

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "cancelled",
  "data": {
    "task_id": "a1b2c3d4-...",
    "status": "cancelled"
  }
}
```

取消操作会调用 S3 `AbortMultipartUpload` 清理已上传分片，并删除本地临时文件和 Checkpoint。

---

### 6.2 下载相关

---

#### 6.2.1 文件下载

**`GET /api/v1/download`**

下载指定 Key 的文件。支持通过 `Range` 头实现客户端断点续传。

**请求**

- Query 参数:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `key` | string | 是 | S3 对象 Key |
| `bucket` | string | 否 | Bucket 名称 |
| `inline` | boolean | 否 | 设为 `true` 时浏览器内联展示，否则触发下载。默认 `false` |

- Headers:

| Header | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `Range` | string | 否 | 断点续传用，如 `bytes=1048576-` 表示从第 1MB 处开始下载 |

**响应 `200 OK`（完整下载）**

- Headers:
  - `Content-Length`: 文件总大小
  - `Content-Type`: MIME 类型
  - `Content-Disposition`: `attachment; filename="xxx"`
  - `Accept-Ranges`: `bytes`
  - `ETag`: 对象 ETag
- Body: 文件二进制流

**响应 `206 Partial Content`（断点续传）**

- Headers:
  - `Content-Range`: `bytes 1048576-2097151/5242880`
  - `Content-Length`: 本次返回的字节数
- Body: 文件的指定区间数据

---

#### 6.2.2 查询对象元信息

**`HEAD /api/v1/download`**

不下载文件内容，仅获取文件的元信息（大小、Content-Type、ETag 等），常用于下载前的预检。

**请求**

- Query 参数: 同下载接口

**响应 `200 OK`**

Headers:
- `Content-Length`: 文件大小
- `Content-Type`: MIME 类型
- `ETag`: 对象 ETag
- `Last-Modified`: 最后修改时间
- `Accept-Ranges`: `bytes`

---

### 6.3 对象管理

---

#### 6.3.1 列出对象

**`GET /api/v1/objects/list`**

列出指定 Bucket 下的对象。

**请求**

- Query 参数:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prefix` | string | 否 | 过滤前缀，如 `backups/` |
| `delimiter` | string | 否 | 分隔符，用于目录层级，默认为 `/` |
| `limit` | integer | 否 | 返回数量上限，默认 1000，最大 1000 |
| `marker` | string | 否 | 分页标记，从上一次响应的 `next_marker` 获取 |
| `bucket` | string | 否 | Bucket 名称 |

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "objects": [
      {
        "key": "backups/data.tar.gz",
        "size": 5242880,
        "last_modified": "2026-05-26T10:30:00Z",
        "etag": "\"d41d8cd98f...\"",
        "storage_class": "STANDARD"
      }
    ],
    "common_prefixes": ["backups/2025/", "backups/2026/"],
    "is_truncated": false,
    "next_marker": null
  }
}
```

---

#### 6.3.2 删除对象

**`DELETE /api/v1/objects/delete`**

**请求**

- Query 参数:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `key` | string | 是 | S3 对象 Key |
| `bucket` | string | 否 | Bucket 名称 |

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "deleted",
  "data": null
}
```

---

#### 6.3.3 批量删除对象

**`POST /api/v1/objects/batch-delete`**

一次删除多个对象（最多 1000 个）。

**请求**

- Content-Type: `application/json`
- Body:

```json
{
  "bucket": "my-bucket",
  "keys": ["backups/old1.tar.gz", "backups/old2.tar.gz"]
}
```

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "deleted": [{"key": "backups/old1.tar.gz"}, {"key": "backups/old2.tar.gz"}],
    "errors": []
  }
}
```

---

#### 6.3.4 生成预签名 URL

**`POST /api/v1/objects/presign`**

生成临时访问 URL，可用于分享给第三方直接下载/上传（绕过 API 服务，直连 S3）。

**请求**

- Content-Type: `application/json`
- Body:

```json
{
  "key": "backups/data.tar.gz",
  "bucket": "my-bucket",
  "expires": 3600,
  "method": "get_object"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `key` | string | 是 | S3 对象 Key |
| `expires` | integer | 否 | 过期时间（秒），默认 3600，最大 604800（7天） |
| `method` | string | 否 | 操作方法，默认 `get_object`，可选 `put_object` |
| `bucket` | string | 否 | Bucket 名称 |

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "url": "https://oss.example.com/my-bucket/backups/data.tar.gz?X-Amz-Expires=3600&...",
    "expires_at": "2026-05-26T11:30:00Z",
    "method": "get_object"
  }
}
```

---

#### 6.3.5 清理未完成的分片上传

**`POST /api/v1/objects/cleanup`**

查询并清理 Bucket 中未完成的分片上传，同时清理本地过期 Checkpoint。

**请求**

- Content-Type: `application/json`
- Body:

```json
{
  "bucket": "my-bucket",
  "older_than_days": 1,
  "dry_run": true
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `older_than_days` | integer | 否 | 仅清理超过 N 天的，默认 1 |
| `dry_run` | boolean | 否 | `true` 仅列出不删除，默认 `true` |
| `bucket` | string | 否 | Bucket 名称 |

**响应 `200 OK`**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "dry_run": true,
    "s3_uploads_found": 3,
    "local_checkpoints_cleaned": 1,
    "details": [
      {
        "upload_id": "2~zQrpFxkE...",
        "key": "backups/large.iso",
        "initiated": "2026-05-25T10:30:00Z",
        "age_hours": 25
      }
    ]
  }
}
```

---

### 6.4 系统相关

---

#### 6.4.1 健康检查

**`GET /api/v1/health`**

```json
{
  "status": "ok",
  "s3_reachable": true,
  "timestamp": "2026-05-26T10:30:00Z"
}
```

---

#### 6.4.2 配置检查

**`GET /api/v1/config/check`**

查看当前配置（脱敏），用于部署验证。

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "s3_endpoint": "https://oss.example.com",
    "s3_region": "us-east-1",
    "s3_bucket": "my-bucket",
    "s3_access_key": "AKID**** (已设置)",
    "s3_secret_key": "**** (已设置)",
    "upload_temp_dir": "/tmp/s3-tools",
    "multipart_threshold": 8388608,
    "part_size": 16777216,
    "max_upload_size": 53687091200,
    "api_port": 8080
  }
}
```

---

### 6.5 统一错误码

| 错误码 | HTTP 状态码 | 说明 |
|--------|-------------|------|
| 0 | 200 | 成功 |
| 40001 | 400 | 参数校验失败 |
| 40002 | 400 | 文件大小超出限制 |
| 40101 | 401 | S3 认证失败（Access Key / Secret Key 错误） |
| 40301 | 403 | S3 权限不足 |
| 40401 | 404 | 对象不存在 |
| 40402 | 404 | Bucket 不存在 |
| 40403 | 404 | 上传任务不存在（task_id 无效或已过期） |
| 40901 | 409 | 上传任务已被取消 |
| 50001 | 500 | 服务内部错误 |
| 50002 | 500 | S3 服务不可达 |
| 50301 | 503 | 服务临时不可用 |

所有错误响应统一格式：

```json
{
  "code": 40401,
  "message": "对象不存在: path/to/file.tar.gz",
  "data": null
}
```

---

### 6.6 接口汇总

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/upload` | 上传文件 |
| GET | `/api/v1/upload/status/{task_id}` | 查询上传进度 |
| POST | `/api/v1/upload/cancel/{task_id}` | 取消上传 |
| GET | `/api/v1/download` | 下载文件（支持 Range 续传） |
| HEAD | `/api/v1/download` | 查询对象元信息 |
| GET | `/api/v1/objects/list` | 列出对象 |
| DELETE | `/api/v1/objects/delete` | 删除对象 |
| POST | `/api/v1/objects/batch-delete` | 批量删除 |
| POST | `/api/v1/objects/presign` | 生成预签名 URL |
| POST | `/api/v1/objects/cleanup` | 清理未完成的分片上传 |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/config/check` | 配置检查（脱敏） |

## 7. 待讨论问题

1. **上传模式**：当前设计是用户先把文件完整传到 API，API 再后台传到 S3。对于 50GB 的大文件，用户→API 的传输本身可能中断。是否需要提供"用户端分片上传到 API"的能力（让用户断点续传到 API），还是认为这个场景由 HTTP Range/Resumable Uploads 或客户端 SDK 自行处理即可？

2. **同步 vs 异步返回值**：当前设计小文件同步返回（等 S3 写入完成）、大文件异步返回（202 + task_id）。这个策略 OK 吗？还是所有上传都走异步？

3. **域名风格**：你使用的 S3 兼容存储是否需要强制 Path-Style？影响 `botocore` 的 `addressing_style` 配置。

4. **认证**：API 服务本身是否需要鉴权（API Key / Token），还是部署在内网无需认证？

5. **上传进度推送**：除了轮询 `GET /status/{task_id}`，是否需要 WebSocket 推送进度？还是轮询就够了？

6. **多实例部署**：如果需要多副本部署，Checkpoint 和临时文件需要改为共享存储（如 NFS）或改用 Redis。当前单实例是否满足需求？

---

请基于以上方案提出你的意见和调整方向，对齐后我输出开发计划。
