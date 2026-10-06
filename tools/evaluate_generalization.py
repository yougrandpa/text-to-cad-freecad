#!/usr/bin/env python3
"""Freeze the production authoring contract, then evaluate isolated real-model turns.

--freeze performs no provider calls. --run uses the explicitly configured provider.
Tasks and independent acceptance checks never enter the system prompt.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace

REPO_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO_ROOT))

from tcad.agent.evaluation import FrozenContractError, assess_case, contract_snapshot, fingerprint, require_frozen_contract

DEFAULT_CASES=REPO_ROOT/'review/generalization/cases.json'
DEFAULT_BASELINE=REPO_ROOT/'review/generalization/baseline.json'


def load_cases(path: Path) -> dict:
    data=json.loads(path.read_text(encoding='utf-8'))
    cases=data.get('cases',[])
    ids=[]
    for case in cases:
        id=case.get('id','')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',id) or id in ids:
            raise ValueError('Evaluation case IDs must be unique safe directory names.')
        if case.get('role') not in {'holdout','diagnostic'} or not case.get('request','').strip():
            raise ValueError('Each case needs a role and a non-empty request.')
        if not case.get('manual'):
            raise ValueError('Each case must declare the semantic checks requiring independent review.')
        ids.append(id)
    if not cases or not any(case['role']=='holdout' for case in cases):
        raise ValueError('The evaluation must contain held-out requests.')
    return data


@contextmanager
def configured_services():
    from tcad.config.loader import load_default_config, resolve_paths
    from tcad.config.settings import effective
    from tcad.core.wiring import apply_llm_settings, build_services
    cfg=load_default_config(); resolve_paths(cfg)
    settings=effective(cfg,cfg.storage.data_dir)
    with tempfile.TemporaryDirectory(prefix='tcad-eval-contract-') as tmp:
        cfg.storage.data_dir=tmp; cfg.storage.sqlite_path=''
        services=build_services(cfg,start_worker=False)
        try:
            apply_llm_settings(services,settings,persist=False)
            if not services.loop_config.require_design_review:
                raise ValueError('Evaluation requires the production design-review mode.')
            yield services,settings
        finally:
            services._worker_handle.close()


def runner_fingerprint() -> str:
    paths=[Path(__file__),REPO_ROOT/'tools/run_model_e2e.py',REPO_ROOT/'tcad/agent/evaluation.py']
    return fingerprint({str(path.relative_to(REPO_ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})


def freeze(path: Path,cases: dict,*,max_steps: int=40,timeout: float=600) -> dict:
    with configured_services() as (services,_):
        baseline={'version':1,'contract':contract_snapshot(services),'cases_sha256':fingerprint(cases),
                  'runner_sha256':runner_fingerprint(),'limits':{'max_steps':max_steps,'timeout':timeout}}
    baseline['baseline_sha256']=fingerprint(baseline)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as output:
        json.dump(baseline,output,ensure_ascii=False,indent=2); output.write('\n')
    return baseline


def validate_baseline(baseline: dict,cases: dict,services) -> None:
    if baseline.get('baseline_sha256') != fingerprint({k:v for k,v in baseline.items() if k != 'baseline_sha256'}):
        raise FrozenContractError('Frozen baseline was modified; create a new version.')
    if baseline.get('version') != 1 or baseline.get('cases_sha256') != fingerprint(cases):
        raise FrozenContractError('Evaluation tasks changed; create a new baseline.')
    if baseline.get('runner_sha256') != runner_fingerprint():
        raise FrozenContractError('Evaluation runner changed; create a new baseline.')
    require_frozen_contract(services,baseline['contract'])


def public_settings(settings) -> dict:
    llm=settings.llm
    return {'provider':llm.provider,'model':llm.resolved_model(),'temperature':llm.temperature,
            'max_tokens_per_step':llm.max_tokens_per_step,'context_window':llm.resolved_context_window(),
            'request_timeout_s':llm.request_timeout_s,'max_retries':llm.max_retries,
            'endpoint_sha256':fingerprint(llm.resolved_base_url()),'supports_vision':llm.resolved_supports_vision()}


async def evaluate(args,cases: dict,baseline: dict) -> int:
    from tools.run_model_e2e import run
    selected=set(args.case or [case['id'] for case in cases['cases']])
    if selected - {case['id'] for case in cases['cases']}:
        raise ValueError('Unknown evaluation case requested.')
    with configured_services() as (services,settings):
        validate_baseline(baseline,cases,services)
        if settings.llm.needs_key() and not settings.llm.resolved_api_key():
            raise ValueError('The selected provider requires explicitly configured credentials.')
    settings=settings.model_copy(deep=True)
    root=Path(args.output_dir or (REPO_ROOT/'.tcad_eval'/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))).resolve()
    root.mkdir(parents=True,exist_ok=False)
    (root/'baseline.json').write_text(json.dumps(baseline,ensure_ascii=False,indent=2),encoding='utf-8')
    report={'contract_sha256':baseline['contract']['contract_sha256'],'cases_sha256':baseline['cases_sha256'],
            'settings':public_settings(settings),'limits':baseline['limits'],'cases':[]}
    for case in cases['cases']:
        if case['id'] not in selected: continue
        print('case:',case['id'],'role:',case['role'],flush=True)
        run_args=SimpleNamespace(request=case['request'],request_file=None,output_dir=str(root/case['id']),
                                 max_steps=baseline['limits']['max_steps'],timeout=baseline['limits']['timeout'],
                                 require_animation=False)
        try:
            await run(run_args,settings_override=settings,
                      contract_check=lambda svc:validate_baseline(baseline,cases,svc))
            summary=json.loads((root/case['id']/'summary.json').read_text(encoding='utf-8'))
            assessment=assess_case(case,summary)
            assessment.update({'state':summary['state'],'steps':summary['steps'],
                               'tokens_in':summary['tokens_in'],'tokens_out':summary['tokens_out'],
                               'elapsed_s':summary['elapsed_s'],'tool_errors':len(summary['tool_errors']),
                               'artifact_dir':summary.get('artifact_dir'),'turn_error':summary.get('turn_error')})
        except FrozenContractError:
            raise
        except (OSError,ValueError,RuntimeError) as exc:
            if runner_fingerprint() != baseline['runner_sha256']:
                raise ValueError('Evaluation code changed during the run; results cannot be pooled.') from exc
            secret=settings.llm.resolved_api_key()
            message=str(exc).replace(secret,'[REDACTED]') if secret else str(exc)
            assessment={**assess_case(case,{}),'run_error':message}
        report['cases'].append(assessment)
        heldout=[row for row in report['cases'] if row['role']=='holdout']
        report['holdout_automated_passed']=sum(row['automated_passed'] for row in heldout)
        report['holdout_evaluated']=len(heldout)
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('report:',root/'report.json',flush=True)
    return 0 if all(row['automated_passed'] for row in report['cases']) else 1


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--freeze',action='store_true'); action.add_argument('--check',action='store_true')
    action.add_argument('--run',action='store_true')
    parser.add_argument('--baseline',type=Path,default=DEFAULT_BASELINE)
    parser.add_argument('--cases',type=Path,default=DEFAULT_CASES)
    parser.add_argument('--case',action='append'); parser.add_argument('--output-dir')
    parser.add_argument('--max-steps',type=int); parser.add_argument('--timeout',type=float)
    args=parser.parse_args(argv)
    if (args.max_steps is not None and args.max_steps<1) or (args.timeout is not None and args.timeout<=0):
        parser.error('Positive evaluation limits are required.')
    cases=load_cases(args.cases)
    if args.freeze:
        baseline=freeze(args.baseline,cases,max_steps=args.max_steps or 40,timeout=args.timeout or 600)
        print('frozen contract:',baseline['contract']['contract_sha256'])
        return 0
    baseline=json.loads(args.baseline.read_text(encoding='utf-8'))
    if any(value is not None and value != baseline['limits'][name]
           for name,value in [('max_steps',args.max_steps),('timeout',args.timeout)]):
        parser.error('Limits differ from the frozen baseline.')
    if args.check:
        with configured_services() as (services,_): validate_baseline(baseline,cases,services)
        print('frozen contract matches:',baseline['contract']['contract_sha256'])
        return 0
    return asyncio.run(evaluate(args,cases,baseline))


if __name__=='__main__': raise SystemExit(main())
