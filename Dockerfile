FROM cgr.dev/chainguard/python:latest-dev AS builder

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install .

FROM cgr.dev/chainguard/python:latest

WORKDIR /app

COPY --from=builder /install /usr/

ENV DRY_RUN=true
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "assure_package_cleaner"]
