# The MCP Registry lists a package; Glama builds a container to score it, and a
# server without a working build is kept out of its search results — which in turn
# blocks the awesome-mcp-servers listing. So the build is ours, not inferred.
#
# Base install only: the semantic extra pulls a 220MB ONNX model, and retrieval
# degrades to BM25 without it rather than breaking. Build with
# `--build-arg EXTRAS=[semantic]` when you want the vector path in the image.
FROM python:3.12-slim

ARG EXTRAS=""

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY skillmem ./skillmem

RUN pip install --no-cache-dir ".${EXTRAS}"

# The database lives outside the image so a container restart keeps the memory.
ENV SKILLMEM_HOME=/data
VOLUME ["/data"]

# stdio transport: an MCP client talks to this process over stdin/stdout.
#
# Directly, not through a shim. A shim that drained stdin first shipped in
# 0.11.2 and was wrong twice over: its very first statement read stdin to EOF,
# which a normal client never sends, so the server was never started at all —
# and the scan it was written for never used this image, because the catalogue
# builds its own. Removed in 0.11.3.
ENTRYPOINT ["skillmem", "mcp"]
CMD []
