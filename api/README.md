# Mysterious API (Python 重构版)

基于 FastAPI + SQLAlchemy 2.0 的分布式压测平台后端，对应原 Java SpringBoot 项目的功能重构。

## 环境要求

- Python 3.11+
- uv（包管理器）
- MySQL 8.0（库 `mysterious`，初始化用 `../docker/init.sql`）
- Redis 6+

## 快速启动

```bash
# 1. 安装 uv（如未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 准备配置
cp .env.example .env
# 按需修改 .env 里的 MYSQL_URL / REDIS_URL

# 3. 同步依赖（自动创建 .venv）
uv sync

# 4. 启动服务
uv run uvicorn app.main:app --reload --port 4321
```

启动成功后：

- 健康检查：`curl http://localhost:4321/health`
- Swagger UI：http://localhost:4321/swagger-ui.html
- OpenAPI JSON：http://localhost:4321/v2/api-docs

## 测试

```bash
uv run pytest -v
```

## 项目结构

```
app/
├── main.py            # FastAPI 应用入口
├── core/              # 配置、响应包装、异常、安全、日志、ContextVar
├── db/                # SQLAlchemy 引擎、Session、Base/Mixin
├── deps/              # FastAPI 依赖（鉴权等）
├── api/v1/            # 路由
├── models/            # ORM 模型（Phase 1+）
├── schemas/           # Pydantic Param/VO/Query（Phase 1+）
├── crud/              # 数据访问（Phase 1+）
└── services/          # 业务服务（Phase 1+）
```

## 与 Java 后端的关系

- 数据库表结构完全沿用 `docker/init.sql`，不重新建表
- 服务端口 4321，与 Java 端一致；切换时需先停 Java 服务
- 统一返回格式 `{code, message, success, currentTime, data}` 与 Java 端 1:1 兼容，前端无需改动
- 鉴权 token 与 Java 同库共用（DB 里的 `mysterious_user.token`）
