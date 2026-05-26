# S3 Tools — 上传/下载 API 设计方案

> 本文档从 [DESIGN_base.md](./DESIGN_base.md) 中提取上传与下载部分，作为当前阶段的聚焦范围。

## 1. 项目概述

将 S3 对象存储的文件上传/下载能力封装为 RESTful API 服务。用户无需关心底层分片细节，只需提交文件、查询进度即可。所有 S3 连接信息通过环境变量注入，代码中不出现任何硬编码的敏感信息。

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

  api/upload.py      ← 对外接口层（用户可见的上传/下载端点）
  api/download.py
  services/uploader.py   ← 业务逻辑层（上传策略、分片、Checkpoint）
  services/downloader.py
  services/s3_client.py  ← S3 客户端层（boto3 封装）
  services/checkpoint.py ← Checkpoint 持久化
  services/task_manager.py ← 上传任务状态管理
  config.py              ← 配置层（环境变量加载与校验）
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

    ┌─ 小文件 (< MULTIPART_THRESHOLD, 默认 8MB) ───┐
    │  temp_file → S3 put_object → 完成             │
    └───────────────────────────────────────────────┘

    ┌─ 大文件 (≥ MULTIPART_THRESHOLD) ─────────────┐
    │  temp_file                                     │
    │  → S3 CreateMultipartUpload                   │
    │  → 逐片 UploadPart（并发）                     │
    │     ├─ 每片完成 → 更新 Checkpoint              │
    │     └─ 若中断 → 下次从 Checkpoint 恢复         │
    │  → S3 CompleteMultipartUpload                 │
    │  → 删除 temp_file + Checkpoint → 完成         │
    └───────────────────────────────────────────────┘
```

### 3.3 下载流程

```
用户视角：

    GET /api/v1/download?key=backups/data.tar.gz
    └── 响应文件二进制流

    HEAD /api/v1/download?key=backups/data.tar.gz
    └── 获取文件大小、ETag 等信息（用于下载前预检）

断点续传：
    客户端记录已下载的字节数，请求时带 Range 头：
    GET /api/v1/download?key=xxx  +  Header Range: bytes=1048576-
    └── 响应 206 Partial Content，从断点处继续
```

### 3.4 项目目录结构

```
s3_tools/
├── main.py                    # FastAPI 应用入口，路由注册，启动恢复扫描
├── config.py                  # 配置管理：pydantic-settings 加载环境变量
├── api/
│   ├── __init__.py
│   ├── upload.py              # 上传相关接口
│   └── download.py            # 下载相关接口
├── services/
│   ├── __init__.py
│   ├── s3_client.py           # S3 客户端封装（boto3 单例）
│   ├── uploader.py            # 上传业务逻辑（内部：判断大小、put_object / multipart）
│   ├── downloader.py          # 下载业务逻辑
│   ├── checkpoint.py          # Checkpoint 文件读写
│   └── task_manager.py        # 上传任务状态管理（内存 + Checkpoint 持久化）
├── schemas/
│   ├── __init__.py
│   └── responses.py           # Pydantic 响应模型
├── requirements.txt           # fastapi[standard], boto3, pydantic-settings, python-multipart
├── .env.example               # 环境变量模板
├── .gitignore
├── DESIGN.md                  # 本方案文档（上传/下载）
└── DESIGN_base.md             # 完整方案文档（含对象管理等）
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

### 4.3 Config 模块

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
    s3_verify_ssl: bool = True
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

断点续传解决的是"API 服务 → S3"这段的上传中断问题。如果用户在"用户 → API"阶段中断，需要重新上传文件（这是 HTTP 单次请求本身的限制）。

流程：

1. 用户 POST 文件到 `/api/v1/upload`，文件先完整存储到 API 的临时目录
2. API 后台任务开始上传到 S3
3. 如果使用分片上传，每完成一个分片就写入 Checkpoint 文件
4. 如果 API 进程崩溃/重启，启动扫描恢复未完成任务
5. 全部分片完成后，调用 S3 CompleteMultipartUpload，删除 Checkpoint 和临时文件

### 5.2 中断恢复矩阵

| 中断场景 | 恢复方式 | 说明 |
|----------|----------|------|
| 用户→API 上传中断 | 用户重新调用 POST /upload | HTTP 无状态，需重新传输 |
| API→S3 单个分片失败 | boto3 自动重试（3次），仍失败则标记该分片待重试 | SDK 层面处理 |
| API 进程崩溃/重启 | 启动时扫描 checkpoint 目录，恢复未完成任务 | Checkpoint 持久化在磁盘 |
| 机器断电/重启 | 同上，Checkpoint 在磁盘上 | 前提是临时文件也在持久化磁盘 |
| S3 upload_id 过期（24h） | 废弃旧 upload_id，重新 CreateMultipartUpload | 已上传分片被自动清理 |
| 用户上传过程中源文件变更 | API 可计算文件 hash 做完整性校验 | 可选特性 |

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

