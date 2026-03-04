#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Check for uv
if ! command -v uv &>/dev/null; then
    echo "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Create venv and install deps
echo "Creating virtual environment..."
uv venv
echo "Installing dependencies..."
uv pip install -r requirements.txt

# Copy example configs if missing
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    echo "Created config.yaml — edit it with your Signal bot number and group IDs."
else
    echo "config.yaml already exists, skipping."
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — add your API key."
else
    echo ".env already exists, skipping."
fi

# Ensure data dir exists
mkdir -p data

echo ""
echo "Setup complete! Next steps:"
echo "  1. Edit config.yaml with your Signal bot number and group IDs"
echo "  2. Edit .env with your API key"
echo "  3. Run: source .venv/bin/activate && python main.py"
