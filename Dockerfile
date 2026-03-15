FROM cgr.dev/chainguard/python:latest-dev AS builder

WORKDIR /app

COPY --chown=nonroot:nonroot pyproject.toml .
COPY --chown=nonroot:nonroot src/ src/

RUN pip install --no-cache-dir --prefix=/home/nonroot/install .

FROM cgr.dev/chainguard/python:latest

WORKDIR /app

COPY --from=builder /home/nonroot/install /usr/

ENV DRY_RUN=true
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "assure_package_cleaner"]
