# Strands Agent → OpenTelemetry Collector → Arize AX

**A Python sample that sends traces from a local AI agent to Arize AX through a Docker-based OpenTelemetry Collector.**

A Strands `WeatherAgent` powered by Anthropic Claude Haiku calls a weather tool. The client converts agent, model, and tool spans to OpenInference format and sends them to the Collector, which handles batching and authenticated export to Arize.

> The weather tool returns fixed sample data. It does not call a live weather API. Model responses use the Anthropic API.

## Architecture

```mermaid
flowchart LR
    subgraph Local["Local machine"]
        subgraph Python["Python client"]
            Agent["Strands WeatherAgent"]
            OI["OpenInference conversion"]
            Agent --> OI
        end
        subgraph Docker["Docker · OTel Collector"]
            Receiver["OTLP Receiver"] --> Processors["Memory Limiter + Batch"]
            Processors --> Exporter["Arize Exporter"]
        end
        OI -->|"OTLP HTTP · 127.0.0.1:4318"| Receiver
    end
    Agent <-->|"Model calls"| Claude["Anthropic Claude Haiku"]
    Exporter -->|"OTLP HTTPS + auth headers"| Arize["Arize AX"]
```

| Component | Responsibility |
| --- | --- |
| Strands Agent | Calls the model, executes tools, and generates native OpenTelemetry spans |
| OpenInference processor | Converts Strands spans to AGENT, CHAIN, LLM, and TOOL semantic conventions |
| Python OTLP exporter | Sends converted spans to the local Collector |
| OTel Collector | Receives spans, limits memory usage, batches, retries, and exports to Arize with authentication |
| Arize AX | Displays traces, parent-child relationships, inputs, outputs, and errors |

The client can run without an Arize API key. The Collector adds the `authorization` and `arize-space-id` headers. The client supplies the project name through the `openinference.project.name` resource attribute.

## Quick start

### 1. Prerequisites

- Python **3.11 or later** and `uv`
- A running Docker engine and Docker Compose
- An Anthropic API key
- An Arize AX API key and Space ID

The examples below use `docker-compose`. If your environment uses the Docker Compose plugin, replace it with `docker compose`.

### 2. Clone the repository and install dependencies

```sh
git clone https://github.com/seanlee10/arize-strand-via-otel.git
cd arize-strand-via-otel
uv sync --locked

# First-time setup: preserve an existing .env file.
cp -n .env.example .env
```

### 3. Configure API keys

Open `.env` and fill in these three values:

```dotenv
ANTHROPIC_API_KEY=your-anthropic-api-key
ARIZE_API_KEY=your-arize-api-key
ARIZE_SPACE_ID=your-arize-space-id
```

`ARIZE_SPACE_ID` must contain the **Space ID**, not the space name. Find it in Arize Space Settings. The `.env` file is excluded from Git.

The defaults are `claude-haiku-4-5-20251001` for the model, `strands-agent-sample` for the project, and the **US region** for Arize.

### 4. Start the Collector

```sh
# Validate environment variables and Collector configuration.
docker-compose config --quiet
docker-compose run --rm --no-deps otel-collector validate --config=/etc/otelcol-contrib/config.yaml

# Start in the background.
docker-compose up -d

# Check readiness. Retry after a short delay if the container just started.
curl --fail http://127.0.0.1:13133/
```

The health endpoint returns `"status":"Server available"` when the Collector is running.

### 5. Run the agent

```sh
uv run agent.py "What's the weather in Seoul?"
```

The agent calls `get_weather` and describes the sample weather for Seoul: **sunny, 22°C**. The current system prompt instructs the agent to answer in Korean; edit `system_prompt` in `agent.py` to change the response language. Exact wording may vary.

### 6. Verify traces in Arize