### 5.5 分片大小策略

服务端根据文件大小自动计算分片大小：

| 文件大小 | 分片大小 | 最大分片数 |
|----------|----------|------------|
| < 8 MB | 不分片（put_object） | — |
| 8 MB ~ 1 GB | 8 MB | ~128 |
| 1 GB ~ 10 GB | 16 MB | ~640 |
| 10 GB ~ 50 GB | 32 MB | ~1600 |

S3 Multipart Upload 约束：至少 1 个分片，最多 10000 个分片，每片 5MB~5GB（最后一片可小于 5MB）。

## 6. API 接口文档

服务根路径：`/api/v1`

### 6.1 上传

---

#### 6.1.1 上传文件

**`POST /api/v1/upload`**

用户上传文件。服务端接收文件后，内部自动判断大小并选择上传策略：
- 小文件（< `MULTIPART_THRESHOLD`，默认 8MB）→ 同步调用 S3 `put_object`，直接返回结果
- 大文件（≥ `MULTIPART_THRESHOLD`）→ 后台异步分片上传，返回 `task_id` 供进度查询

**请求**

- Content-Type: `multipart/form-data`
- Body:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file(binary) | 是 | 要上传的文件 |
| `key` | string | 是 | S3 对象 Key（路径）。以 `/` 结尾时自动拼接原始文件名，如 `backups/` + `data.tar.gz` → `backups/data.tar.gz` |
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
    "total_parts": 640
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
| `failed` | 上传失败（含 `error_message`） |
| `cancelled` | 已被用户取消 |

---

#### 6.1.3 取消上传任务

**`POST /api/v1/upload/cancel/{task_id}`**

取消正在进行的异步上传任务。会调用 S3 `AbortMultipartUpload` 清理已上传分片，并删除本地临时文件和 Checkpoint。

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

---

### 6.2 下载

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

### 6.3 系统

---

#### 6.3.1 健康检查

**`GET /api/v1/health`**

```json
{
  "status": "ok",
  "s3_reachable": true,
  "timestamp": "2026-05-26T10:30:00Z"
}
```

---

#### 6.3.2 配置检查

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
    "s3_use_ssl": true,
    "s3_verify_ssl": true,
    "s3_path_style": false,
    "upload_temp_dir": "/tmp/s3-tools",
    "multipart_threshold": 8388608,
    "part_size": 16777216,
    "max_upload_size": 53687091200,
    "api_port": 8080
  }
}
```

---

### 6.4 统一错误码

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

### 6.5 接口汇总

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/upload` | 上传文件 |
| GET | `/api/v1/upload/status/{task_id}` | 查询上传进度 |
| POST | `/api/v1/upload/cancel/{task_id}` | 取消上传 |
| GET | `/api/v1/download` | 下载文件（支持 Range 续传） |
| HEAD | `/api/v1/download` | 查询对象元信息 |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/config/check` | 配置检查（脱敏） |

## 7. 已确认决策

| # | 问题 | 决策 |
|---|------|------|
| 1 | 用户→API 上传中断 | 客户端自行处理，API 不处理此场景 |
| 2 | 上传同步/异步策略 | 小文件同步，大文件异步 |
| 3 | 域名风格 | S3 compatible storage，`S3_PATH_STYLE` 和 `S3_VERIFY_SSL` 可配 |
| 4 | API 鉴权 | 内网部署，无需鉴权 |
| 5 | 进度推送 | 仅轮询 `GET /status/{task_id}`，不需 WebSocket |
| 6 | 多实例部署 | 当前仅单实例 |

## 8. S3 Compatible Storage 部署备忘

使用自建 S3 服务（如 MinIO 等）时，常见的三个配置：

| 问题 | 现象 | 配置 |
|------|------|------|
| 服务启动卡住 | Waiting for application startup 一直不结束 | 已修：S3 检查改为后台线程 + 5s 超时 |
| SSL 证书错误 | certificate verify failed | `S3_VERIFY_SSL=false` |
| 连不上 S3 | could not connect to endpoint URL | `S3_PATH_STYLE=true` |
| HTTP 连接 | — | `S3_ENDPOINT=http://xxx:8060/` + `S3_USE_SSL=false` |
