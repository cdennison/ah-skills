FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY search.py mcp_search.py query_service.py scan_service.py ./
COPY prompts ./prompts

ENV SKILLS_QDRANT_URL=http://qdrant:6333
ENV MCP_QDRANT_URL=http://qdrant:6333
EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "query_service:app", "--host", "0.0.0.0", "--port", "8000"]
