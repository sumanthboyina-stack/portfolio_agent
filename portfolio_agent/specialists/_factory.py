"""Shared factory for all specialist LlmAgent instances."""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.agents import LlmAgent


@dataclass(frozen=True)
class SpecialistSpec:
    """Immutable descriptor for a specialist agent.

    Assign .make to a module-level name to expose a public factory:
        make_fundamentals_agent = _SPEC.make

    Call .make() to get the default singleton:
        fundamentals_agent = _SPEC.make()
    """
    name: str
    instruction: str
    tools: tuple
    output_key: str
    default_model: str

    def make(self, model: str | None = None) -> LlmAgent:
        return LlmAgent(
            name=self.name,
            model=model if model is not None else self.default_model,
            instruction=self.instruction,
            tools=list(self.tools),
            output_key=self.output_key,
        )
