"""Component DAG: each node is a whole sequential PartDesign document."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class BuildNode(BaseModel):
    id: str
    digest: str
    dependencies: list[str] = Field(default_factory=list)
    document_sha256: str | None = None
    source_artifact: str | None = None


class BuildGraph(BaseModel):
    nodes: list[BuildNode]

    @model_validator(mode="after")
    def acyclic(self):
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("duplicate build node")
        visited, visiting = set(), set()
        def visit(name):
            if name in visiting:
                raise ValueError("cyclic build graph")
            if name in visited:
                return
            if name not in nodes:
                raise ValueError("missing build dependency")
            visiting.add(name)
            for dependency in nodes[name].dependencies:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
        for name in nodes:
            visit(name)
        return self
