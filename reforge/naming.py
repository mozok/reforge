"""Shared bpy-free naming primitives for exported Defold assets."""

import re


_DUPLICATE_SUFFIX_RE = re.compile(r"^(.*)\.(\d{3})$")


def sanitize_id(value):
    """Return a filename-friendly Defold identifier."""
    value = str(value).strip().replace(" ", "_")
    value = "".join(ch for ch in value if ch.isalnum() or ch in "_-")
    return value or "prototype"


def split_duplicate_suffix(object_name):
    """Return ``(base_name, suffix)`` for Blender's ``.001`` name form."""
    object_name = str(object_name)
    match = _DUPLICATE_SUFFIX_RE.match(object_name)
    if not match:
        return object_name, None
    return match.groups()
