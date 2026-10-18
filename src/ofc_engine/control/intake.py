from __future__ import annotations

from copy import deepcopy

from pydantic import Field, model_validator

from ..copilot_llm import CopilotDraft, DraftChoice


class GraphChoice(DraftChoice):
    candidate_names: tuple[str, ...] = Field(min_length=1, max_length=8)


class GraphDraft(CopilotDraft):
    choices: tuple[GraphChoice, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def causal_graph(self):
        incoming = {choice.decision_id: len(choice.depends_on) for choice in self.choices}
        children = {key: [] for key in incoming}
        for choice in self.choices:
            for parent in choice.depends_on:
                children[parent].append(choice.decision_id)
        ready = [key for key, count in incoming.items() if count == 0]
        visited = 0
        while ready:
            key = ready.pop(); visited += 1
            for child in children[key]:
                incoming[child] -= 1
                if incoming[child] == 0:
                    ready.append(child)
        if visited != len(incoming):
            raise ValueError("cyclic proposed plan")
        return self


def graph_schema(base):
    schema = deepcopy(base)
    choices = schema["properties"]["choices"]
    choices["maxItems"] = 64
    choices["items"]["properties"]["candidate_names"].update(minItems=1, maxItems=8)
    return schema
