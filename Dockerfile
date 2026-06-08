# Garamatic Agentic Service - MCP Server
# Multi-stage build for smaller image size

# Stage 1: Build / Install dependencies
FROM python:3.12-slim AS builder
WORKDIR /app

# Install uv via pip
RUN pip install --no-cache-dir uv

# Copy dependency file and source
COPY pyproject.toml .
COPY README.md .
COPY src/ src/

# Install dependencies into a virtual environment (isolates uv itself from the runtime)
RUN uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# Stage 2: Runtime — minimal image
FROM python:3.12-slim
WORKDIR /app

# Copy the virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Ensure data directory exists for the approvals database
RUN mkdir -p /app/data

# Expose port for SSE transport
EXPOSE 3001

# Default command runs the MCP server for external AI clients
CMD ["garamatic-mcp"]

# Alternative: run the REST API server (includes /agent/chat, CORS, /health)
# CMD ["garamatic-api"]
