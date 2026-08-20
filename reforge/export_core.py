# reforge/export_core.py
import os
from mathutils import Matrix

from .utils import (
    ensure_dir,
    safe_remove_file,
    write_text_file,
    sanitize_id,
    select_only,
    export_glb_selected,
    get_prop,
    is_object_visible,
)

from .materials import (
    iter_unique_materials_in_order,
    resolve_defold_material_and_texture_for_material,
    ensure_material_props,
)

from .collision import (
    export_convex_hull_points,
    make_collisionobject_text,
)

from .defold_formats import (
    make_model_text_multi,
    make_go_ref_model_text,
    make_collection_text_grouped_embedded,
)

from .bake import bake_color_emit_png
from .variant_planner import plan_variant_export


AXIS_CONVERT = Matrix((
    (1, 0, 0, 0),
    (0, 0, 1, 0),
    (0, -1, 0, 0),
    (0, 0, 0, 1),
))


def has_defold_collision(obj) -> bool:
    return bool(get_prop(obj, "defold_collision"))


def get_collision_group(obj) -> str:
    v = get_prop(obj, "collision_group")
    v = (str(v).strip() if v is not None else "")
    return v if v else "default"


def get_collision_mask(obj) -> str:
    v = get_prop(obj, "collision_mask")
    v = (str(v).strip() if v is not None else "")
    return v if v else "default"


def to_defold_trs(obj):
    """
    Convert Blender world transform to Defold-friendly TRS using axis conversion.
    Returns:
      pos (x,y,z), quat (x,y,z,w), scale (x,y,z)
    """
    mw = obj.matrix_world.copy()
    c = AXIS_CONVERT
    mw_def = c @ mw @ c.inverted()
    loc = mw_def.to_translation()
    rot = mw_def.to_quaternion()
    scl = mw_def.to_scale()
    return (loc.x, loc.y, loc.z), (rot.x, rot.y, rot.z, rot.w), (scl.x, scl.y, scl.z)


def _make_baked_texture_filename(proto: str, mat_name: str) -> str:
    # Stable filename, always PNG
    return f"{proto}__{sanitize_id(mat_name)}_albedo.png"

def _material_prop_bool(mat, key: str, default: bool = False) -> bool:
    if not mat:
        return default
    v = mat.get(key)
    return bool(v) if v is not None else default

def _material_prop_int(mat, key: str, default: int) -> int:
    if not mat:
        return default
    v = mat.get(key)
    try:
        return int(v)
    except Exception:
        return default


def export_prototype_glb(context, obj, abs_path: str):
    """Export one object's GLB, optionally removing its world translation."""
    select_only(obj)

    if not context.scene.reforge_settings.export_at_world_origin:
        export_glb_selected(abs_path)
        return

    original_matrix_world = obj.matrix_world.copy()
    try:
        obj.matrix_world.translation = (0.0, 0.0, 0.0)
        export_glb_selected(abs_path)
    finally:
        obj.matrix_world = original_matrix_world


def _asset_directories(settings):
    project_root = settings.project_root
    if not project_root or not os.path.isdir(project_root):
        raise RuntimeError("Project Root is empty or not found.")
    return {
        "models": os.path.join(project_root, settings.models_dir),
        "prefabs": os.path.join(project_root, settings.prefabs_dir),
        "textures": os.path.join(project_root, settings.textures_dir),
        "collisions": os.path.join(project_root, settings.collisions_dir),
    }


def _material_slots(obj):
    try:
        return list(obj.data.materials) if obj and obj.data else []
    except Exception:
        return []


def _resolve_model_material_blocks(
    settings,
    obj,
    abs_textures,
    material_bindings=None,
    texture_filename_prefix="",
    bake_proto=None,
):
    """Resolve model blocks, optionally binding etalon names to variant slots."""
    if material_bindings is not None:
        slots = _material_slots(obj)
        blocks = []
        for binding in material_bindings:
            slot_index = binding["slot_index"]
            mat = slots[slot_index] if slot_index is not None and slot_index < len(slots) else None
            if mat:
                ensure_material_props(mat)
            _, defold_mat_path, defold_tex_path = resolve_defold_material_and_texture_for_material(
                settings=settings,
                mat=mat,
                abs_textures_dir=abs_textures,
                textures_dir_project=settings.textures_dir,
                obj=obj,
                texture_filename_prefix=texture_filename_prefix,
            )
            blocks.append((binding["name"], defold_mat_path, defold_tex_path))
        return blocks

    materials = iter_unique_materials_in_order(obj)
    for mat in materials:
        ensure_material_props(mat)

    blocks = []
    for mat in materials or [None]:
        mat_name, defold_mat_path, defold_tex_path = resolve_defold_material_and_texture_for_material(
            settings=settings,
            mat=mat,
            abs_textures_dir=abs_textures,
            textures_dir_project=settings.textures_dir,
            obj=obj,
        )

        if mat and bake_proto and _material_prop_bool(mat, "bake_color_texture"):
            bake_resolution = _material_prop_int(mat, "bake_resolution", 1024)
            bake_padding = _material_prop_int(mat, "bake_padding", 8)
            baked_filename = _make_baked_texture_filename(bake_proto, mat_name)
            baked_abs = os.path.join(abs_textures, baked_filename)
            safe_remove_file(baked_abs)
            if bake_color_emit_png(
                obj=obj,
                mat=mat,
                out_abs_path=baked_abs,
                resolution=bake_resolution,
                padding=bake_padding,
            ):
                defold_tex_path = f"/{settings.textures_dir}/{baked_filename}".replace("\\", "/")

        blocks.append((mat_name, defold_mat_path, defold_tex_path))
    return blocks


