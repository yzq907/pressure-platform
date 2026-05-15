FROM python:3.11-slim

# 设置时区
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先拷贝依赖文件（利用 Docker 缓存层）
COPY api/pyproject.toml api/uv.lock ./

# 安装生产依赖
RUN uv sync --frozen --no-dev

# 拷贝应用代码
COPY api/app ./app
COPY api/jmx_base ./jmx_base

# 暴露端口
EXPOSE 4321

# 启动 FastAPI
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4321"]