Sign in to [Arize AX](https://app.arize.com), select your configured Space, and open the `strands-agent-sample` project. Inspect the latest trace for:

- `invoke_agent WeatherAgent`: the request and final response
- `chat`: model calls and responses
- `execute_tool get_weather`: tool arguments and results
- `execute_event_loop_cycle`: agent processing steps

An end-to-end run through the Collector produced **1 AGENT, 2 CHAIN, 2 LLM, and 1 TOOL span**: **6 spans**, all with status `OK`. Inputs, outputs, and parent-child relationships were verified. Actual span counts can vary with tool calls and retries.

```sh
# Inspect received span counts and export errors.
docker-compose logs --tail=50 otel-collector
```

The health response shows Collector availability, and debug span counts show local receipt and pipeline processing. **Verify ingestion separately in Arize.**

## Environment variables

| Variable | Used by | Default / description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Python | Required: API key for model calls |
| `ANTHROPIC_MODEL` | Python | `claude-haiku-4-5-20251001` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Python | `http://127.0.0.1:4318/v1/traces` |
| `ARIZE_PROJECT_NAME` | Python | `strands-agent-sample` |
| `ARIZE_API_KEY` | Collector | Required: API key for Arize export |
| `ARIZE_SPACE_ID` | Collector | Required: destination Space ID |
| `ARIZE_COLLECTOR_ENDPOINT` | Collector | `https://otlp.arize.com/v1/traces` — US |

For convenience, the sample uses a single `.env` file. Python configuration validation requires only the Anthropic key. Compose passes only the three Arize exporter variables to the Collector container. Existing shell environment variables take precedence over `.env`.

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `ARIZE_COLLECTOR_ENDPOINT` configure separate hops. Both use a **full HTTP URL**, including `/v1/traces`.

## Project layout

```text
.
├── agent.py                # Haiku model, WeatherAgent, and get_weather tool
├── instrumentation.py      # OpenInference conversion and local OTLP export
├── compose.yaml            # Collector container, ports, and environment
├── otel-collector.yaml      # Receiver → Processors → Exporters pipeline
├── .env.example            # Environment variable template
├── pyproject.toml          # Python dependencies
├── uv.lock                 # Locked dependency versions
└── tests/test_tracing.py    # Local OTLP verification without external APIs
```

## Tracing implementation

**Client:** `StrandsAgentsToOpenInferenceProcessor` → `BatchSpanProcessor` → OTLP HTTP exporter. The converter mutates spans in place, so it is registered before the exporter. The global `TracerProvider` is configured before the agent is created.

**Collector:** OTLP receiver → `memory_limiter` → `batch` → Arize exporter. The debug exporter logs basic span counts. The Collector image is pinned to `otel/opentelemetry-collector-contrib:0.160.0`.

**Wire format:** The Arize HTTP exporter uses `compression: none`. In the tested environment, default gzip export was rejected with HTTP 400 (`cannot parse invalid wire-format data`). Uncompressed protobuf export succeeded.

**Shutdown:** Python calls `force_flush()` and `shutdown()` even when an error occurs. This completes export to the Collector; the Collector handles its own batching and retries separately.

## Operational commands

```sh
# Check container status.
docker-compose ps

# Follow logs.
docker-compose logs -f otel-collector

# Recreate after changing Collector configuration or Arize credentials.
docker-compose up -d --force-recreate

# Stop and remove the container and Compose network.
docker-compose down
```

| Host address | Purpose |
| --- | --- |
| `127.0.0.1:4318` | OTLP HTTP — used by the Python sample |
| `127.0.0.1:4317` | OTLP gRPC — available for other clients |
| `127.0.0.1:13133` | Collector health endpoint |

Host ports are exposed only on localhost. If you move the client into a container on the same Compose network, set its export URL to `http://otel-collector:4318/v1/traces`.

This configuration is intended for local experimentation. The Collector uses an in-memory queue and retries for up to 60 seconds. No persistent queue is configured, so forced shutdowns or extended export failures can cause queued data to be lost.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

Tests use a fake model and a local HTTP receiver. No API keys, running Collector, or external API calls are required. They execute a real Strands agent loop and check:

- AGENT, LLM, and TOOL spans with inputs and outputs after OpenInference conversion
- A shared trace ID and valid parent-child relationships
- The OTLP path and project resource attribute
- Use of the local endpoint without Arize authentication headers on client requests

Verify export through the Docker Collector to Arize separately using the quick-start steps above.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `docker: unknown command: docker compose` | Use the `docker-compose` command. |
| `connection refused` | Check Docker and Collector status, port 4318, and the client endpoint. |
| Port already in use | Check for conflicts on ports 4317, 4318, and 13133. If you change the host HTTP port, update the client endpoint too. |
| Arize export returns 401/403 | Check the Collector's API key, Space ID, and access to that Space. |
| HTTP 400 / `invalid wire-format data` | Check `compression: none` and the full URL ending in `/v1/traces`. |
| Model API quota or credit error | Check the Anthropic API key, available credits, and usage limits. Execution may fail before the tool runs. |
| Collector is running but Arize shows no traces | Check Collector export errors, the Arize Space, project, region, and the UI time range. |
| Changes to `.env` do not take effect | Check for overriding shell environment variables. Recreate the container after changing Collector settings. |

## References

- [Arize AX: Strands integration](https://arize.com/docs/ax/integrations/python-agent-frameworks/aws-strands/aws-strands-tracing)
- [OpenTelemetry Collector configuration](https://opentelemetry.io/docs/collector/configuration/)
- [OTLP HTTP exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)
- [Anthropic models](https://platform.claude.com/docs/en/models/overview)