def _write_model_asset(abs_model, mesh_project_path, model_name, blocks):
    safe_remove_file(abs_model)
    write_text_file(abs_model, make_model_text_multi(mesh_project_path, model_name, blocks))


def _export_collision_assets(settings, obj, proto, abs_collisions):
    abs_convex = os.path.join(abs_collisions, f"{proto}.convexshape")
    abs_colobj = os.path.join(abs_collisions, f"{proto}.collisionobject")
    safe_remove_file(abs_convex)
    safe_remove_file(abs_colobj)
    if not has_defold_collision(obj):
        return None

    convex_project_path = f"/{settings.collisions_dir}/{proto}.convexshape".replace("\\", "/")
    colobj_project_path = f"/{settings.collisions_dir}/{proto}.collisionobject".replace("\\", "/")
    export_convex_hull_points(obj, abs_convex)
    write_text_file(
        abs_colobj,
        make_collisionobject_text(
            convex_project_path,
            get_collision_group(obj),
            get_collision_mask(obj),
        ),
    )
    return colobj_project_path


def _create_prefab_once(abs_go, model_project_path, collisionobject_project_path):
    if not os.path.isfile(abs_go):
        write_text_file(abs_go, make_go_ref_model_text(model_project_path, collisionobject_project_path))


def export_single_prototype_assets(context, obj) -> str:
    """
    Export assets for ONE prototype:
      - <proto>.glb
      - <proto>.model
      - create <proto>.go once (never overwritten)
      - optional collision: <proto>.convexshape + <proto>.collisionobject (overwritten)
      - optional bake: per-material PNGs (overwritten)
    Returns: proto id (sanitized)
    """
    s = context.scene.reforge_settings
    dirs = _asset_directories(s)

    if not obj or obj.type != "MESH":
        raise RuntimeError("Active object is not a MESH.")

    proto_raw = get_prop(obj, "defold_prototype")
    if not proto_raw:
        raise RuntimeError("Active object has no 'defold_prototype' custom property.")
    proto = sanitize_id(proto_raw)

    abs_models = dirs["models"]
    abs_prefabs = dirs["prefabs"]
    abs_textures = dirs["textures"]
    abs_collisions = dirs["collisions"]

    materials = iter_unique_materials_in_order(obj)
    for m in materials:
        ensure_material_props(m)
    needs_bake = any(_material_prop_bool(m, "bake_color_texture") for m in materials)

    # ensure dirs
    ensure_dir(abs_models)
    ensure_dir(abs_prefabs)
    ensure_dir(abs_collisions)
    if s.export_textures or needs_bake:
        ensure_dir(abs_textures)

    # filenames
    glb_filename = f"{proto}.glb"
    model_filename = f"{proto}.model"
    go_filename = f"{proto}.go"

    # absolute paths
    abs_glb = os.path.join(abs_models, glb_filename)
    abs_model = os.path.join(abs_models, model_filename)
    abs_go = os.path.join(abs_prefabs, go_filename)

    # project paths
    glb_project_path = f"/{s.models_dir}/{glb_filename}".replace("\\", "/")
    model_project_path = f"/{s.models_dir}/{model_filename}".replace("\\", "/")

    # Preserve the ordinary export's cleanup timing if GLB export fails.
    safe_remove_file(abs_glb)
    safe_remove_file(abs_model)
    safe_remove_file(os.path.join(abs_collisions, f"{proto}.convexshape"))
    safe_remove_file(os.path.join(abs_collisions, f"{proto}.collisionobject"))
    export_prototype_glb(context, obj, abs_glb)

    blocks = _resolve_model_material_blocks(
        s,
        obj,
        abs_textures,
        bake_proto=proto,
    )
    _write_model_asset(abs_model, glb_project_path, proto, blocks)

    collisionobject_project_path = _export_collision_assets(s, obj, proto, abs_collisions)
    _create_prefab_once(abs_go, model_project_path, collisionobject_project_path)

    return proto


