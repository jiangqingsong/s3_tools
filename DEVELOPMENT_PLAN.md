# S3 Tools — 上传/下载 开发计划

> 基于 [DESIGN.md](./DESIGN.md) 第 7 节用户反馈，按功能模块拆分开发步骤。

## 前置确认

| 决策项 | 结论 |
|--------|------|
| 用户→API 传输中断 | 客户端自行处理 |
| 上传同步/异步策略 | 小文件同步，大文件异步（保持 DESIGN.md 策略） |
| S3 存储类型 | S3 compatible storage（Path-Style 将作为可配置项保留，默认 false） |
| API 鉴权 | 无鉴权，内网调用 |
| 进度推送 | 仅轮询 |
| 部署模式 | 单实例 |

## 开发步骤总览

```
  Phase 1          Phase 2          Phase 3          Phase 4
  ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
  │ 项目骨架 │ ──► │ 小文件   │ ──► │ 大文件   │ ──► │ 下载     │
  │ 配置    │     │ 上传     │     │ 分片上传 │     │ 断点续传 │
  └─────────┘     └─────────┘     └─────────┘     └─────────┘
        │               │               │               │
        ▼               ▼               ▼               ▼
     可启动服务      小文件上          大文件上传       下载完成
     S3 连通         传完成           进度可查         支持 Range
     健康检查                         断点可恢复
```

## Phase 1：项目骨架 + 配置模块 + S3 客户端

### 目标
搭好项目框架，确保服务能启动、配置能校验、S3 能连通。

### 任务清单

| # | 任务 | 产出 | 验证方式 |
|---|------|------|----------|
| 1.1 | 创建 `requirements.txt`，安装依赖 | 依赖就绪 | `pip list` 确认 fastapi, boto3, pydantic-settings, python-multipart |
| 1.2 | 实现 `config.py`：`pydantic-settings` 加载所有环境变量，必填项校验 | 配置模块 | 缺必填项时启动报错并提示缺失变量名 |
| 1.3 | 实现 `services/s3_client.py`：boto3 Session + Client 封装，从 config 读取参数构造 | S3 客户端单例 | 调用 `head_bucket` 确认连通 |
| 1.4 | 实现 `main.py`：FastAPI 应用骨架，挂载路由，启动事件中校验 S3 连通性 | 服务入口 | `uvicorn main:app` 启动成功 |
| 1.5 | 实现 `GET /api/v1/health` | 健康检查端点 | `curl /health` 返回 `{"status":"ok","s3_reachable":true}` |
| 1.6 | 实现 `GET /api/v1/config/check` | 配置检查端点（脱敏） | 返回脱敏后的配置信息 |
| 1.7 | 创建 `.env.example` 和 `.gitignore` | 环境模板 | `.env` 不在 git 追踪中 |

### 目录结构（Phase 1 完成时）

