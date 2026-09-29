"""Thin wrapper around the model so the rest of the code (and the tests) only see JSON."""

import json
from typing import Protocol

from . import config


class JsonModel(Protocol):
    def json(self, system: str, user: str) -> dict: ...


class OpenAIModel:
    def __init__(self, model: str | None = None):
        from openai import OpenAI

        self.client = OpenAI(api_key=config.require("OPENAI_API_KEY"))
        self.model = model or config.OPENAI_MODEL

    def json(self, system: str, user: str) -> dict:
        response = self.client.responses.create(
            model=self.model,
            instructions=system,
            input=user + "\n\nRespond with a single JSON object only.",
            text={"format": {"type": "json_object"}},
        )
        text = response.output_text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model returned invalid JSON: {text[:300]}") from exc
