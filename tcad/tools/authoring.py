"""Model-facing recipes: small structured arguments, ordinary IR underneath."""
import copy
import functools
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tcad.core.types import ToolSpec, ToolTier, ToolResult, ToolError, ToolErrorKind
from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.rotary import RotaryRig
from tcad.ir.schema import ConstraintExpr


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
                'description':'Last read IR version (integer or decimal string), or current. Explicit versions still reject stale edits.'}
        return expanded
    return expand(root)


def error(exc, hint):
    message = '; '.join(f"{'.'.join(map(str,e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:3]) if isinstance(exc,ValidationError) else str(exc)
    return ToolResult(ok=False,error=ToolError(kind=ToolErrorKind.SCHEMA,message=message,hint=hint))


class RequirementsCall(BaseModel):
    model_config=ConfigDict(extra='forbid')
    constraints: list[ConstraintExpr] = Field(min_length=1,max_length=40)
    reason: str = Field(min_length=1)
    base_version: int | Literal['current'] = 'current'


class RotationCall(RotaryRig):
    duration_s: float = Field(default=12,ge=0.1,le=60)
    frames: int = Field(default=121,ge=2,le=600)
    reason: str = Field(min_length=1)
    base_version: int | Literal['current'] = 'current'
    hanging_body_ids: list[str] = Field(default_factory=list,max_length=32)
    damping_per_s: float = Field(default=0.5,ge=0,le=100)


async def build_parts_handler(services,args,ctx):
    from tcad.tools.ir_tools import ir_patch_handler
    try:
        request=BuildParts.model_validate(args)
        ir=services.store.load(ctx.model_id)
        ops,created=parts_patch(request,[b.id for b in ir.bodies])
        old={f.id:(b.id,f) for b in ir.bodies for f in b.features}
        new_ids={op['payload']['id'] for op in ops if op['op']=='add_feature'}
        for op in ops:
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
        result=await ir_patch_handler(services,{'base_version':base,'ops':ops,'summary':'Built compact part recipes'},ctx)
        if result.ok:
            result.content=json.dumps({'version':services.store.current_version(ctx.model_id),'created_bodies':created,'features':[op['payload'].get('id',op.get('target_id')) for op in ops if op['op'] in ('add_feature','update_feature')],'next':'Add remaining parts or assembly_motion; then ir_commit.'},separators=(',',':'))
        return result
    except (ValueError,TypeError) as exc:
        return error(exc,'box: center+size; cylinder/tube/beam: start+end. tube adds inner_radius. Each body must be one connected solid; recipes are atomic.')


async def requirements_handler(services,args,ctx):
    from tcad.tools.ir_tools import ir_patch_handler
    try: request=RequirementsCall.model_validate(args)
    except ValueError as exc: return error(exc,'Use a supported measured kind; keep qualitative goals in design_review. raw_text is preserved automatically.')
    return await ir_patch_handler(services,{'base_version':request.base_version,'ops':[{'op':'update_requirement','payload':{'constraints_append':[c.model_dump(mode='json') for c in request.constraints]},'reason':request.reason}]},ctx)


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
    from tcad.tools.ir_tools import _ir_patch_schema
    from tcad.ir.validate import _VERIFIED_OP_PARAMS
    topic=args['topic']
    schema=_ir_patch_schema(); branches=schema['properties']['ops']['items']['anyOf']
    selected={'sketch':['add_sketch','update_sketch'],'feature':['add_feature','update_feature'],'requirements':['update_requirement'],'assembly':['set_assembly'],'patch':[]}[topic]
    data={'schema':[b for b in branches if b['properties']['op']['enum'][0] in selected]}
    if topic=='sketch':
        data['rules']='World coordinates: XY uses x,y,z=0; XZ uses x,z,y=0; YZ uses y,z,x=0. Pad normals XY:+Z, XZ:-Y, YZ:+X. Fully fixed geometry uses constraints [{type:Block,refs:[index]}]. For an editable circle use DistanceX/DistanceY on refs [index,3] (centre) and Radius on refs [index], each with a numeric value. Block also fixes the radius; it does not support diameter editing.'
        data['example']={'op':'add_sketch','payload':{'id':'sk','name':'sk','body_id':'base','plane':{'kind':'origin_plane','plane':'XY'},'geometry':[{'id':'c','kind':'circle','points':[{'x':0,'y':0,'z':0}],'radius':10}],'constraints':[{'type':'Block','refs':[0]}]},'reason':'Circle for pad'}
    elif topic=='feature':
        data['params']=sorted(_VERIFIED_OP_PARAMS.get(args.get('feature_op','pad'),[]))
        data['example']={'op':'add_feature','payload':{'id':'pad','name':'pad','body_id':'base','op':'pad','profile_sketch':'sk','params':{'length':5}},'reason':'Extrude profile'}
    elif topic=='requirements':
        data['rules']='raw_text is immutable. Qualitative motion/functionality is not a geometry constraint. Do not invent kind=note/motion/dimension or user-confirmed dimensions.'
        data['example']={'constraints':[{'kind':'bbox','value':{'x':80,'y':50,'z':8},'source_text':'80×50×8','confirmed':True}],'reason':'Record explicit user dimensions'}
    elif topic=='patch':
        data={'operations':[b['properties']['op']['enum'][0] for b in branches],
              'next':'Prefer cad_build_parts / cad_wheel / assembly_motion. For exact low-level shapes request topic=sketch, feature, requirements or assembly; do not fetch all schemas.'}
    return ToolResult(ok=True,content=json.dumps(data,ensure_ascii=False,separators=(',',':')))


def build_authoring_tools(services):
    part_schema=inline_schema(BuildParts.model_json_schema())
    item=part_schema['properties']['parts']['items']
    common={'id','body_id','shape','operation','copies'}
    dimensions={'box':{'center','size'},'cylinder':{'start','end','radius'},
                'tube':{'start','end','radius','inner_radius'},'beam':{'start','end','width','depth'}}
    branches=[]
    for shape,fields in dimensions.items():
        branch=copy.deepcopy(item)
        branch['properties']={k:v for k,v in branch['properties'].items() if k in common|fields}
        branch['properties']['shape']={'type':'string','enum':[shape]}
        branch['required']=['id','body_id','shape']+sorted(fields)
        for field in fields:
            branch['properties'][field]=branch['properties'][field]['anyOf'][0]
        branches.append(branch)
    part_schema['properties']['parts']['items']={'anyOf':branches}
    return {
        'cad_wheel_support':ToolSpec(name='cad_wheel_support',tier=ToolTier.WRITE,
            description='Create a connected rear double A-frame, base, bearing and shaft for a Y-axis cad_wheel. wheel_body_id reads actual wheel center/thickness; no manual leg/cap coordinates. Support is behind the wheel, opposite positive-axis cabin offset, to keep cabins clear of the frame. The shaft fits the wheel bore with radial clearance; adds a bore if absent. body_id must be new. base_z and base_thickness locate the floor plate. Then assembly_motion uses this stationary body automatically. Structural loads/bearing friction are not verified.',
            params_schema=inline_schema(WheelSupport.model_json_schema()),handler=functools.partial(wheel_support_handler,services)),
        'cad_cabins':ToolSpec(name='cad_cabins',tier=ToolTier.WRITE,
            description='Create repeated upright open-box cabins with vertical hangers around a horizontal wheel. mm: wheel_center, wheel_axis, radius, count, axial_offset, box_size [x,y,z], hanger_length (hinge to box TOP), wall. Optional hang_drop overrides center distance; omit axial_offset for automatic wheel clearance. Body IDs prefix_0..prefix_N. Same prefix atomically replaces its generated cabins and refreshes existing suspension pivots; custom added features are protected. Tool computes exact circular hinge positions and copies each cavity/hanger; no coordinate arithmetic or IR sketches needed. Explicit axial_offset must clear wheel half-thickness plus cabin half-width and 5mm. Then assembly_motion(rotating_body_ids=[wheel], hanging_body_ids=returned IDs, center=wheel_center, axis=wheel_axis), ir_commit, assembly_simulate, assembly_export. Highest hanger endpoint defines the automatic hinge; all cabins stay below it.',
            params_schema=inline_schema(CabinCopies.model_json_schema()),handler=functools.partial(cabin_copies_handler,services)),
        'cad_wheel':ToolSpec(name='cad_wheel',tier=ToolTier.WRITE,
            description='Create one connected editable radial wheel (rim, straight spokes, hub and optional axial bore) from dimensions in mm. center/axis locate wheel; radius is outer radius; rim_width is radial wall; thickness is axial rim/hub length; spoke_width is web width. Cut fully constrained polygonal windows from one cylinder, avoiding fragile rod fusions. bore_radius must be smaller than hub_radius. Use for Ferris wheels/pulleys/structural wheels, not verified gears. Then add supports/cabins using cad_build_parts, assembly_motion and ir_commit.',
            params_schema=inline_schema(RadialWheel.model_json_schema()),handler=functools.partial(radial_wheel_handler,services)),
        'cad_build_parts':ToolSpec(name='cad_build_parts',tier=ToolTier.WRITE,
            description='Batch editable PartDesign primitives in mm. Repeated recipe id updates the same shape/placement in its original history; new IDs add features. Changing tube to cylinder under the same ID is rejected; remove the bore explicitly with scoped ir_patch if a solid cylinder is intended. box=center+size [x,y,z]; cylinder=start+end+radius; tube adds inner_radius, with the bore limited to start..end; beam=start+end+width+depth, centered cross section. Same body_id fuses connected additions; cut requires existing material. Disconnected/moving parts need distinct body_id. Optional polar copies use right-hand axis: IDs gain _0,_1,...; rotate=false keeps upright; set anchor to the suspension pivot so the cabin stays BELOW each copied hinge. separate_bodies=false fuses spokes into one wheel. Atomic, no compile yet. Batch connected geometry then ir_commit. No manual sketches or constraints needed.',
            params_schema=part_schema,handler=functools.partial(build_parts_handler,services)),
        'ir_requirements':ToolSpec(name='ir_requirements',tier=ToolTier.WRITE,
            description='Append typed measurable geometry constraints only. Original request is preserved automatically. confirmed=true requires explicit user-stated dimensions/source_text; guessed dimensions remain unconfirmed. Qualitative animation/gravity goals belong in design_review, not constraint kinds. No requirement call is needed when the user gave no measurable numbers.',
            params_schema=inline_schema(RequirementsCall.model_json_schema()),handler=functools.partial(requirements_handler,services)),
        'assembly_motion':ToolSpec(name='assembly_motion',tier=ToolTier.WRITE,
            description='Configure constant-speed horizontal wheel/rotor and passive gravity-hanging cabins (Z up, mm/s). rotating_body_ids revolve about center/axis. For cad_cabins use hanging_body_ids directly: uniform-density COM/inertia and highest vertical hanger endpoint are measured automatically. Or use suspensions with explicit body_ids/pivot. Bodies must be upright, COM below hinge. Omit com_distance_mm/inertia_factor to use measured mass properties. RK4 integrates gravity, moving-hinge acceleration and damping; cabins swing naturally, never rotate rigidly with the wheel. Other bodies are fixed. duration_s/frames set saved playback. Commit, assembly_simulate for evidence/collision samples, assembly_export for GIF. Not contact/structural analysis.',
            params_schema=inline_schema(RotationCall.model_json_schema()),handler=functools.partial(rotation_handler,services)),
        'ir_help':ToolSpec(name='ir_help',tier=ToolTier.READ,
            description='Fetch exact scoped schemas and a working example BEFORE unfamiliar low-level ir_patch. Common shapes already have complete schemas: use cad_build_parts/cad_wheel directly. topic=patch lists editing operations; specific topics expose only necessary editing contracts.',
            params_schema={'type':'object','additionalProperties':False,'required':['topic'],'properties':{'topic':{'type':'string','enum':['sketch','feature','requirements','assembly','patch']},'feature_op':{'type':'string'}}},handler=functools.partial(help_handler,services),concurrency_safe=True),
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
    reason: str = Field(min_length=1)
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
    reason: str = Field(min_length=1)
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
    reason: str = Field(min_length=1)
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
