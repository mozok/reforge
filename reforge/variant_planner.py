"""Pure planning for selection-driven variant exports.

This module intentionally has no Blender dependency.  Callers provide plain object
records and receive a plan containing only dictionaries, lists, tuples, and strings.
"""

from .naming import sanitize_id, split_duplicate_suffix


def _variant_names(object_name):
    base_name, suffix = split_duplicate_suffix(object_name)
    if suffix is None:
        return base_name, sanitize_id(object_name)
    return base_name, f"{sanitize_id(base_name)}_{suffix}"


def _find_output_collisions(plan):
    outputs = {
        "shared mesh": {},
        "model": {},
        "texture prefix": {},
    }
    for variant_set in plan:
        outputs["shared mesh"].setdefault(
            f"{variant_set['shared_mesh_name']}.glb", []
        ).append(f"set {variant_set['base_name']!r}")
        for variant in variant_set["variants"]:
            owner = f"object {variant['object_name']!r}"
            outputs["model"].setdefault(
                f"{variant['model_name']}.model", []
            ).append(owner)
            outputs["texture prefix"].setdefault(
                variant["texture_prefix"], []
            ).append(owner)

    collisions = []
    for output_kind, names in outputs.items():
        for output_name, owners in names.items():
            if len(owners) > 1:
                collisions.append(
                    f"{output_kind} {output_name!r} from {', '.join(owners)}"
                )
    return collisions


def plan_variant_export(objects):
    """Return an export plan for plain selection records.

    Each input record has ``name``, ``geometry_signature`` and an ordered
    ``material_slots`` sequence.  Material entries may be names or ``None``.
    Variant material blocks always use etalon names and identify the variant
    material to resolve by slot index.
    """
    groups = {}
    for item in objects:
        object_name = str(item["name"])
        base_name, model_name = _variant_names(object_name)
        record = {
            "name": object_name,
            "model_name": model_name,
            "geometry_signature": tuple(item["geometry_signature"]),
            "material_slots": tuple(item.get("material_slots", ())),
        }
        groups.setdefault(base_name, []).append(record)

    plan = []
    for base_name in sorted(groups):
        records = sorted(groups[base_name], key=lambda record: record["name"])
        etalon = records[0]
        etalon_slots = etalon["material_slots"]
        block_names = tuple(name or "default" for name in etalon_slots) or ("default",)
        warnings = []
        variants = []

        for record in records:
            if record["geometry_signature"] != etalon["geometry_signature"]:
                warnings.append(
                    f"{record['name']}: geometry differs from etalon {etalon['name']}"
                )
            if len(record["material_slots"]) != len(etalon_slots):
                warnings.append(
                    f"{record['name']}: material slot count differs from etalon {etalon['name']}"
                )

            materials = []
            for slot_index, material_name in enumerate(block_names):
                materials.append({
                    "name": material_name,
                    "slot_index": slot_index if etalon_slots else None,
                })

            variants.append({
                "object_name": record["name"],
                "model_name": record["model_name"],
                "texture_prefix": f"{record['model_name']}__",
                "materials": materials,
            })

        plan.append({
            "base_name": base_name,
            "shared_mesh_name": sanitize_id(base_name),
            "etalon_name": etalon["name"],
            "variants": variants,
            "warnings": warnings,
        })

    collisions = _find_output_collisions(plan)
    if collisions:
        raise ValueError(
            "Variant export output names collide after sanitizing: "
            + "; ".join(collisions)
        )

    return plan
