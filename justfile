set dotenv-load := true

bin := "freecad-collab-server"

default: check

# Format, lint, test
check: fmt-check lint test

fmt:
    cargo fmt --all

fmt-check:
    cargo fmt --all -- --check

lint:
    cargo clippy --all-targets -- -D warnings

test:
    cargo test

build:
    cargo build --release

run:
    cargo run -- serve

migrate:
    cargo run -- migrate

# Create a user and print their API token
user name:
    cargo run --quiet -- create-user {{name}}

# Issue another token for an existing user
token name label="cli":
    cargo run --quiet -- issue-token {{name}} {{label}}

schema:
    cargo run --quiet -- print-schema > protocol.schema.json

docker-build tag="freecad-collab-server:latest":
    docker build -t {{tag}} .

docker-run tag="freecad-collab-server:latest":
    docker run --rm -p 14008:14008 -v collab-data:/data --env-file .env {{tag}}

# End-to-end REST smoke test against a running server
smoke base="http://127.0.0.1:14008":
    ./scripts/smoke.sh {{base}}

# Symlink the FreeCAD plugin into FreeCAD's Mod directory
install-plugin mod="":
    ./scripts/install-plugin.sh {{mod}}

# End-to-end test of the plugin's sync core against a real server
plugin-test:
    cargo build
    python3 plugin/tests/test_sync.py

# Build a release bundle: just dist linux | just dist windows
dist platform="linux":
    ./scripts/dist.sh {{platform}}

# Install the server as a system service from this source tree (Linux)
install-server *args:
    cargo build --release
    sudo ./installer/linux/install.sh server {{args}}

# End-to-end test of the AI agent (fake model APIs)
agent-test:
    cargo build
    python3 agent/tests/test_agent.py

# Run the AI agent from source: just agent agent.env
agent env_file="agent/agent.env":
    ./agent/collab-agent --env-file {{env_file}}
