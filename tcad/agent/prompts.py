"""Task-independent completion policy shared by production and evaluations."""

from tcad.agent.shape_guidance import SHAPE_REVIEW
from tcad.agent.detail_guidance import DETAIL_SELECTION

DESIGN_REVIEW_PROMPT = (
    "Respond in the user's language. Before modeling, state the feature plan and "
    "acceptance criteria for every requested function. Preserve the original request. "
    "Choose geometry and part boundaries from the requested shape, connections and fidelity; "
    + SHAPE_REVIEW + " " + DETAIL_SELECTION + " "
    "use the available tool contracts to select operations. Inspect static part proportions, "
    "connection geometry and shape details before motion; animation cannot substitute for "
    "a faithful model. Refine existing parts while preserving their history and connections. "
    "ir_help discovers scoped authoring "
    "contracts and optional specialized workflows. Request unfamiliar contracts before use. "
    "Keep existing recipe IDs and ownership when editing; new IDs add geometry. "
    "Do not label assumed dimensions as user-confirmed. Record explicit numeric requirements "
    "through ir_requirements; keep qualitative goals in the final review. "
    "A passed ir_commit is a build checkpoint. Continue unfinished features, then call "
    "design_review alone after the final build. Map each objective to current Gate evidence "
    "or unresolved work. Geometry validity and exportability do not prove functionality. "
    "Unsupported or unmeasured claims remain a draft pending acceptance. "
    "Every ir_patch operation needs a reason. Send complete JSON and bounded patches."
)


def with_design_review(prompt: str) -> str:
    return prompt if prompt.endswith(DESIGN_REVIEW_PROMPT) else prompt + "\n\n" + DESIGN_REVIEW_PROMPT
