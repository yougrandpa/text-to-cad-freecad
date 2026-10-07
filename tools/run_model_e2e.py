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


def animation_deliveries(exports, artifact_id, data_dir):
    """Only readable, multi-frame media from the final artifact is delivered."""
    from PIL import Image

    delivered = []
    root = Path(data_dir).resolve()
    for export in exports:
        if export.get('artifact_id') != artifact_id or export.get('mode') not in ('motion', 'assemble', 'explode'):
            continue
        path = Path(export.get('path', '')).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != '.gif':
            continue
        try:
            with Image.open(path) as media:
                frames = media.n_frames
                for index in range(frames):
                    media.seek(index)
                    media.load()
            if frames > 1:
                delivered.append({**export, 'file_frames': frames})
        except (OSError, ValueError, EOFError):
            continue
    return delivered


def delivery_passed(summary, *, require_animation=False, required_mode=None):
    exports = summary.get('http_artifact_status') or {}
    delivered = (summary.get('current_build') and summary.get('state') in ('succeeded', 'draft')
                 and summary.get('http_scene_status') == 200 and bool(exports)
                 and all(code == 200 for code in exports.values()))
    if require_animation or required_mode:
        media = summary.get('http_animation_status') or {}
        delivered = (delivered and bool(summary.get('gif_paths')) and bool(media)
                     and all(code == 200 for code in media.values()))
    if required_mode:
        delivered = delivered and required_mode in summary.get('animation_modes', [])
    return bool(delivered)


async def run(args, *, settings_override=None, contract_check=None):
    from tcad.config.loader import load_default_config, resolve_paths
    from tcad.config.settings import effective
    from tcad.core.wiring import build_services, apply_llm_settings
    from tcad.ir.schema import IrDocument, RequirementSpec
    from tcad.inspect.artifact import ArtifactReader
    from tcad.server.app import ChatRequest, run_turn_request, create_app
    cfg=load_default_config(); resolve_paths(cfg)
    settings=settings_override or effective(cfg,cfg.storage.data_dir)
    secret=settings.llm.resolved_api_key()
    def redact(value):
        if isinstance(value,str): return value.replace(secret,'[REDACTED]') if secret else value
        if isinstance(value,list): return [redact(v) for v in value]
        if isinstance(value,dict): return {k:redact(v) for k,v in value.items()}
        return value
    request=args.request if args.request is not None else Path(args.request_file).read_text(encoding='utf-8')
    root=Path(args.output_dir or (Path(__file__).resolve().parents[1]/'output/local/tcad_e2e'/('live-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')))).resolve()
    root.mkdir(parents=True,exist_ok=False)
    cfg.storage.data_dir=str(root); cfg.storage.sqlite_path=''
    cfg.loop.max_steps_per_turn=args.max_steps; cfg.loop.turn_wall_clock_s=args.timeout
    resolve_paths(cfg)
    services=build_services(cfg); apply_llm_settings(services,settings,persist=False)
    services.store.create('e2e-model',IrDocument(model_id='e2e-model',requirements=RequirementSpec(raw_text=request)))
    start=time.monotonic(); calls=Counter(); failures=[]; animation_exports=[]
    def observer(kind,data):
        with (root/'events.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps(redact({'kind':kind,**data}),ensure_ascii=False)+'\n')
        if kind=='model':
            print('step',data['step'],'tools',[t['name'] for t in data.get('tool_calls',[])],flush=True)
        if kind=='tool':
            calls[data['name']]+=1
            if data['name']=='assembly_export' and data.get('ok'):
                try:
                    exported=json.loads(data.get('content') or '{}')
                    if isinstance(exported,dict): animation_exports.append(exported)
                except ValueError:
                    pass
            if not data.get('ok'):
                failure=redact({'step':data['step'],'tool':data['name'],**(data.get('error') or {})})
                failures.append(failure)
                print('  error:',failure.get('message','')[:500],flush=True)
    try:
        if contract_check is not None:
            contract_check(services)
        print('model:',settings.llm.resolved_model(),'output:',root,flush=True)
        result=await run_turn_request(services,ChatRequest(model_id='e2e-model',text=request),observer=observer)
        (root/'result.json').write_text(json.dumps(redact(result.model_dump(mode='json')),ensure_ascii=False,indent=2),encoding='utf-8')
        summary={'model':settings.llm.resolved_model(),'state':result.state.value,'steps':result.steps,
            'tokens_in':result.tokens_in,'tokens_out':result.tokens_out,'elapsed_s':round(time.monotonic()-start,2),
            'tool_calls':dict(calls),'tool_errors':failures,'build_passed':bool(result.gate_report and result.gate_report.passed),
            'final_ir_version':services.store.current_version('e2e-model'),'turn_error':redact(result.error)}
        reader=ArtifactReader(root)
        try:
            manifest,artifact_root=reader.resolve('e2e-model')
            scene=reader.scene(manifest,artifact_root)
            digest=reader.digest(manifest,artifact_root)
            built_ir=json.loads(reader.read_file(manifest,artifact_root,'ir.json'))
            assembly=built_ir.get('assembly') or {}
            summary.update({'artifact_id':manifest.artifact_id,'artifact_dir':str(artifact_root),
                'artifact_ir_version':manifest.ir_version,
                'current_build':bool(summary['build_passed'] and manifest.ir_version==summary['final_ir_version']
                    and result.gate_report.ir_version==manifest.ir_version),
                'measurements':{'is_valid':digest.is_valid,'volume':digest.volume,'solids':digest.topology.solids,
                                'bbox':digest.bbox.model_dump(mode='json')},
                'bodies':len(scene.body_ids),'animation_frames':len(scene.animation['frames']) if scene.animation else 0,
                'native_joint_count':len(assembly.get('joints') or []),'native_driver_count':len(assembly.get('drivers') or []),
                'solver':scene.animation.get('solver') if scene.animation else None,
                'max_swing_deg':scene.animation.get('max_swing_deg') if scene.animation else None,
                'animation_exports':animation_deliveries(animation_exports,manifest.artifact_id,root)})
            summary['gif_paths']=[item['path'] for item in summary['animation_exports']]
            summary['animation_modes']=sorted({item['mode'] for item in summary['animation_exports']})
            # Exercise the production HTTP delivery path without another LLM call.
            from fastapi.testclient import TestClient
            from tcad.server.app import artifact_url_for
            with TestClient(create_app(services,config=cfg)) as client:
                summary['http_scene_status']=client.get('/models/e2e-model/mesh').status_code
                summary['http_artifact_status']={name:client.get('/artifact-sets/'+manifest.artifact_id+'/files/'+name).status_code
                    for name in manifest.files if name.endswith(('.step','.stl','.FCStd'))}
                summary['http_animation_status']={path:client.get(artifact_url_for(path)).status_code
                    for path in summary['gif_paths']}
        except (FileNotFoundError,ValueError) as exc:
            summary['artifact_error']=redact(str(exc))
        delivered=delivery_passed(summary,require_animation=args.require_animation,
                                  required_mode=getattr(args,'require_animation_mode',None))
        summary['delivery_passed']=delivered
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
    parser.add_argument('--require-animation-mode',choices=('motion','assemble','explode'),
                        help='Require readable GIF media in this mode from the final build')
    args=parser.parse_args()
    if args.max_steps<1 or args.timeout<=0: parser.error('positive max-steps and timeout are required')
    return asyncio.run(run(args))


if __name__=='__main__': raise SystemExit(main())
