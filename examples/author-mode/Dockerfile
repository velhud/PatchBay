# Build explicitly before registering a Docker check profile. The runtime never pulls images.
FROM python:3.11-slim
RUN apt-get update && apt-get install --no-install-recommends -y git && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir pytest==9.0.3
WORKDIR /workspace
# Add project dependencies to this image explicitly. Verification runs without network.
