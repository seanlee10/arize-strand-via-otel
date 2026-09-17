"""Run a small Strands agent and export its native spans to Arize AX."""

import argparse
import os
import sys

from strands import Agent, tool
from strands.models.anthropic import AnthropicModel

from instrumentation import load_config, setup_tracing


@tool
def get_weather(city: str) -> dict:
    """Return synthetic demo weather, not live weather observations.

    Args:
        city: City whose demo weather should be returned.
    """
    return {
        "status": "success",
        "content": [{"text": f"[샘플 데이터] {city}: 맑음, 기온 22°C."}],
    }


def create_agent(model) -> Agent:
    return Agent(
        name="WeatherAgent",
        model=model,
        tools=[get_weather],
        system_prompt=(
            "You are a helpful weather assistant. Always call get_weather for "
            "weather questions. Answer in Korean and clearly explain that the "
            "weather is synthetic demo data, not a live observation."
        ),
        callback_handler=None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="서울 날씨를 알려줘.")
    parser.add_argument("--space-id", help="Destination Space ID (overrides ARIZE_SPACE_ID)")
    parser.add_argument("--project-name", help="Arize project (overrides ARIZE_PROJECT_NAME)")
    args = parser.parse_args()
    try:
        load_config()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    provider = setup_tracing(space_id=args.space_id, project_name=args.project_name)
    try:
        model = AnthropicModel(
            model_id=os.getenv("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001",
            max_tokens=1024,
        )
        result = create_agent(model)(args.prompt)
        print(result)
    finally:
        try:
            if not provider.force_flush(timeout_millis=10000):
                print("트레이스 flush가 제한 시간 내 완료되지 않았습니다.", file=sys.stderr)
        finally:
            provider.shutdown()
    # A completed flush alone does not prove that Arize ingested the spans.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
