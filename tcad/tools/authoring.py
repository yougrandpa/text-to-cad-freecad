"""Model-facing recipes: small structured arguments, ordinary IR underneath."""
import copy
import functools
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tcad.core.types import ToolSpec, ToolTier, ToolResult, ToolError, ToolErrorKind
from tcad.ir.builders import AUTO_REASON, BuildParts, parts_patch, resolve_attachments
from tcad.ir.rotary import RotaryRig
from tcad.ir.schema import ConstraintExpr
from tcad.loop.intent import PlanCall
from tcad.agent.shape_guidance import SHAPE_CHOICES, SHAPE_REVIEW, shape_example


def inline_schema(schema):
    root=copy.deepcopy(schema)
    def expand(node):
        if isinstance(node,list): return [expand(v) for v in node]
        if not isinstance(node,dict): return node
        if '$ref' in node:
            target=root
            for name in node['$ref'].split('/')[1:]: target=target[name]
            return expand(target)
        expanded={k:expand(v) for k,v in node.items() if k not in ('$defs','title')}
        if 'base_version' in expanded.get('properties',{}):
            expanded['properties']['base_version']={'anyOf':[{'type':'integer'},
                {'type':'string','enum':['current']},{'type':'string','pattern':'^[0-9]+$'}],
                'description':'Optional; defaults to "current" (the version at apply time — no read-back needed). Pass the integer you last read to detect stale edits; an explicit stale version is rejected.'}
        return expanded
    return expand(root)


