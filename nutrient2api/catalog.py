"""Model catalog.

Two names exist per model and they are not the same thing:

- ``id``       what callers ask for, and what appears in responses
- ``upstream`` what the gateway will actually accept on the wire

The gateway only routes its own ``nutrient-*`` aliases. The real upstream model
names are visible (they leak in error messages) but are rejected as request
models, so they are metadata only and never sent.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Model:
    id: str
    upstream: str
    real: str
    context_window: int
    max_output: int
    input_usd: float
    output_usd: float
    api: str = "chat"

    @property
    def modalities(self) -> Tuple[str, ...]:
        return ("text",) if self.api == "embeddings" else ("text", "image", "pdf")


CATALOG: Dict[str, Model] = {
    "gpt-6-luna": Model("gpt-6-luna", "nutrient-s", "gpt-6-luna", 1_050_000, 128_000, 0.12, 0.60),
    "gpt-6.1-sol": Model("gpt-6.1-sol", "nutrient-m", "gpt-6.1-sol", 1_050_000, 128_000, 2.40, 12.00),
    "gpt-6-astra": Model("gpt-6-astra", "nutrient-l", "gpt-6-astra", 1_050_000, 128_000, 12.00, 60.00),
    "text-embedding-3-small": Model(
        "text-embedding-3-small", "nutrient-embedding", "text-embedding-3-small", 8_192, 0, 0.024, 0.0, "embeddings"
    ),
}

ROUTES: Dict[str, str] = {
    # gateway aliases (also accepted so callers can pass through unchanged)
    "nutrient-s": "gpt-6-luna",
    "nutrient-m": "gpt-6.1-sol",
    "nutrient-l": "gpt-6-astra",
    "nutrient-embedding": "text-embedding-3-small",
    # canonical ids
    "gpt-6-luna": "gpt-6-luna",
    "gpt-6.1-sol": "gpt-6.1-sol",
    "gpt-6-astra": "gpt-6-astra",
    # convenience aliases
    "gpt-6": "gpt-6.1-sol",
    "gpt-6.1": "gpt-6.1-sol",
    "gpt-6-mini": "gpt-6-luna",
    "gpt-6-pro": "gpt-6-astra",
    "small": "gpt-6-luna",
    "medium": "gpt-6.1-sol",
    "large": "gpt-6-astra",
    # embedding aliases
    "text-embedding-3-small": "text-embedding-3-small",
    "text-embedding-ada-002": "text-embedding-3-small",
    "ada-002": "text-embedding-3-small",
}


def resolve(name: str) -> Optional[Model]:
    key = (name or "").strip().lower()
    target = ROUTES.get(key)
    return CATALOG.get(target) if target else None


def list_models() -> Tuple[Model, ...]:
    return tuple(CATALOG.values())