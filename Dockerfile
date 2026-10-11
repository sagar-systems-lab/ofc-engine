FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir --disable-pip-version-check . \
    && useradd --system --uid 10001 --no-create-home ofc

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c 'import os; from urllib.request import urlopen; urlopen("http://127.0.0.1:"+os.environ.get("PORT", "8000")+"/readyz", timeout=2).close()' || exit 1
CMD ["python", "-m", "ofc_engine.hosted_preview", "--bind", "0.0.0.0"]