def error(exc, hint):
    message = '; '.join(f"{'.'.join(map(str,e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:3]) if isinstance(exc,ValidationError) else str(exc)
    return ToolResult(ok=False,error=ToolError(kind=ToolErrorKind.SCHEMA,message=message,hint=hint))


class RequirementsCall(BaseModel):
    model_config=ConfigDict(extra='forbid')
    constraints: list[ConstraintExpr] = Field(min_length=1,max_length=40)
    reason: str = Field(default=AUTO_REASON)
    base_version: int | Literal['current'] = 'current'


class RotationCall(RotaryRig):
    duration_s: float = Field(default=12,ge=0.1,le=60)
    frames: int = Field(default=121,ge=2,le=600)
    reason: str = Field(default=AUTO_REASON)
    base_version: int | Literal['current'] = 'current'
    hanging_body_ids: list[str] = Field(default_factory=list,max_length=32)
    damping_per_s: float = Field(default=0.5,ge=0,le=100)


def _check_recipe_identity(part, old):
    """Check composite recipe ownership before converting generated operations."""
    rotor_ids = [part.id+'_hub'] + [f'{part.id}_blade_{i}' for i in range(8)]
    rotor_members = [old[id] for id in rotor_ids if id in old]
    root = old.get(part.id)
    if part.id+'_hub' in old and len(rotor_members) > 1:
        if part.shape != 'rotor' or any(body != part.body_id for body,_ in rotor_members):
            raise ValueError(f'recipe ID {part.id} already belongs to another body/shape; use a new ID')
    if root and root[1].op in {'additive_loft', 'subtractive_loft'}:
        if part.shape != 'loft' or root[0] != part.body_id:
            raise ValueError(f'recipe ID {part.id} already belongs to another body/shape; use a new ID')
    elif part.shape in {'loft', 'rotor'}:
        primitive_ids = [part.id]
        aliased = old.get(part.id+'_shape')
        if aliased and aliased[0] == part.id:
            primitive_ids.append(part.id+'_shape')
        if part.id+'_0' in old and part.id+'_1' in old:
            primitive_ids += [f'{part.id}_{i}' for i in range(32)]
        if any(id in old for id in primitive_ids):
            raise ValueError(f'recipe ID {part.id} already belongs to another body/shape; use a new ID')


def _recipe_family_ids(part):
    """Every IR id this recipe may own or rewrite (its stable-ID surface)."""
    ids = {part.id, part.id + '_shape'}
    if part.shape == 'loft':
        ids |= {f'{part.id}_plane_{i}' for i in range(32)}
        ids |= {f'{part.id}_section_{i}' for i in range(32)}
    elif part.shape == 'rotor':
        ids |= {part.id + '_hub', part.id + '_bore'}
        ids |= {f'{part.id}_blade_{i}' for i in range(32)}
    else:
        ids |= {part.id + '_bore'}
        for i in range(32):
            ids |= {f'{part.id}_{i}', f'{part.id}_{i}_shape', f'{part.id}_{i}_bore'}
    return ids


def _assembly_references(ir, body_id):
    """Whether the current assembly declaration names this body (grounded, joint or rig)."""
    assembly = getattr(ir, 'assembly', None)
    if assembly is None:
        return False
    if body_id in (assembly.grounded or []):
        return True
    rotation = getattr(assembly, 'rotation', None)
    if rotation is not None:
        if body_id in rotation.rotating_body_ids:
            return True
        if any(body_id in suspension.body_ids for suspension in rotation.suspensions):
            return True
    return any(body_id in (joint.side1.body_id, joint.side2.body_id) for joint in assembly.joints)


def _check_body_rules(part, ir, old):
    """One uniform rule for every compact recipe call.

    * a body that does not exist is created;
    * an existing EMPTY body is filled;
    * a recipe this tool generated is updated through its stable IDs;
    * a cut or explicit extend_existing appends a refinement to local history;
    * other new additions to a populated body are refused by name.

    The refusal is the point: fusing new material into hand-made or
    third-party geometry silently rewrites someone's custom modification, and
    "the recipe tool touched my part" is much worse than one clear error.
    """
    if _recipe_family_ids(part) & set(old):
        return  # stable-ID update of this tool's own recipe (identity checked above)
    body = next((b for b in ir.bodies if b.id == part.body_id), None)
    if body is None:
        return  # create path
    if body.part_ref is not None:
        raise ValueError(
            f'body {body.id} is a referenced component; recipes cannot add local '
            f'features to it. Build into a new body_id or edit the source model.')
    if getattr(part, 'operation', 'add') == 'cut' or getattr(part, 'extend_existing', False):
        return  # explicit refinement; append history without replacing geometry
    if body.sketches or body.features:
        # The refusal is correct — but when the body is also part of the current
        # assembly, the model's next move ("rebuild it") is itself refused by the
        # assembly validation. Name the escape path here instead of letting three
        # tools' errors form a closed loop (observed live: four wasted rounds).
        assembly_note = (
            ' This body is referenced by the current assembly declaration; '
            'to rebuild or replace it, clear the assembly first '
            '(ir_patch set_assembly assembly=null), rebuild, then reconfigure it.'
            if _assembly_references(ir, body.id) else '')
        raise ValueError(
            f'body {body.id} already contains geometry; cad_build_parts only creates '
            f'new bodies, fills empty ones, or updates its own recipes by stable ID by default. '
            f'Resend the existing recipe id to edit it, use a NEW body_id for new '
            f'components, set extend_existing=true to append connected material, '
            f'or operation=cut to cut existing material. Use ir_patch for custom '
            f'operations.' + assembly_note)


async def build_parts_handler(services,args,ctx):
    from tcad.tools.ir_tools import ir_patch_handler
    try:
        request=BuildParts.model_validate(args)
        ir=services.store.load(ctx.model_id)
        # Relations resolve once, over the WHOLE call: an attach target must be
        # an earlier part in the same batch, and its declared numbers are what
        # the anchor arithmetic reads. The per-part passes below then see
        # ordinary coordinates.
        parts=resolve_attachments(request.parts)
        old={f.id:(b.id,f) for b in ir.bodies for f in b.features}
        old_sketches={s.id:(b.id,s) for b in ir.bodies for s in b.sketches}
        ops=[]; created=[]; bodies=[b.id for b in ir.bodies]
        identities={}
        for part in parts:
            identity=(part.body_id,part.shape)
            if part.id in identities and identities[part.id] != identity:
                raise ValueError(f'recipe ID {part.id} cannot have different bodies/shapes in one request')
            identities[part.id]=identity
            _check_recipe_identity(part,old)
            _check_body_rules(part,ir,old)
            group,new_bodies=parts_patch(request.model_copy(update={'parts':[part]}),bodies)
            bodies+=new_bodies; created+=new_bodies
            generated={op['payload']['id'] for op in group if op['op']=='add_feature'}
            anchors={}
            for body in ir.bodies:
                for feature in body.features:
                    if feature.id in generated:
                        anchors[body.id]=feature.id
            for op in group:
                if op['op']!='add_feature' or op['payload']['id'] in old: continue
                payload=op['payload']; body=payload['body_id']
                if body in anchors:
                    payload['after_feature']=anchors[body]
                    anchors[body]=payload['id']
            ops+=group
        removed=[]
        for part in request.parts:
            if part.shape != 'rotor': continue
            for index in range(part.blade_count,8):
                feature_id=f'{part.id}_blade_{index}'
                if feature_id not in old: continue
                body,feature=old[feature_id]
                if body != part.body_id or feature.op != 'additive_box' or feature.name != feature_id:
                    raise ValueError(f'cannot remove customized rotor feature {feature_id}; use a new recipe ID')
                removed.append({'op':'remove_feature','target_id':feature_id,'reason':request.reason})
        new_ids={op['payload']['id'] for op in ops if op['op']=='add_feature'}
        for op in ops:
            if op['op']=='add_sketch' and op['payload']['id'] in old_sketches:
                body,_=old_sketches[op['payload']['id']]
                if op['payload']['body_id'] != body:
                    raise ValueError(f"recipe sketch {op['payload']['id']} already belongs to another body")
                op['op']='update_sketch'; op['target_id']=op['payload'].pop('id')
                op['payload'].pop('body_id')
                continue
            if op['op']!='add_feature' or op['payload']['id'] not in old: continue
            body,feature=old[op['payload']['id']]
            payload=op['payload']
            if payload['body_id']!=body or payload['op']!=feature.op:
                raise ValueError(f"recipe ID {feature.id} already belongs to another body/shape; use a new ID")
            bore_id=feature.id+'_bore'
            bore=old.get(bore_id)
            if (payload['op']=='additive_cylinder' and bore is not None and bore[0]==body
                    and bore[1].op=='subtractive_cylinder' and bore_id not in new_ids):
                raise ValueError(f"recipe ID {feature.id} has an existing tube bore; changing tube to cylinder is unsupported; use a new ID or remove the bore explicitly")
            op['op']='update_feature'; op['target_id']=payload.pop('id')
            payload.pop('body_id')
        if ir.assembly and ir.assembly.rotation and created:
            assembly=ir.assembly.model_dump(mode='json')
            assembly['grounded']+=created
            ops.append({'op':'set_assembly','payload':{'assembly':assembly},'reason':request.reason})
        base=ir.version if request.base_version=='current' else request.base_version
        ops=removed+ops
        result=await ir_patch_handler(services,{'base_version':base,'ops':ops,'summary':'Built compact part recipes'},ctx)
        if result.ok:
            result.content=json.dumps({'version':services.store.current_version(ctx.model_id),'created_bodies':created,
                'recipes':[{'id':p.id,'body_id':p.body_id,'shape':p.shape} for p in request.parts],
                'features':[op['payload'].get('id',op.get('target_id')) for op in ops if op['op'] in ('add_feature','update_feature')],
                'edit_hint':'To move/resize a recipe, resend the same recipe id and body_id with revised parameters. To refine an existing local body, use a new recipe id with operation=cut, or extend_existing=true for connected additions. Existing history and assembly connections are preserved; commit to verify the result. ir_get accepts the actual feature IDs listed above, or the body_id.',
                'next':'Complete the primary outline, then ir_commit and inspect geo_view images in isometric and relevant orthographic views before details or motion. Fix silhouette/proportion mismatches; a valid solid is not shape acceptance. ir_help(topic=shape) compares recipes; shape selects a compact schema and example.'},separators=(',',':'))
        return result
    except (ValueError,TypeError) as exc:
        return error(exc,'Use shape-specific fields: box center+size; cylinder/tube/beam start+end; loft section_axis+sections(center,radii); rotor center+axis+radius+blade_count+blade_width+thickness+hub_radius. Each body must be one connected solid; recipes are atomic. Resend stable IDs to resize; operation=cut refines existing material; extend_existing=true explicitly appends connected additions. Custom operations use scoped ir_help then ir_patch.')


async def requirements_handler(services,args,ctx):
    from tcad.tools.ir_tools import ir_patch_handler
    try: request=RequirementsCall.model_validate(args)
    except ValueError as exc: return error(exc,'Use a supported measured kind; keep qualitative goals in design_review. raw_text is preserved automatically.')
    return await ir_patch_handler(services,{'base_version':request.base_version,'ops':[{'op':'update_requirement','payload':{'constraints_append':[c.model_dump(mode='json') for c in request.constraints]},'reason':request.reason}]},ctx)


async def ir_plan_handler(services,args,ctx):
    from tcad.loop.intent import IntentPlan, PlanCall, read_plan, review_intent, write_plan
    try:
        request=PlanCall.model_validate(args)
    except ValueError as exc:
        return error(exc,'List each planned part with id, goal and origin (user|model). Re-send the same ids as the design evolves; mark a reduced part simplified/dropped, with a note when the user required it.')
    try:
        ir=services.store.load(ctx.model_id)
    except Exception:  # noqa: BLE001 — a plan outlives a missing snapshot
        ir=None
    previous=read_plan(ctx.data_dir, ctx.model_id)
    if previous:
        old_parts={part.id:part for part in previous.parts}
        for part in request.parts:
            old=old_parts.get(part.id)
            if old:
                for field in ('form','outline_id'):
                    if field not in part.model_fields_set:
                        setattr(part,field,getattr(old,field))
    plan=IntentPlan(model_id=ctx.model_id,
                    recorded_version=(previous.recorded_version if previous
                                      else getattr(ir,'version',0) or 0),
                    parts=request.parts)
    write_plan(ctx.data_dir, ctx.model_id, plan)
    data={'recorded':True,'recorded_version':plan.recorded_version,
          'parts':[part.id for part in plan.parts],
          'next':'Re-send the same ids as the design evolves. Parts with no geometry yet are listed under not_built_yet; at review a user-required part still missing becomes LOST and is recorded as a degradation.'}
    from tcad.agent.shape_guidance import FORM_ROUTES
    data['shape_routes']={part.id:FORM_ROUTES[part.form] for part in plan.parts
                          if part.form in FORM_ROUTES}
    if ir is not None:
        review=review_intent(plan,ir)
        data['against_current_ir']={
            'kept':[entry['id'] for entry in review['kept']],
            'not_built_yet':[entry['id'] for entry in review['lost']],
            'simplified_or_dropped':[entry['id'] for entry in review['reduced']],
            # Only an explicit simplified/dropped status is a degradation here;
            # a planned part with no geometry yet is simply not built yet.
            # Calling it "degraded" the moment the plan is recorded misread as
            # a defect in a live session.
            'degraded_user_parts':[entry['id'] for entry in review['degraded']
                                   if entry['state'] != 'lost']}
    return ToolResult(ok=True,content=json.dumps(data,ensure_ascii=False,separators=(',',':')))


async def rotation_handler(services,args,ctx):
    from tcad.tools.ir_tools import ir_patch_handler
    try:
        request=RotationCall.model_validate(args)
        ir=services.store.load(ctx.model_id)
        if request.hanging_body_ids:
            from tcad.ir.rotary import Suspension
            bodies={b.id:b for b in ir.bodies}
            request.suspensions += [Suspension(body_ids=[id], damping_per_s=request.damping_per_s,
                pivot=list(bodies[id].suspension_pivot.as_tuple()) if id in bodies and bodies[id].suspension_pivot else None) for id in request.hanging_body_ids]
        request=RotationCall.model_validate(request.model_dump())
        request.validate_bodies([b.id for b in ir.bodies])
        moving=set(request.rotating_body_ids)|{id for s in request.suspensions for id in s.body_ids}
        fixed=[b.id for b in ir.bodies if b.id not in moving]
        if not fixed: raise ValueError('at least one stationary support body is required')
        rig=request.model_dump(mode='json',exclude={'duration_s','frames','reason','base_version','hanging_body_ids','damping_per_s'})
        return await ir_patch_handler(services,{'base_version':ir.version if request.base_version=='current' else request.base_version,
            'ops':[{'op':'set_assembly','payload':{'assembly':{'grounded':fixed,'rotation':rig,'start':0,'end':request.duration_s,'step':request.duration_s/(request.frames-1)}},'reason':request.reason}]},ctx)
    except ValueError as exc: return error(exc,'Use horizontal axis, existing body IDs and zero-angle upright suspension geometry; clear body.motion. Native joints use assembly_configure instead.')


async def help_handler(services,args,ctx):
    if args['topic'] == 'detail':
        from tcad.agent.detail_guidance import DETAIL_ROUTES, detail_profile_example
        detail = args.get('detail')
        if detail is None:
            data = {'choices': {name: route['description'] for name, route in DETAIL_ROUTES.items()},
                    'next': 'Choose detail=slot|arc|fillet|chamfer|groove for a native example and matching scoped edits. Other operations remain available through topic=feature (revolution, thickness, draft, mirrored, linear_pattern, polar_pattern).',
                    'rules': 'Slots cut along an extrusion direction; groove cuts by revolving a profile. A sketch arc shapes a boundary; fillet rounds existing solid edges. Choose by geometry, not command count.'}
        elif detail not in DETAIL_ROUTES:
            return error(ValueError(f'Unknown detail: {detail}'), 'Choose a detail from the catalog.')
        else:
            route = DETAIL_ROUTES[detail]
            if detail in {'fillet','chamfer'}:
                result = await help_handler(services, {'topic':'feature','feature_op':detail}, ctx)
                if not result.ok:
                    return result
                data = json.loads(result.content)
            else:
                data = {'example': detail_profile_example(detail),
                        'rules': 'Examples are mm. Arc points contain the centre, radius is positive, theta1/theta2 are radians in the sketch LOCAL plane (XY here); arcs sweep counterclockwise. Close the wire with matching endpoints; an open arc cannot form a pad/pocket solid. Block fixes each example curve; change the sketch geometry for other dimensions. Commit and verify material removal/volume and the rendered outline.',
                        'assumptions': {'slot':'Existing base spans x=0..80,y=0..50,z=0..8. This 26x6 slot cuts 3 mm upward from the bottom XY face (reversed=true). Adapt location, plane and depth to measured material; use a measured face/datum for other elevations. Do not guess FaceN or use sketch.offset.',
                                        'arc':'Standalone semicircular solid: radius 8, height 5, in a new arc_part Body. For a curved cut on an existing part adapt ownership and use pocket with the measured face and direction.',
                                        'groove':'Existing base is a shaft of radius 10 and height 20 along Z. The XZ rectangle x=8..12,z=6..10 cuts an annular recess about local v_axis (world Z here). Cut profile must intersect material and leave one solid.'}[detail]}
            data.update(detail=detail, feature_ops=route['feature_ops'],
                        operations=['add_sketch','update_sketch','add_feature','update_feature']
                                   if 'sketch' in route['topics'] else ['add_feature','update_feature'],
                        unlocks='Successful detail help exposes its sketch edits and only the listed feature_ops in ir_patch on the next model request.')
        return ToolResult(ok=True, content=json.dumps(data,ensure_ascii=False,separators=(',',':')))
    if args['topic'] == 'shape':
        shape = args.get('shape')
        data = {'choices': SHAPE_CHOICES, 'review': SHAPE_REVIEW,
                'native_alternatives': 'For non-elliptical outlines use topic=sketch for ellipse/bspline and topic=feature for pad/pocket. Fillet/chamfer treat measured edges; they do not redesign the primary outline.',
                'next': 'Choose shape to read only that recipe schema and a standalone cad_build_parts example; mixed-shape batches remain supported.'}
        if shape is not None:
            if shape not in SHAPE_CHOICES:
                return error(ValueError(f'Unknown shape: {shape}'), 'Choose a shape from the recipe catalog.')
            schema = copy.deepcopy(build_authoring_tools(services)['cad_build_parts'].params_schema)
            branches = schema['properties']['parts']['items']['anyOf']
            schema['properties']['parts']['items']['anyOf'] = [
                branch for branch in branches if branch['properties']['shape']['enum'] == [shape]]
            data.update(shape=shape, schema=schema, example=shape_example(shape))
            if shape == 'loft':
                data['rules'] = ('section_axis orders section centers strictly increasing or decreasing. '
                    'radii are positive semi-axes on world Y/Z for X, X/Z for Y, X/Y for Z. '
                    'Use 2..12 sections; ruled=false blends smoothly, ruled=true gives straight generators. '
                    'Choose a few intentional stations; abrupt alternating radii or center shifts can create unwanted waves. '
                    'End sections must have positive area; no point caps, rotated sections or polar copies. '
                    'The example is solid, not a hollow shell. Inspect the compiled shape. '
                    'Custom cross-sections use sketch + datum_plane + additive_loft feature help.')
        return ToolResult(ok=True, content=json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    from tcad.tools.ir_tools import _ir_patch_schema
    from tcad.ir.validate import _VERIFIED_OP_PARAMS
    topic=args['topic']
    schema=_ir_patch_schema(); branches=schema['properties']['ops']['items']['anyOf']
    selected={'sketch':['add_sketch','update_sketch'],'feature':['add_feature','update_feature'],'requirements':['update_requirement'],'assembly':['set_assembly'],'patch':[],'workflow':[]}[topic]
    # The next request carries the complete executable schema. Repeating it in
    # help bloats the conversation and can truncate the actual usage guidance.
    data={'operations':selected}
    data['unlocks']=('Successful scoped help exposes matching ir_patch operations in the next model request. '
        'Feature help is scoped per operation: the next request carries only the fields of the operation(s) '
        'you read; other operations stay one scoped help call away.')
    if topic=='workflow':
        from tcad.agent.workflows import WORKFLOWS
        workflow=args.get('workflow')
        if workflow is None:
            data['workflows']=[{'name':name,'description':item['description']} for name,item in WORKFLOWS.items()]
            data['unlocks']='Choose a listed workflow to expose its specialized tools in the next model request.'
            data['general_mechanisms']=('These workflows are optional specializations. For general rigid mechanisms '
                '(rotors, propellers, linkages, gears, screws) read topic=assembly and configure native joints/drivers with assembly_configure.')
        elif workflow not in WORKFLOWS:
            return error(ValueError(f'Unknown workflow: {workflow}'),'Choose a workflow from the help catalog.')
        else:
            data.update(WORKFLOWS[workflow])
            data['workflow']=workflow
            data['unlocks']='These specialized tools are exposed in the next model request for this turn.'
    elif topic=='sketch':
        data['rules']='Profile points are WORLD coordinates. On origin planes: XY uses x,y,z=0; XZ uses x,z,y=0; YZ uses y,z,x=0. Pad normals XY:+Z, XZ:-Y, YZ:+X. For an elevated profile, commit first and use ir_digest to choose an actual planar face, then set plane={kind:face,feature_id:existing_feature,sub:FaceN}; write points at that measured face location in world coordinates. FaceN is a placeholder, not a guessed name. Nonzero sketch.offset is refused; do not use it to raise a profile. A body may reference a previously created feature face; keep the dependency order valid. Native circular arcs (centre point + radius + theta1/theta2 in radians), ellipse and bspline curves are supported; topic=detail,detail=slot|arc gives closed native profile examples; do not approximate curved outlines with stacked boxes. Ellipse major_radius/minor_radius are semi-axes in mm; rotation is local-plane degrees. BSpline points are WORLD interpolation points, not control poles; periodic=true closes the curve smoothly without repeating the first point. An open spline needs other edges to close a pad/pocket profile. Fully fixed geometry uses constraints [{type:Block,refs:[index]}], including ellipse/bspline. For an editable circle use DistanceX/DistanceY on refs [index,3] (centre) and Radius on refs [index], each with a numeric value. Block also fixes the radius; it does not support diameter editing.'
        data['example']={'op':'add_sketch','payload':{'id':'sk','name':'sk','body_id':'base','plane':{'kind':'origin_plane','plane':'XY'},'geometry':[{'id':'c','kind':'circle','points':[{'x':0,'y':0,'z':0}],'radius':10}],'constraints':[{'type':'Block','refs':[0]}]},'reason':'Circle for pad'}
    elif topic=='feature':
        from tcad.ir.capability import capability
        op=args.get('feature_op','pad')
        data['feature_op']=op
        data['params']=sorted(_VERIFIED_OP_PARAMS.get(op,[]))
        cap=capability(op)
        if cap is None:
            return error(ValueError(f'Unknown feature_op: {op}'), 'Choose feature_op from the declared operation enum.')
        data['capability']=cap.tier
        if op in {'pad','pocket'}:
            data['rules']='Use a closed sketch profile, not an open arc. length is mm along the sketch normal; pocket cuts into the opposite direction by default, reversed=true flips it. Measure the target plane and material before choosing depth/direction. Rounded slots use native arc/line sketches; ir_help(topic=detail,detail=slot) exposes a complete example.'
            data['example']={'op':'add_feature','payload':{'id':op,'name':op,'body_id':'base','op':op,'profile_sketch':'sk','params':{'length':5}},'reason':'Extrude profile' if op=='pad' else 'Cut profile'}
        elif op in {'revolution','groove'}:
            data['rules']='Revolve a closed profile about params.axis: X/Y/Z are body origin axes; h_axis/v_axis/n_axis are sketch LOCAL axes. angle is degrees (arc theta1/theta2 are radians). On an origin XZ sketch v_axis is world Z. Keep the profile on one side of the rotation axis. groove removes material and must intersect the existing solid; it is a revolved recess, not a straight slot. ir_help(topic=detail,detail=groove) gives the complete cutting profile.'
            data['example']={'op':'add_feature','payload':{'id':op,'name':op,'body_id':'base','op':op,'profile_sketch':'sk','params':{'axis':'v_axis','angle':360}},'reason':'Revolve a closed profile' if op=='revolution' else 'Cut a revolved recess'}
        elif op in {'fillet','chamfer'}:
            key='radius' if op=='fillet' else 'size'
            data['rules']='First commit the base geometry. When appending a treatment, base_feature is the preceding solid feature in that Body (not an earlier primitive); read ir_list_features to find it, then ir_digest(feature_id=that_id) for its actual Edge names, lengths and mid-points. Select sub_elements only from that measured feature. Values are mm. Edge1 below is only a placeholder: select the measured edge matching your intent, and re-read after topology changes. After a failed build, ir_digest(artifact_id=last_passed_artifact,feature_id=that_id) still reads prior topology. Reduce radius/size or select fewer edges; remove/suppress only the failed treatment if needed and recommit, preserving the main shape.'
            data['example']={'op':'add_feature','payload':{'id':op,'name':op,'body_id':'base','op':op,'base_feature':'existing_feature','sub_elements':['Edge1'],'params':{key:1}},'reason':'Round measured edges' if op=='fillet' else 'Bevel measured edges'}
        elif op in {'additive_loft','subtractive_loft'}:
            data['rules']='Native parametric loft: profile_sketch is the first closed section; sections is an ordered list of additional distinct sketch IDs in the SAME body. Smooth mode ruled=false blends through the sections; ruled=true connects them with straight generators. closed=false caps the end profiles; closed=true loops last back to first, not cap ends. Keep matching edge counts, starting points and winding across sections to avoid twist. For separated section planes, create datum_plane features with typed world placement and attach each sketch using plane={kind:datum_plane,feature_id:plane_id}; points remain WORLD coordinates on that plane. Subtractive loft needs existing intersecting material. This builds native CAD features, not a mesh or arbitrary code.'
            data['example']={'op':'add_feature','payload':{'id':op,'name':op,'body_id':'base','op':op,'profile_sketch':'section_start','sections':['section_middle','section_end'],'params':{'ruled':False,'closed':False}},'reason':'Blend closed profiles into a continuous solid' if op=='additive_loft' else 'Cut material with a section loft'}
        elif op=='datum_plane':
            data['rules']='Unattached native datum plane, default normal world +Z. Typed placement.position places its origin; placement.axis and angle rotate the plane in world space (degrees). For XZ cross-sections at y=a, use position={x:0,y:a,z:0}, axis={x:1,y:0,z:0}, angle=90. For XY cross-sections at z=a, use position={x:0,y:0,z:a}. Attach sketches via plane={kind:datum_plane,feature_id:plane_id}, using WORLD points that lie on this plane. Do not apply sketch.offset; a datum plane contains no solid.'
            data['example']={'op':'add_feature','payload':{'id':'section_plane','name':'section_plane','body_id':'base','op':'datum_plane','placement':{'position':{'x':0,'y':0,'z':10}}},'reason':'Place a native cross-section plane'}
        elif op.startswith(('additive_','subtractive_')):
            shape=op.split('_',1)[1]
            recipes={
                'box':({'length':10,'width':8,'height':4}, 'placement.position is the local minimum corner; length/width/height extend along positive local X/Y/Z, not around a centre.'),
                'cylinder':({'radius':4,'height':10}, 'placement.position is the base centre; height extends along positive local Z before rotation.'),
                'sphere':({'radius':6}, 'placement.position is the sphere centre. A full sphere extends by radius in both directions on every axis, including below the centre; it is not automatically a dome clipped at the base plane.'),
                'cone':({'radius1':5,'radius2':2,'height':10}, 'placement.position is the base centre; height extends along positive local Z, from radius1 at the base to radius2 at the top.'),
            }
            if shape in recipes:
                params,rule=recipes[shape]
                data['params']=sorted(params)
                data['params_note']='Basic scalar recipe, not an exhaustive native property table. Use typed placement for position/rotation, not params.center.'
                data['rules']=rule+' Additions must intersect the existing solid; subtraction must intersect material. Measure the resulting bbox, including minima, and render after commit.'
                data['example']={'op':'add_feature','payload':{'id':op,'name':op,'body_id':'base','op':op,'params':params,'placement':{'position':{'x':0,'y':0,'z':0}}},'reason':'Place native primitive; adapt dimensions and position to measured existing material'}
    elif topic=='requirements':
        data['rules']='raw_text is immutable. Qualitative motion/functionality is not a geometry constraint. Do not invent kind=note/motion/dimension or user-confirmed dimensions.'
        data['example']={'constraints':[{'kind':'bbox','value':{'x':80,'y':50,'z':8},'source_text':'80×50×8','confirmed':True}],'reason':'Record explicit user dimensions'}
    elif topic=='assembly':
        data['unlocks']='assembly_configure is exposed in the next model request after successful ir_help(topic=assembly).'
        data['rules']='Create each independent part in its own Body and place it in the assembled pose. Configure actual connections through assembly_configure: Fixed for rigid connections, appropriate movable joints for moving parts, and drivers only for requested motion. Then ir_commit, assembly_solve for saved constraint evidence or assembly_simulate for saved motion frames and sampled overlap checks. Multiple Bodies alone do not prove assembly constraints. Clear body.motion before configuring native joints/drivers. Every body must build as exactly one connected solid; solid_count grades that at ir_commit. To rebuild a body the declaration references, clear the assembly first (set_assembly assembly=null), rebuild, then reconfigure.'
        data['coordinates']='Connector position and axis are WORLD coordinates (mm); use the same connector on both sides to preserve an assembled pose. Angular formulas use radians, Linear formulas mm, and time seconds. start/end/step control saved frames (at most 600).'
        data['part_verification']='Model physical connections: shafts, supports, bearings, bores and axial stops where needed; joints create no holes or fasteners. Refine existing parts with cad_build_parts operation=cut or extend_existing=true; use scoped feature help for fillet/chamfer. After commit, geo_measure(body_id=...,what=["bbox","holes","volume"]) checks each source BRep. Review mating dimensions, clearance, proportions and details in static views before animation; missing measurements stay unresolved.'
        data['motion_verification']='Synchronized formulas remain independent drives. A single-input mechanism needs one driven input and passive joints/couplings plus physical transmission parts; Gears/Belt equations alone are virtual coupling. assembly_simulate reports driver count and ALL-frame motion_summary (closed cycles may share endpoints). Use track_points=[{name,body_id,point:[x,y,z]}] and sample_frames for trajectories/phases. Rigid oscillation proves no contact or propulsion. Check intermediate poses and sampled overlap, repair unintended collisions; unmeasured functions remain pending.'
        data['animation_modes']='For an assembly process / 装配动画, assembly_export(mode="assemble") shows separated parts converging; mode="explode" shows disassembly; mode="motion" shows operation. Choose the requested mode explicitly. A static assembly with Fixed joints needs no drivers for assemble/explode. Align Revolute and shaft axes; inspect intermediate frames and sampled overlap before claiming a mating fit.'
        data['example']={'assembly':{'grounded':['housing'],'joints':[{'id':'rotor_axis','type':'Revolute','side1':{'body_id':'housing','position':[0,0,30],'axis':[0,0,1]},'side2':{'body_id':'rotor','position':[0,0,30],'axis':[0,0,1]}}],'drivers':[{'joint_id':'rotor_axis','type':'Angular','formula':'2*pi*time'}],'start':0,'end':1,'step':0.05},'reason':'Attach and animate the rotor with a native revolute joint'}
    elif topic=='patch':
        data={'operations':[b['properties']['op']['enum'][0] for b in branches],
              'rules':'Use update_body with target_id=body_id and payload={name:new_name} to rename a body. The rename operation accepts sketch or feature IDs only. Use update_feature/update_sketch to change geometry; their target_id identifies the existing node, so do not include body_id in the partial payload.',
              'next':'Common primitives use cad_build_parts. Native ellipse/bspline profiles, pad/pocket and fillet/chamfer are available through scoped sketch/feature help, which exposes matching ir_patch operations in the next request. For exact contracts request topic=sketch, feature, requirements or assembly; do not fetch all schemas.'}
    if topic in {'sketch','feature','patch','assembly'}:
        data['body_routing']='Choose single-body or multi-body modeling based on the requirements and their complexity. An integral part can use one Body even with many features; separate components, manufacturing boundaries or relative motion may require multiple Bodies. Feature count alone does not require splitting. Honor explicit single-part or assembly intent. When splitting, create each independent part with add_body. Set payload.body_id on add_sketch/add_feature; it is required with multiple bodies. Omission uses the sole body or creates body_1 in an empty model. Updates use target_id and preserve ownership. ir_list_features returns each feature with its body_id.'
    return ToolResult(ok=True,content=json.dumps(data,ensure_ascii=False,separators=(',',':')))


def build_authoring_tools(services):
    from tcad.agent.workflows import WORKFLOWS
    from tcad.agent.detail_guidance import DETAIL_ROUTES
    from tcad.ir.capability import all_ops
    part_schema=inline_schema(BuildParts.model_json_schema())
    item=part_schema['properties']['parts']['items']
    common={'id','body_id','shape','operation','extend_existing','copies','attach','anchor'}
    # A part placed by `attach` supplies its own local origin, so the field
    # the anchor arithmetic needs (box.center / cylinder.start) may be omitted
    # and defaults to the origin; the Python validator still enforces every
    # OTHER dimension.
    relation_defaulted={'box':{'center'},'cylinder':{'start'},'tube':{'start'},'beam':{'start'}}
    dimensions={'box':{'center','size'},'cylinder':{'start','end','radius'},
                'tube':{'start','end','radius','inner_radius'},'beam':{'start','end','width','depth'},
                'loft':{'section_axis','sections'},
                'rotor':{'center','axis','radius','blade_count','blade_width','thickness','hub_radius'}}
    branches=[]
    for shape,fields in dimensions.items():
        branch=copy.deepcopy(item)
        extras = {'ruled'} if shape == 'loft' else set()
        branch['properties']={k:v for k,v in branch['properties'].items() if k in common|fields|extras}
        if shape in {'loft','rotor'}:
            branch['properties'].pop('copies', None)
        branch['properties']['shape']={'type':'string','enum':[shape]}
        branch['required']=['id','body_id','shape']+sorted(fields-relation_defaulted.get(shape,set()))
        for field in fields:
            branch['properties'][field]=branch['properties'][field]['anyOf'][0]
        branches.append(branch)
        if shape in {'cylinder','tube','beam'}:
            aligned=copy.deepcopy(branch)
            aligned['properties'].pop('end')
            aligned['required']=[field for field in aligned['required'] if field!='end']+['align','length']
            for field in ('align','length'):
                aligned['properties'][field]=copy.deepcopy(item['properties'][field]['anyOf'][0])
            branches.append(aligned)
    part_schema['properties']['parts']['items']={'anyOf':branches}
    return {
        'cad_wheel_support':ToolSpec(name='cad_wheel_support',tier=ToolTier.WRITE,
            description='Create a connected rear double A-frame, base, bearing and shaft for a Y-axis cad_wheel. wheel_body_id reads actual wheel center/thickness; no manual leg/cap coordinates. Support is behind the wheel, opposite positive-axis cabin offset, to keep cabins clear of the frame. The shaft fits the wheel bore with radial clearance; adds a bore if absent. body_id must be new. base_z and base_thickness locate the floor plate. Then assembly_motion uses this stationary body automatically. Structural loads/bearing friction are not verified.',
            params_schema=inline_schema(WheelSupport.model_json_schema()),handler=functools.partial(wheel_support_handler,services)),
        'cad_cabins':ToolSpec(name='cad_cabins',tier=ToolTier.WRITE,
            description='Create repeated upright open-box cabins with vertical hangers around a horizontal wheel. mm: wheel_center, wheel_axis, radius, count, axial_offset, box_size [x,y,z], hanger_length (hinge to box TOP), wall. Optional hang_drop overrides center distance; omit axial_offset for automatic wheel clearance. Body IDs prefix_0..prefix_N. Same prefix atomically replaces its generated cabins and refreshes existing suspension pivots; custom added features are protected. Tool computes exact circular hinge positions and copies each cavity/hanger; no coordinate arithmetic or IR sketches needed. Explicit axial_offset must clear wheel half-thickness plus cabin half-width and 5mm. Then assembly_motion(rotating_body_ids=[wheel], hanging_body_ids=returned IDs, center=wheel_center, axis=wheel_axis), ir_commit, assembly_simulate, assembly_export. Highest hanger endpoint defines the automatic hinge; all cabins stay below it.',
            params_schema=inline_schema(CabinCopies.model_json_schema()),handler=functools.partial(cabin_copies_handler,services)),
        'cad_wheel':ToolSpec(name='cad_wheel',tier=ToolTier.WRITE,
            description='Create a connected native radial structure with rim, straight spokes, hub and optional axial bore, in mm. center/axis locate it; radius is outer radius; rim_width is radial wall; thickness is axial length; spoke_width is web width. bore_radius must be smaller than hub_radius. Geometry is editable; this does not verify meshing, loads or product functionality.',
            params_schema=inline_schema(RadialWheel.model_json_schema()),handler=functools.partial(radial_wheel_handler,services)),
        'cad_build_parts':ToolSpec(name='cad_build_parts',tier=ToolTier.WRITE,
            description='Choose the primary silhouette and proportions before primitives; inspect rendered static views before detail or motion. ir_help(topic=shape,shape=loft) gives compact shape-specific schema and examples. box/beam fit prismatic parts; curved/tapered outlines need loft or sketch/pad, not stacked boxes. Batch editable native CAD recipes in mm. box=center+size; cylinder=start+end+radius; tube adds inner_radius; beam=start+end+width+depth. loft uses section_axis X/Y/Z and 2..12 ordered elliptical sections {center:[x,y,z],radii:[u,v]}; semi-axes lie on world Y/Z for X, X/Z for Y, X/Y for Z. ruled=false is smooth. rotor generates a cylindrical hub and 2..8 flat rectangular radial blades from center, axis, radius, blade_count, blade_width, thickness and hub_radius; it has no rim, pitch or airfoil. Select recipes only when these geometries fit the request; scoped sketch/feature help exposes other native operations. A missing body is created; an existing EMPTY body is filled; an existing recipe is edited by resending its stable IDs and body_id. To refine an existing local body, use a NEW recipe id with operation=cut for holes/cavities, or extend_existing=true for connected additions such as supports. Both append features, preserving prior geometry and assembly connections; no rebuild is needed. Default additions to populated bodies are refused; use a NEW body_id for separate components. Referenced components cannot receive local features. Custom operations use scoped ir_help then ir_patch. Cuts must intersect existing material. Relations instead of coordinates: attach={to:<earlier part id in this call>,where:center|top|bottom|left|right|front|back|start|end,offset:[x,y,z]} plus anchor=<own anchor> aligns this part to that anchor (default anchor: start for cylinder/tube/beam, center otherwise); align:X|Y|Z|-X|-Y|-Z with length gives cylinder/tube/beam a direction without a second endpoint. Primitive polar copies gain indexed IDs; separate_bodies=false places all copies in ONE body (they must still INTERSECT into a single connected solid — disconnected copies fail ir_commit), separate_bodies=true gives each copy its own body. No compile yet: ir_commit builds and verifies the current version; every body must compile as exactly one connected solid.',
            params_schema=part_schema,handler=functools.partial(build_parts_handler,services)),
        'ir_requirements':ToolSpec(name='ir_requirements',tier=ToolTier.WRITE,
            description='Append typed measurable geometry constraints only. Original request is preserved automatically. confirmed=true requires explicit user-stated dimensions/source_text; guessed dimensions remain unconfirmed. Qualitative animation/gravity goals belong in design_review, not constraint kinds. No requirement call is needed when the user gave no measurable numbers.',
            params_schema=inline_schema(RequirementsCall.model_json_schema()),handler=functools.partial(requirements_handler,services)),
        'ir_plan':ToolSpec(name='ir_plan',tier=ToolTier.WRITE,
            description='Record the short PART PLAN for this design and keep it true. Call once when starting a design: one entry per part with a short goal describing its silhouette, proportions and chosen shape operations, and form=prismatic|round|tapered|curved when known. Bind outline_id to the stable recipe/native feature shaping the primary outline, not a support or hole. Use ir_help(topic=shape,shape=loft) for varying sections and scoped sketch/feature help for other profiles. Preserve form/outline_id during repairs; preview limits do not justify flattening the CAD outline. Set origin=user (explicit in the user request) or model (your own choice). Re-send the SAME ids as the design evolves; a part you reduce is marked simplified/dropped — a note is required when the user required it. ir_commit compares the plan against the built IR: a planned part with no geometry is reported LOST, and a reduced user-required part is recorded as a design degradation (continue refining, or deliver a draft pending acceptance). Planning does not prove geometry, comfort or strength.',
            params_schema=inline_schema(PlanCall.model_json_schema()),handler=functools.partial(ir_plan_handler,services)),
        'assembly_motion':ToolSpec(name='assembly_motion',tier=ToolTier.WRITE,
            description='Ferris-wheel/gravity-pendulum rig ONLY: a horizontal wheel axis (Z is up) with passive hanging cabins. For ordinary propellers, vertical rotors and native joints use ir_help(topic=assembly), then assembly_configure; this tool is not a general rotor driver. rotating_body_ids revolve about center/axis. cad_cabins use hanging_body_ids directly; uniform-density COM/inertia and highest vertical hanger endpoint are measured automatically. Or use suspensions with explicit body_ids/pivot. Bodies upright, COM below hinge. RK4 integrates gravity, moving-hinge acceleration and damping. duration_s/frames set saved playback. Commit, assembly_simulate for sampled collisions, assembly_export for GIF. Not contact/structural analysis.',
            params_schema=inline_schema(RotationCall.model_json_schema()),handler=functools.partial(rotation_handler,services)),
        'ir_help':ToolSpec(name='ir_help',tier=ToolTier.READ,
            description='Discover native CAD contracts: topic=detail maps slots, circular arcs, edge fillets/chamfers and revolved grooves to runnable native examples; optional detail selects one and unlocks its needed sketch/feature edits together. topic=shape compares shape choices and static review criteria; optional shape gives just that cad_build_parts schema and example. topic=sketch explains arc/ellipse/bspline profiles; topic=feature with feature_op explains a native operation, including loft, datum_plane and fillet. Successful scoped help unlocks matching ir_patch edits in the next request; absence from the initial tool table is not a capability limit. topic=assembly unlocks assembly_configure for joints/drivers. topic=patch lists editing operations. topic=workflow lists optional specialized workflows; provide workflow to read its assumptions and expose its tools. Choose operations and workflows from the requested geometry.',
            params_schema={'type':'object','additionalProperties':False,'required':['topic'],'properties':{'topic':{'type':'string','enum':['shape','detail','sketch','feature','requirements','assembly','patch','workflow']},'detail':{'type':'string','enum':list(DETAIL_ROUTES)},'shape':{'type':'string','enum':list(SHAPE_CHOICES)},'feature_op':{'type':'string','enum':list(all_ops())},'workflow':{'type':'string','enum':list(WORKFLOWS)}}},handler=functools.partial(help_handler,services),concurrency_safe=True),
    }

class RadialWheel(BaseModel):
    model_config=ConfigDict(extra='forbid', allow_inf_nan=False)
    body_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,32}$')
    center: list[float] = Field(min_length=3,max_length=3)
    axis: list[float] = Field(default_factory=lambda:[0,1,0],min_length=3,max_length=3)
    radius: float = Field(gt=0)
    thickness: float = Field(gt=0)
    rim_width: float = Field(gt=0)
    hub_radius: float = Field(gt=0)
    spoke_count: int = Field(default=8,ge=3,le=32)
    spoke_width: float = Field(gt=0)
    bore_radius: float = Field(default=0,ge=0)
    reason: str = Field(default=AUTO_REASON)
    base_version: int | Literal['current'] = 'current'


async def radial_wheel_handler(services,args,ctx):
    import math
    try:
        p=RadialWheel.model_validate(args); norm=math.hypot(*p.axis)
        if norm<=1e-12 or not 0 <= p.bore_radius < p.hub_radius < p.radius-p.rim_width:
            raise ValueError('require nonzero axis and bore_radius < hub_radius < radius-rim_width')
        if p.spoke_width > p.hub_radius*2: raise ValueError('spoke_width must not exceed hub diameter')
        u=[a/norm for a in p.axis]
        seed=[1,0,0] if abs(u[0])<0.9 else [0,1,0]
        dot=sum(a*b for a,b in zip(seed,u)); v=[seed[i]-dot*u[i] for i in range(3)]
        v=[a/math.hypot(*v) for a in v]
        start=[p.center[i]-u[i]*p.thickness/2 for i in range(3)]
        end=[p.center[i]+u[i]*p.thickness/2 for i in range(3)]
        prefix=p.body_id
        from tcad.ir.builders import _placement
        from tcad.ir.motion import rotate
        from tcad.tools.ir_tools import ir_patch_handler
        ir=services.store.load(ctx.model_id)
        if any(b.id==prefix for b in ir.bodies):
            raise ValueError('cad_wheel requires a new body_id; use scoped ir_patch to edit existing wheels')
        ops=[{'op':'add_body','payload':{'id':prefix,'name':prefix},'reason':p.reason},
             {'op':'add_feature','payload':{'id':prefix+'_blank','name':prefix+'_blank','body_id':prefix,
                'op':'additive_cylinder','params':{'radius':p.radius,'height':p.thickness},'placement':_placement(start,u)},'reason':p.reason}]
        # Cut polygonal windows out of a single disk: avoids fragile coincident
        # rod fusions, while retaining editable sketches and straight spoke webs.
        inner=p.hub_radius+p.spoke_width/2
        outer=p.radius-p.rim_width
        if inner>=outer or p.spoke_width/2>=inner*math.sin(math.pi/p.spoke_count):
            raise ValueError('wheel dimensions leave no room for spoke windows')
        for i in range(p.spoke_count):
            theta=(i+0.5)*2*math.pi/p.spoke_count
            points=[]
            for r,sign in ((inner,-1),(outer,-1),(outer,1),(inner,1)):
                half=math.pi/p.spoke_count-math.asin(p.spoke_width/(2*r))
                turn=rotate(v,u,theta+sign*half)
                points.append(dict(zip('xyz',[end[k]+r*turn[k] for k in range(3)])))
            sketch=prefix+'_window_'+str(i)
            ops.append({'op':'add_sketch','payload':{'id':sketch,'name':sketch,'body_id':prefix,
                'plane':{'kind':'face','feature_id':prefix+'_blank','sub':'Face3'},
                'geometry':[{'id':'g'+str(j),'kind':'line','points':[points[j],points[(j+1)%4]]} for j in range(4)],
                'constraints':[{'type':'Block','refs':[j]} for j in range(4)]},'reason':p.reason})
            ops.append({'op':'add_feature','payload':{'id':sketch+'_cut','name':sketch+'_cut','body_id':prefix,
                'op':'pocket','profile_sketch':sketch,'params':{'length':p.thickness},'refs':[prefix+'_blank']},'reason':p.reason})
        if p.bore_radius:
            ops.append({'op':'add_feature','payload':{'id':prefix+'_bore','name':prefix+'_bore','body_id':prefix,
                'op':'subtractive_cylinder','params':{'radius':p.bore_radius,'height':p.thickness+2},
                'placement':_placement([start[k]-u[k] for k in range(3)],u)},'reason':p.reason})
        return await ir_patch_handler(services,{'base_version':ir.version if p.base_version=='current' else p.base_version,
            'ops':ops,'summary':f'Built radial wheel {prefix} with {p.spoke_count} editable spoke windows'},ctx)
    except ValueError as exc: return error(exc,'Use positive wheel dimensions and one new connected body. Spoke windows need room between the hub and rim.')

class CabinCopies(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    prefix: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,28}$')
    wheel_center: list[float] = Field(min_length=3,max_length=3)
    wheel_axis: list[float] = Field(default_factory=lambda:[0,1,0],min_length=3,max_length=3)
    radius: float = Field(gt=0)
    count: int = Field(default=8,ge=3,le=32)
    axial_offset: float | None = None
    box_size: list[float] = Field(default_factory=lambda:[20,20,24],min_length=3,max_length=3)
    hang_drop: float | None = Field(default=None,gt=0)
    hanger_length: float = Field(default=12,gt=0)
    wall: float = Field(default=2,gt=0)
    hanger_radius: float = Field(default=1.5,gt=0)
    reason: str = Field(default=AUTO_REASON)
    base_version: int | Literal['current'] = 'current'


async def cabin_copies_handler(services,args,ctx):
    import math
    from tcad.ir.motion import rotate
    try:
        p=CabinCopies.model_validate(args); norm=math.hypot(*p.wheel_axis)
        if norm<=1e-12 or abs(p.wheel_axis[2])>1e-8:
            raise ValueError('wheel_axis must be horizontal and nonzero')
        sx,sy,sz=p.box_size
        if p.hang_drop is None: p.hang_drop=sz/2+p.hanger_length
        ir=services.store.load(ctx.model_id)
        half_thickness=0
        for body in ir.bodies:
            for feature in body.features:
                if feature.id==body.id+'_blank' and feature.op=='additive_cylinder' and feature.placement:
                    h=feature.params.get('height',0)
                    direction=rotate([0,0,1],list(feature.placement.axis.as_tuple()),math.radians(feature.placement.angle))
                    location=[feature.placement.position.as_tuple()[i]+direction[i]*h/2 for i in range(3)]
                    if math.dist(location,p.wheel_center)<0.05: half_thickness=max(half_thickness,h/2)
        clearance=half_thickness+sy/2+5
        if p.axial_offset is None: p.axial_offset=clearance
        elif abs(p.axial_offset)<clearance:
            raise ValueError(f'axial_offset overlaps the wheel/cabin envelope; use abs(axial_offset)>={clearance:g} mm or omit it for automatic clearance')
        if min(sx,sy,sz)<=2*p.wall or p.hang_drop<=sz/2 or p.hanger_radius>=min(sx,sy)/2:
            raise ValueError('require box dimensions > 2*wall, hang_drop > box height/2 and hanger fitting inside the wall')
        # Place the hanger against the back wall: cavity subtraction cannot sever it.
        u=[v/norm for v in p.wheel_axis]
        v=[u[1],-u[0],0]
        hinge=[p.wheel_center[i]+v[i]*p.radius+u[i]*p.axial_offset for i in range(3)]
        center=[hinge[0],hinge[1],hinge[2]-p.hang_drop]
        copies={'count':p.count,'center':p.wheel_center,'axis':p.wheel_axis,
                'anchor':hinge,'rotate':False,'separate_bodies':True}
        def recipe(id,shape,**kw):
            return {'id':p.prefix+'_'+id,'body_id':p.prefix,'shape':shape,'copies':copies,**kw}
        # A centered front/back pair keeps COM exactly below the pivot and leaves
        # a visible yoke while joining the rim of the open cabin on both sides.
        parts=[recipe('box','box',center=center,size=p.box_size),
            recipe('cavity','box',operation='cut',center=[center[0],center[1],center[2]+p.wall/2+0.5],
                   size=[sx-2*p.wall,sy-2*p.wall,sz-p.wall+1])]
        if abs(u[0])>1e-8:
            raise ValueError('cabins currently use wheel_axis along +Y/-Y; other rotor axes use cad_build_parts with explicit suspensions')
        for sign in (-1,1):
            y=center[1]+sign*(sy/2-p.wall/2)
            parts.append(recipe('hanger_'+('front' if sign<0 else 'back'),'cylinder',
                start=[center[0],y,center[2]+sz/2-p.wall],end=[hinge[0],y,hinge[2]],radius=min(p.hanger_radius,p.wall/2)))
        parts.append(recipe('pin','cylinder',start=[hinge[0],center[1]-sy/2,hinge[2]],end=[hinge[0],center[1]+sy/2,hinge[2]],radius=min(p.hanger_radius,p.wall/2)))
        # Pin radius extends above its axis; keep an explicit accurate hinge instead
        # of inferring the upper bounding box for these generated cabins.
        from tcad.tools.ir_tools import ir_patch_handler
        from tcad.ir.motion import rotate
        ir=services.store.load(ctx.model_id)
        ids=[f'{p.prefix}_{i}' for i in range(p.count)]
        old_ids={b.id for b in ir.bodies if b.id.startswith(p.prefix+'_') and b.id[len(p.prefix)+1:].isdigit()}
        for body in ir.bodies:
            if body.id not in old_ids: continue
            if body.suspension_pivot is None or any(not f.id.startswith((p.prefix+'_box_',p.prefix+'_cavity_',p.prefix+'_hanger_',p.prefix+'_pin_')) for f in body.features):
                raise ValueError('prefix belongs to custom bodies; use a fresh prefix or edit selectively')
        request=BuildParts(parts=parts,reason=p.reason,base_version=p.base_version)
        ops=[{'op':'remove_body','target_id':id,'payload':{},'reason':p.reason} for id in sorted(old_ids)]
        additions,_=parts_patch(request,[b.id for b in ir.bodies if b.id not in old_ids])
        ops.extend(additions); pivots={}
        for i,id in enumerate(ids):
            radial=rotate([hinge[k]-p.wheel_center[k] for k in range(3)],p.wheel_axis,i*2*math.pi/p.count)
            pivot=[p.wheel_center[k]+radial[k] for k in range(3)]
            pivots[id]=pivot
            ops.append({'op':'update_body','target_id':id,'payload':{'suspension_pivot':dict(zip('xyz',pivot))},'reason':p.reason})
        if ir.assembly is not None:
            if ir.assembly.rotation is None:
                raise ValueError('native joints reference these bodies; clear/reconfigure the native assembly before replacing cabins')
            assembly=ir.assembly.model_dump(mode='json'); rig=assembly['rotation']
            if set(rig['rotating_body_ids']) & old_ids:
                raise ValueError('existing cabins are assigned to the rigid rotor; reconfigure assembly_motion first')
            matching=[s for s in rig['suspensions'] if set(s['body_ids'])&old_ids]
            if any(len(s['body_ids'])!=1 for s in matching):
                raise ValueError('cabins belong to grouped suspensions; reconfigure the group before replacement')
            previous={s['body_ids'][0]:s for s in matching}
            rig['suspensions']=[s for s in rig['suspensions'] if not set(s['body_ids'])&old_ids]
            if previous:
                for id in ids:
                    template=previous.get(id,next(iter(previous.values())))
                    rig['suspensions'].append({**template,'body_ids':[id],'pivot':pivots[id]})
            all_ids=({b.id for b in ir.bodies}-old_ids)|set(ids)
            moving=set(rig['rotating_body_ids'])|{id for susp in rig['suspensions'] for id in susp['body_ids']}
            assembly['grounded']=sorted(all_ids-moving)
            ops.append({'op':'set_assembly','payload':{'assembly':assembly},'reason':p.reason})
        result=await ir_patch_handler(services,{'base_version':ir.version if p.base_version=='current' else p.base_version,'ops':ops,'summary':'Generated upright cabins and physical hinge metadata'},ctx)
        if result.ok:
            result.content=json.dumps({'version':services.store.current_version(ctx.model_id),
                'hanging_body_ids':[f'{p.prefix}_{i}' for i in range(p.count)],
                'hinge_note':'Use assembly_motion hanging_body_ids; automatic hinge excludes the upper pin radius.',
                'next':'Configure assembly_motion, ir_commit, assembly_simulate and assembly_export.'},separators=(',',':'))
        return result
    except ValueError as exc: return error(exc,'Use a unique recipe prefix, a horizontal Y-axis and a cabin box/hanger that fits the wheel clearance.')

class WheelSupport(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    body_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,28}$')
    wheel_body_id: str
    base_z: float = 0
    base_thickness: float = Field(default=10,gt=0)
    leg_width: float = Field(default=12,gt=0)
    foot_span: float | None = Field(default=None,gt=0)
    rear_spacing: float = Field(default=30,gt=0)
    reason: str = Field(default=AUTO_REASON)
    base_version: int | Literal['current'] = 'current'


async def wheel_support_handler(services,args,ctx):
    import math
    from tcad.ir.motion import rotate
    from tcad.ir.builders import _placement
    from tcad.tools.ir_tools import ir_patch_handler
    try:
        p=WheelSupport.model_validate(args);ir=services.store.load(ctx.model_id)
        wheel=next((b for b in ir.bodies if b.id==p.wheel_body_id),None)
        blank=next((f for f in wheel.features if f.id==p.wheel_body_id+'_blank'),None) if wheel else None
        if blank is None or blank.placement is None: raise ValueError('wheel_body_id must refer to a cad_wheel body')
        direction=rotate([0,0,1],blank.placement.axis.as_tuple(),math.radians(blank.placement.angle))
        if abs(direction[1])<0.999999: raise ValueError('automatic support currently requires a Y-axis wheel')
        thickness=blank.params['height']; radius=blank.params['radius']; sign=1 if direction[1]>0 else -1
        c=[blank.placement.position.as_tuple()[i]+direction[i]*thickness/2 for i in range(3)]
        if c[2]<=p.base_z+p.base_thickness+p.leg_width: raise ValueError('wheel center must be above the base and support caps')
        span=p.foot_span or 1.5*radius
        front=thickness/2+2*p.leg_width+5;rear=front+p.rear_spacing
        center=[c[0],c[1]-sign*(front+rear)/2,p.base_z+p.base_thickness/2]
        parts=[{'id':p.body_id+'_base','body_id':p.body_id,'shape':'box','center':center,
            'size':[span+2*p.leg_width+20,p.rear_spacing+2*p.leg_width+20,p.base_thickness]}]
        for side,distance in (('front',front),('rear',rear)):
            y=c[1]-sign*distance
            for i in (-1,1):
                parts.append({'id':p.body_id+'_'+side+('_left' if i<0 else '_right'),
                    'body_id':p.body_id,'shape':'beam','start':[c[0]+i*span/2,y,p.base_z+p.base_thickness-1],
                    'end':[c[0],y,c[2]],'width':p.leg_width,'depth':p.leg_width})
        parts.append({'id':p.body_id+'_bearing','body_id':p.body_id,'shape':'cylinder',
            'start':[c[0],c[1]-sign*(rear+p.leg_width),c[2]],'end':[c[0],c[1]-sign*(front-p.leg_width),c[2]],'radius':p.leg_width})
        bore=next((f for f in wheel.features if f.id in (p.wheel_body_id+'_bore',p.wheel_body_id+'_auto_bore')),None)
        bore_radius=bore.params['radius'] if bore else min(4,p.leg_width/3)
        parts.append({'id':p.body_id+'_shaft','body_id':p.body_id,'shape':'cylinder',
            'start':[c[0],c[1]-sign*(rear+p.leg_width),c[2]],'end':[c[0],c[1]+sign*(thickness/2+3),c[2]],'radius':bore_radius*0.85})
        request=BuildParts(parts=parts,reason=p.reason,base_version=p.base_version)
        ops,created=parts_patch(request,[b.id for b in ir.bodies])
        if p.body_id in {b.id for b in ir.bodies}: raise ValueError('support body_id already exists; edit existing features with cad_build_parts')
        if bore is None:
            ops.append({'op':'add_feature','payload':{'body_id':wheel.id,'id':wheel.id+'_auto_bore','name':wheel.id+'_auto_bore',
                'op':'subtractive_cylinder','params':{'radius':bore_radius,'height':thickness+2},
                'placement':_placement([c[i]-direction[i]*(thickness/2+1) for i in range(3)],direction)},'reason':p.reason})
        if ir.assembly is not None:
            if ir.assembly.rotation is None: raise ValueError('configure native grounding explicitly before adding a support')
            assembly=ir.assembly.model_dump(mode='json');assembly['grounded']+=created
            ops.append({'op':'set_assembly','payload':{'assembly':assembly},'reason':p.reason})
        return await ir_patch_handler(services,{'base_version':ir.version if p.base_version=='current' else p.base_version,
            'ops':ops,'summary':'Added connected rear A-frame, bearing and clearance shaft'},ctx)
    except ValueError as exc: return error(exc,'Use a new support body ID and a cad_wheel on the Y axis; the support is behind the wheel, opposite positive-axis cabin offset.')