```
s3_tools/
├── main.py
├── config.py
├── api/
│   ├── __init__.py
│   └── router.py
├── services/
│   ├── __init__.py
│   └── s3_client.py
├── schemas/
│   ├── __init__.py
│   └── responses.py
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Phase 2：小文件上传（同步）

### 目标
实现 `POST /api/v1/upload`，用户上传文件后，服务端判断为小文件时走 `put_object` 同步返回结果。

### 任务清单

| # | 任务 | 产出 | 验证方式 |
|---|------|------|----------|
| 2.1 | 实现 `schemas/responses.py`：统一响应模型 `ApiResponse`（code, message, data） | 响应 Schema | FastAPI 自动生成的 OpenAPI 文档中可见 |
| 2.2 | 实现 `api/upload.py`：`POST /api/v1/upload` 端点，接收 multipart/form-data | 上传端点 | 参数校验正确，自动文档可测试 |
| 2.3 | 实现上传流程：文件接收 → 存临时目录 → 判断大小 → < 阈值则 `put_object` → 返回同步结果 | 小文件上传 | `curl -F "file=@test.txt" -F "key=test.txt" /upload`，S3 上可查到文件 |
| 2.4 | 文件上传完成后清理本地临时文件 | 临时文件清理 | 上传完成后 `/tmp/s3-tools/` 下无残留文件 |
| 2.5 | 参数校验与错误处理：bucket 缺失、key 不合法、文件超限、S3 异常 | 错误处理 | 各种异常场景返回正确的错误码和 message |

### 接口（Phase 2 完成时）

```
POST /api/v1/upload          ← 小文件走同步返回
GET  /api/v1/health
GET  /api/v1/config/check
```

---

## Phase 3：大文件上传 + 断点续传

### 目标
实现大文件（≥8MB）的后台分片上传、进度查询、取消、以及进程重启后从 Checkpoint 恢复。

### 任务清单

| # | 任务 | 产出 | 验证方式 |
|---|------|------|----------|
| 3.1 | 实现 `services/checkpoint.py`：Checkpoint 文件的创建、更新、读取、删除 | Checkpoint 模块 | 单元测试：写入→读取→更新→删除 |
| 3.2 | 实现 `services/task_manager.py`：上传任务状态管理（内存字典），支持创建任务、更新进度、查询状态、取消任务 | 任务管理器 | 创建任务→更新进度→查询状态→取消→清理 |
| 3.3 | 实现 `services/uploader.py` 分片上传逻辑：`CreateMultipartUpload` → 分片 `UploadPart`（并发）→ 每片完成后更新 Checkpoint → `CompleteMultipartUpload` | 分片上传引擎 | 上传一个大文件到 S3，确认文件完整 |
| 3.4 | 扩展 `api/upload.py` 中 `POST /api/v1/upload`：≥ 阈值时分叉到异步分片流程，返回 202 + task_id | 大小判断分叉 | 上传 > 8MB 文件返回 202，< 8MB 返回 200 |
| 3.5 | 实现 `GET /api/v1/upload/status/{task_id}` | 进度查询 | 上传大文件过程中轮询，progress 从 0 增长到 1.0 |
| 3.6 | 实现 `POST /api/v1/upload/cancel/{task_id}` | 取消上传 | 取消后 S3 上无残留分片，本地临时文件和 Checkpoint 已清理 |
| 3.7 | 实现启动恢复扫描：`main.py` 启动时扫描 checkpoint 目录，恢复未完成任务到 task_manager 并继续上传 | 进程重启恢复 | 上传进行中 kill 进程，重启后任务自动恢复，status 接口可查到进度 |
| 3.8 | Checkpoint 和临时文件过期清理：启动时和定时清理超过 24h 的残留 Checkpoint 和临时文件 | 过期清理 | 过期文件被自动删除 |

### 接口（Phase 3 完成时）

```
POST /api/v1/upload               ← 小文件同步 / 大文件异步
GET  /api/v1/upload/status/{id}   ← 进度查询
POST /api/v1/upload/cancel/{id}   ← 取消上传
GET  /api/v1/health
GET  /api/v1/config/check
```

---

## Phase 4：下载 + 断点续传

### 目标
实现文件下载，支持 Range 头实现客户端断点续传。

### 任务清单

| # | 任务 | 产出 | 验证方式 |
|---|------|------|----------|
| 4.1 | 实现 `services/downloader.py`：封装下载逻辑，支持 Range 参数转发 | 下载服务 | 完整下载文件内容正确 |
| 4.2 | 实现 `api/download.py`：`GET /api/v1/download`，流式返回文件，支持 Range 头 | 下载端点 | `curl -O` 下载文件，`diff` 与原文件一致 |
| 4.3 | 实现 Range 响应：无 Range → 200 + 完整文件；有 Range → 206 + Content-Range | 断点续传 | `curl -H "Range: bytes=0-99"` 只返回前 100 字节 |
| 4.4 | 实现 `HEAD /api/v1/download`：返回元信息（Content-Length, ETag, Last-Modified） | 元信息查询 | `curl -I` 返回正确的 headers |
| 4.5 | 实现 `Content-Disposition` 支持：`?inline=true` 内联展示，否则 attachment 下载 | 文件展示控制 | 浏览器中 inline 预览 vs 触发下载 |

### 接口（Phase 4 完成时 — 全部）

```
POST /api/v1/upload               # 上传文件
GET  /api/v1/upload/status/{id}   # 查询上传进度
POST /api/v1/upload/cancel/{id}   # 取消上传
GET  /api/v1/download             # 下载文件（支持 Range）
HEAD /api/v1/download             # 查询对象元信息
GET  /api/v1/health               # 健康检查
GET  /api/v1/config/check         # 配置检查（脱敏）
```

---

## 开发顺序说明

Phase 1 → Phase 2 → Phase 3 → Phase 4 严格串行：

- Phase 1 是基础设施，所有后续阶段依赖它
- Phase 2 先跑通最简单的上传链路（小文件），验证 S3 客户端正确、接口设计合理
- Phase 3 在 Phase 2 的接口上扩展（同一个 `POST /upload` 端点增加分叉逻辑），不是新建接口，改动可控
- Phase 4 独立于上传，但依赖 Phase 1 的 S3 客户端和项目骨架

---

## 进度跟踪

| Phase | 状态 | 开始时间 | 完成时间 |
|-------|------|----------|----------|
| Phase 1 项目骨架 | ✅ 已完成 | 2026-05-26 | 2026-05-26 |
| Phase 2 小文件上传 | ✅ 已完成 | 2026-05-26 | 2026-05-26 |
| Phase 3 大文件上传 | ⬜ 待开始 | — | — |
| Phase 4 下载 | ⬜ 待开始 | — | — |

---

Phase 1 已完成，详情参见 [DESIGN.md](./DESIGN.md#6-api接口文档)。
