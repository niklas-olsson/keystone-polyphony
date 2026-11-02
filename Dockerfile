# Use a lightweight Python base image
FROM python:3.12-slim

# Install Node.js, GitHub CLI (gh), sudo, and other dependencies
RUN apt-get update && apt-get install -y nodejs npm git curl sudo && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user 'developer' with UID 1000
RUN groupadd --gid 1000 developer && \
    useradd --uid 1000 --gid 1000 -m developer && \
    echo "developer ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/developer && \
    chmod 0440 /etc/sudoers.d/developer

# Install GitHub CLI (gh) from official release
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Note: hyperswarm-python is NOT used directly; we use the Node sidecar.

# Install Node.js dependencies for the sidecar
COPY src/liminal_bridge/sidecar/package.json ./src/liminal_bridge/sidecar/
RUN cd src/liminal_bridge/sidecar && npm install

# Copy the rest of the application
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default command: Run the MCP server
CMD ["python", "-m", "src.liminal_bridge.server"]
