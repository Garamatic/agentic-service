# Garamatic Agentic Service - MCP Server
FROM python:3.12-slim

WORKDIR /app

# Install uv for fast package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency files
COPY pyproject.toml .
COPY src/ src/

# Install dependencies
RUN uv pip install --system -e ".[dev]"

# Copy source code
COPY src/ src/

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

# Expose port for SSE transport
EXPOSE 3001

# Default command runs MCP server over stdio
# For HTTP/SSE, override with: --transport sse --port 3001
CMD ["python", "-m", "agentic_service.server"]
