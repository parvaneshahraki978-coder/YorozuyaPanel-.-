# YorozuyaPanel — image سبک برای Render (Docker runtime)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data \
    PORT=10000

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# فقط فایل‌های برنامه کپی می‌شن (data/ محلی وارد image نمی‌شه)
COPY main.py ./
COPY static ./static

RUN useradd --create-home --uid 1000 yorozuya \
    && mkdir -p /app/data \
    && chown -R yorozuya:yorozuya /app/data
USER yorozuya

EXPOSE 10000

CMD ["python", "main.py"]
