# Full FEniCSx runtime by default; override for a lightweight mock-only image:
#   docker build --build-arg BASE_IMAGE=python:3.12-slim -f server/Dockerfile .
ARG BASE_IMAGE=dolfinx/dolfinx:stable
FROM ${BASE_IMAGE}

WORKDIR /app
COPY server/ /app/server/
COPY examples/ /app/examples/
COPY protocol/ /app/protocol/

RUN pip install --no-cache-dir /app/server

EXPOSE 8000
CMD ["uvicorn", "fenixspoon.main:app", "--host", "0.0.0.0", "--port", "8000"]
