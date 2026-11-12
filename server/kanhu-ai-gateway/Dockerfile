# 基础镜像走 1ms.run (实测比 daocloud 快很多;
# 服务器全局 daemon.json 的加速器已失效, 但这里不动全局配置)
FROM docker.1ms.run/library/python:3.10-slim

# 时区对齐国内 (事件时间戳 / 日志)
RUN ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

# ffmpeg: edge-tts 出的是 mp3, 要转 PCM16 给板子
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pypi 用国内源 (实测服务器直连 pypi.org 也通, 但国内源更快)
# --extra-index-url 保留官方源做兜底
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/

# 先装依赖再拷代码 —— 改代码不会触发重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX" \
        --extra-index-url https://pypi.org/simple/ \
        -r requirements.txt

COPY app.py .

# 独立数据目录 (设备表挂载点)
RUN mkdir -p /kanhu-ai/data

EXPOSE 18000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18000/v1/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18000", "--workers", "1"]
