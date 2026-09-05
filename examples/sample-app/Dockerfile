FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS runtime
COPY app/ ./app/
ENV PORT=8080
EXPOSE 8080
USER 1000
CMD ["python", "app/server.py"]
