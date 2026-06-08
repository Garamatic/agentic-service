# Garamatic Agentic Service - MCP Server
FROM python:3.12-slim

WORKDIR /app

# Install uv via pip (ghcr.io auth not available)
RUN pip install --no-cache uv

# Copy dependency file and source
COPY pyproject.toml .
COPY README.md .
COPY src/ src/

# Install dependencies
RUN uv pip install --system --no-cache -e "."

# Ensure data directory exists for the approvals database
RUN mkdir -p /app/data && apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Expose port for SSE transport
EXPOSE 3001

# Default command runs the REST API server (includes /agent/chat, CORS, /health)
CMD ["garamatic-api"]

# Alternative: run the MCP server for external AI clients
# CMD ["garamatic-mcp"]
