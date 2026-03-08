from __future__ import annotations

import json
from dataclasses import dataclass

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from langchain.chains import LLMChain
except ImportError:  # pragma: no cover
    LLMChain = None


@dataclass
class LangChainRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        if LLMChain is None:
            raise RuntimeError("LangChain not installed. Install hivepilot[langchain].")
        chain_path = self.definition.options.get("chain")
        if not chain_path:
            raise ValueError("LangChain runner requires 'chain' option pointing to a module path.")
        module_name, attr = chain_path.split(":")
        module = __import__(module_name, fromlist=[attr])
        chain_factory = getattr(module, attr)
        chain: LLMChain = chain_factory(payload)
        logger.info("langchain_runner.start", project=payload.project_name, step=payload.step.name)
        result = chain.run(json.dumps(payload.metadata))
        logger.info("langchain_runner.end", project=payload.project_name, output=result)
