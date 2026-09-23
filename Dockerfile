FROM python:3.12-slim

WORKDIR /app

# 安装系统基础证书和依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码和静态网页
COPY server.py index.html ./

# 创建数据存储目录
RUN mkdir -p /app/data

ENV HOST=0.0.0.0
ENV PORT=8765
ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "server.py"]