def export_variant_assets(context, selected_objects):
    """Export one shared GLB per set plus one model and model-only prefab per variant."""
    mesh_objects = [obj for obj in selected_objects if obj and obj.type == "MESH"]
    if not mesh_objects:
        raise RuntimeError("No mesh objects selected.")

    s = context.scene.reforge_settings
    dirs = _asset_directories(s)

    objects_by_name = {obj.name: obj for obj in mesh_objects}
    selection_data = []
    for obj in mesh_objects:
        selection_data.append({
            "name": obj.name,
            "geometry_signature": (len(obj.data.vertices), len(obj.data.polygons)),
            "material_slots": tuple(
                mat.name if mat else None for mat in _material_slots(obj)
            ),
        })

    # Plan and validate every output name before creating, deleting, or writing files.
    plan = plan_variant_export(selection_data)

    ensure_dir(dirs["models"])
    ensure_dir(dirs["prefabs"])
    if s.export_textures:
        ensure_dir(dirs["textures"])

    for variant_set in plan:
        shared_mesh_name = variant_set["shared_mesh_name"]
        glb_filename = f"{shared_mesh_name}.glb"
        abs_glb = os.path.join(dirs["models"], glb_filename)
        glb_project_path = f"/{s.models_dir}/{glb_filename}".replace("\\", "/")

        safe_remove_file(abs_glb)
        export_prototype_glb(
            context,
            objects_by_name[variant_set["etalon_name"]],
            abs_glb,
        )

        for variant in variant_set["variants"]:
            obj = objects_by_name[variant["object_name"]]
            blocks = _resolve_model_material_blocks(
                s,
                obj,
                dirs["textures"],
                material_bindings=variant["materials"],
                texture_filename_prefix=variant["texture_prefix"],
            )
            model_filename = f"{variant['model_name']}.model"
            abs_model = os.path.join(dirs["models"], model_filename)
            _write_model_asset(
                abs_model,
                glb_project_path,
                variant["model_name"],
                blocks,
            )

            model_project_path = f"/{s.models_dir}/{model_filename}".replace("\\", "/")
            abs_go = os.path.join(dirs["prefabs"], f"{variant['model_name']}.go")
            _create_prefab_once(abs_go, model_project_path, None)

    return {
        "sets": len(plan),
        "variants": sum(len(item["variants"]) for item in plan),
        # Kept for the later warning-presentation issue; issue 02 reports counts only.
        "warnings": [warning for item in plan for warning in item["warnings"]],
    }


def export_all_prototypes_assets_no_scene(context) -> int:
    """
    Export assets (.glb/.model/optional collisions/bake) for all prototypes in scene.
    Does NOT regenerate .collection.
    """
    s = context.scene.reforge_settings
    view_layer = context.view_layer

    groups = {}
    for obj in context.scene.objects:
        if obj.type != "MESH":
            continue
        if s.export_visible_only and not is_object_visible(obj, view_layer):
            continue
        proto = get_prop(obj, "defold_prototype")
        if not proto:
            continue
        proto = sanitize_id(proto)
        groups.setdefault(proto, []).append(obj)

    if not groups:
        raise RuntimeError("No MESH objects with 'defold_prototype' found (with current visibility filter).")

    n = 0
    for proto in sorted(groups.keys()):
        # export only the first object by name as the etalon mesh for this proto
        etalon = min(groups[proto], key=lambda obj: obj.name)
        export_single_prototype_assets(context, etalon)
        n += 1
    return n


def run_export_scene(context) -> str:
    """
    Full pipeline:
      - export assets for each prototype (etalon)
      - generate <collection_name>.collection with grouped embedded instances:
            root
              ├─ <protoA> -> children instances
              ├─ <protoB> -> children instances
              ...
    Returns absolute path to generated .collection
    """
    s = context.scene.reforge_settings
    project_root = s.project_root
    if not project_root or not os.path.isdir(project_root):
        raise RuntimeError("Project Root is empty or not found.")

    abs_scenes = os.path.join(project_root, s.scenes_dir)
    ensure_dir(abs_scenes)

    # group objects by prototype
    groups = {}
    view_layer = context.view_layer
    for obj in context.scene.objects:
        if obj.type != "MESH":
            continue
        if s.export_visible_only and not is_object_visible(obj, view_layer):
            continue
        proto = get_prop(obj, "defold_prototype")
        if not proto:
            continue
        proto = sanitize_id(proto)
        groups.setdefault(proto, []).append(obj)

    if not groups:
        raise RuntimeError("No MESH objects with 'defold_prototype' found (with current visibility filter).")

    # ensure all prototypes are exported; create .go once
    proto_to_go = {}
    for proto, objs in groups.items():
        etalon = min(objs, key=lambda obj: obj.name)
        export_single_prototype_assets(context, etalon)
        proto_to_go[proto] = f"/{s.prefabs_dir}/{proto}.go".replace("\\", "/")

    # create instance list per proto
    instances_by_proto = {}
    counters = {p: 0 for p in groups.keys()}

    for proto, objs in groups.items():
        for obj in objs:
            counters[proto] += 1
            inst_id = f"{proto}_{counters[proto]:03d}"
            pos, quat, scale = to_defold_trs(obj)
            instances_by_proto.setdefault(proto, []).append({
                "id": inst_id,
                "prototype": proto_to_go[proto],
                "pos": pos,
                "quat": quat,
                "scale": scale,
            })

    protos_sorted = sorted(groups.keys())
    collection_text = make_collection_text_grouped_embedded(
        s.collection_name,
        protos_sorted,
        instances_by_proto
    )

    abs_collection = os.path.join(abs_scenes, f"{s.collection_name}.collection")
    safe_remove_file(abs_collection)
    write_text_file(abs_collection, collection_text)
    return abs_collection
