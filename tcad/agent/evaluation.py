"""Freeze the model-facing contract and grade artifact measurements independently."""
from __future__ import annotations

import hashlib
import inspect
import json
import math


class FrozenContractError(ValueError):
    """A changed evaluation boundary invalidates the run rather than the model."""


def fingerprint(value) -> str:
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def contract_snapshot(services) -> dict:
    from tcad.agent.workflows import WORKFLOWS
    from tcad.core.types import TurnKind
    from tcad.loop.budget import BudgetLimits
    from tcad.loop.engine import LoopEngine
    from tcad.tools.authoring import help_handler
    from tcad.tools.base import build_default_registry
    registry=build_default_registry(services,enable_privileged=bool(services.config.policy.allow_privileged))
    engine=LoopEngine(services,registry,BudgetLimits(),services.loop_config)
    tools={kind.value:registry.as_openai_tools(kind) for kind in TurnKind}
    initial={kind:engine._authoring_surface(definitions) for kind,definitions in tools.items()}
    payload={'schema_version':1,'system_prompt':engine.config.system_prompt,
             'tools_sha256':fingerprint(tools),'initial_tools_sha256':fingerprint(initial),
             'help_sha256':fingerprint({'handler':inspect.getsource(help_handler),'workflows':WORKFLOWS}),
             'discovery_sha256':fingerprint(inspect.getsource(LoopEngine._authoring_surface))}
    return {**payload,'contract_sha256':fingerprint(payload)}


def require_frozen_contract(services,baseline: dict) -> None:
    if contract_snapshot(services) != baseline:
        raise FrozenContractError('Model-facing contract changed; create a new baseline instead of mixing evaluation versions.')


def assess_case(case: dict,summary: dict) -> dict:
    """Automated checks are scoped evidence, not a semantic-completion verdict."""
    checks={'delivery':bool(summary.get('delivery_passed')),
            'current_artifact':bool(summary.get('current_build')),
            'valid_geometry':summary.get('measurements',{}).get('is_valid') is True}
    expected=case.get('automated',{})
    measured=summary.get('measurements',{})
    if 'solids' in expected:
        checks['solid_count']=measured.get('solids') == expected['solids']
    if 'bodies' in expected:
        checks['body_count']=summary.get('bodies') == expected['bodies']
    if 'min_bodies' in expected:
        checks['body_count']=summary.get('bodies',0) >= expected['min_bodies']
    if 'volume' in expected:
        value=measured.get('volume')
        checks['volume']=isinstance(value,(int,float)) and math.isfinite(value) and math.isclose(
            value,expected['volume'],rel_tol=expected.get('volume_rel_tol',1e-5),abs_tol=1e-6)
    if expected.get('motion'):
        checks['saved_motion']=summary.get('animation_frames',0)>1
    if expected.get('native_motion'):
        checks['native_motion']=bool(summary.get('native_joint_count') and summary.get('native_driver_count'))
    return {'id':case['id'],'role':case['role'],'automated_checks':checks,
            'automated_passed':all(checks.values()),'task_verified':False,
            'manual_review_required':case.get('manual',[])}
