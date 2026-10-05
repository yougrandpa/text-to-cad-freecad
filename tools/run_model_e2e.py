#!/usr/bin/env python3
"""Run a real, saved-provider CAD turn in an isolated data directory.

Example:
    .venv/bin/python tools/run_model_e2e.py --request '生成一个带孔底座' --max-steps 35

This invokes the actual configured external model (and may incur provider cost).
Credentials are read in memory from current settings and never copied to output.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


async def run(args):
    from tcad.config.loader import load_default_config, resolve_paths
    from tcad.config.settings import effective
    from tcad.core.wiring import build_services, apply_llm_settings
    from tcad.ir.schema import IrDocument, RequirementSpec
    from tcad.inspect.artifact import ArtifactReader
    from tcad.server.app import ChatRequest, run_turn_request, create_app
    cfg=load_default_config(); resolve_paths(cfg)
    settings=effective(cfg,cfg.storage.data_dir)
    secret=settings.llm.resolved_api_key()
    def redact(value):
        if isinstance(value,str): return value.replace(secret,'[REDACTED]') if secret else value
        if isinstance(value,list): return [redact(v) for v in value]
        if isinstance(value,dict): return {k:redact(v) for k,v in value.items()}
        return value
    request=args.request if args.request is not None else Path(args.request_file).read_text(encoding='utf-8')
    root=Path(args.output_dir or ('.tcad_e2e/live-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))).resolve()
    root.mkdir(parents=True,exist_ok=False)
    cfg.storage.data_dir=str(root); cfg.storage.sqlite_path=''
    cfg.loop.max_steps_per_turn=args.max_steps; cfg.loop.turn_wall_clock_s=args.timeout
    resolve_paths(cfg)
    services=build_services(cfg); apply_llm_settings(services,settings,persist=False)
    services.store.create('e2e-model',IrDocument(model_id='e2e-model',requirements=RequirementSpec(raw_text=request)))
    start=time.monotonic(); calls=Counter(); failures=[]
    def observer(kind,data):
        with (root/'events.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps(redact({'kind':kind,**data}),ensure_ascii=False)+'\n')
        if kind=='model':
            print('step',data['step'],'tools',[t['name'] for t in data.get('tool_calls',[])],flush=True)
        if kind=='tool':
            calls[data['name']]+=1
            if not data.get('ok'):
                failure=redact({'step':data['step'],'tool':data['name'],**(data.get('error') or {})})
                failures.append(failure)
                print('  error:',failure.get('message','')[:500],flush=True)
    try:
        print('model:',settings.llm.resolved_model(),'output:',root,flush=True)
        result=await run_turn_request(services,ChatRequest(model_id='e2e-model',text=request),observer=observer)
        (root/'result.json').write_text(json.dumps(redact(result.model_dump(mode='json')),ensure_ascii=False,indent=2),encoding='utf-8')
        summary={'model':settings.llm.resolved_model(),'state':result.state.value,'steps':result.steps,
            'tokens_in':result.tokens_in,'tokens_out':result.tokens_out,'elapsed_s':round(time.monotonic()-start,2),
            'tool_calls':dict(calls),'tool_errors':failures,'build_passed':bool(result.gate_report and result.gate_report.passed)}
        reader=ArtifactReader(root)
        try:
            manifest,artifact_root=reader.resolve('e2e-model')
            scene=reader.scene(manifest,artifact_root)
            summary.update({'artifact_id':manifest.artifact_id,'artifact_dir':str(artifact_root),
                'bodies':len(scene.body_ids),'animation_frames':len(scene.animation['frames']) if scene.animation else 0,
                'solver':scene.animation.get('solver') if scene.animation else None,
                'max_swing_deg':scene.animation.get('max_swing_deg') if scene.animation else None,
                'gif_paths':[str(p) for p in root.glob('derived/animations/**/*.gif')]})
            # Exercise the production HTTP delivery path without another LLM call.
            from fastapi.testclient import TestClient
            with TestClient(create_app(services,config=cfg)) as client:
                summary['http_scene_status']=client.get('/models/e2e-model/mesh').status_code
                summary['http_artifact_status']={name:client.get('/artifact-sets/'+manifest.artifact_id+'/files/'+name).status_code
                    for name in manifest.files if name.endswith(('.step','.stl','.FCStd'))}
        except (FileNotFoundError,ValueError) as exc:
            summary['artifact_error']=redact(str(exc))
        delivered=(summary['build_passed'] and result.state.value in ('succeeded','draft')
                   and summary.get('http_scene_status')==200 and bool(summary.get('http_artifact_status'))
                   and all(code==200 for code in summary.get('http_artifact_status',{}).values()))
        if args.require_animation:
            delivered=delivered and summary.get('animation_frames',0)>1 and bool(summary.get('gif_paths'))
        summary['delivery_passed']=bool(delivered)
        (root/'summary.json').write_text(json.dumps(redact(summary),ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(redact(summary),ensure_ascii=False,indent=2),flush=True)
        return 0 if delivered else 1
    finally:
        services._worker_handle.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    request=parser.add_mutually_exclusive_group(required=True)
    request.add_argument('--request'); request.add_argument('--request-file')
    parser.add_argument('--output-dir',help='New isolated directory; must not already exist')
    parser.add_argument('--max-steps',type=int,default=60)
    parser.add_argument('--timeout',type=float,default=1200)
    parser.add_argument('--require-animation',action='store_true')
    args=parser.parse_args()
    if args.max_steps<1 or args.timeout<=0: parser.error('positive max-steps and timeout are required')
    return asyncio.run(run(args))


if __name__=='__main__': raise SystemExit(main())
