FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY tools ./tools
COPY adapters ./adapters
COPY apps ./apps
COPY policies ./policies
COPY runbooks ./runbooks
COPY templates ./templates
COPY examples ./examples
COPY AGENTS.md ./
RUN pip install --no-cache-dir -e ".[dev]"
ENTRYPOINT []
CMD ["devops-agent", "--help"]
