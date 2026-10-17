# PII detection service: Presidio with spaCy's large English model.
#
#   docker build -t pii-service:0.1.0 pii-service             # the service
#   docker build --target test pii-service                    # runs the tests

FROM python:3.12-slim AS base
# TLDEXTRACT_CACHE: Presidio's email and URL recognizers need the Public Suffix
# List. It's fetched once here, so the service makes no network calls at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TLDEXTRACT_CACHE=/app/.tldextract
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m spacy download en_core_web_lg \
    && python -c "import tldextract; tldextract.extract('example.com')"
COPY app ./app

FROM base AS test
RUN pip install httpx==0.28.1  # needed by FastAPI's TestClient
COPY tests ./tests
RUN python -m unittest discover -s tests -v

FROM base AS service
# tldextract takes a lock file even when reading its cache, so the service user owns it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin pii && chown -R pii /app/.tldextract
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
