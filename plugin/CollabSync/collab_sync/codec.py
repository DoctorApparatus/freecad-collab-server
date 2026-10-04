"""Encode FreeCAD properties into protocol values and back.

Almost everything goes through FreeCAD's own property serializer
(`dumpPropertyContent` / `restorePropertyContent`), the same code path used
when saving an .FCStd, so every property type round-trips without custom
per-type code. Only Part::Feature shapes (non-parametric geometry, e.g.
imported STEP) are sent as BREP text.
"""

import base64

# Per-user or handled separately.
SKIP_PROPS = {"Visibility", "Label"}
# Computed or runtime-only: every client recomputes these itself.
SKIP_STATUS = {"Output", "Transient"}
# Objects whose Shape is data, not a recompute result.
SHAPE_IS_DATA = {"Part::Feature"}


def property_status(obj, prop):
    try:
        status = obj.getPropertyStatus(prop)
    except Exception:
        return ()
    return status if isinstance(status, (list, tuple)) else ()


# Restore order for a sketch: constraints refer to geometry by index, so the
# geometry must exist first; expressions refer to everything else.
_ORDER = {"Geometry": 0, "ExternalGeo": 0, "ExternalGeometry": 0, "Constraints": 2, "ExpressionEngine": 3}


def apply_order(prop):
    return _ORDER.get(prop, 1)


def is_synced(obj, prop):
    if prop in SKIP_PROPS:
        return False
    try:
        type_id = obj.getTypeIdOfProperty(prop)
    except Exception:
        type_id = ""
    # Shapes are recompute results (Shape, InternalShape, SuppressedShape, …),
    # except the Shape of plain Part::Feature objects, which is data.
    if type_id.startswith("Part::PropertyPartShape") or prop == "Shape":
        return prop == "Shape" and obj.TypeId in SHAPE_IS_DATA
    return not any(s in SKIP_STATUS for s in property_status(obj, prop))


def is_synced_name(prop):
    """Receiver-side guard against computed shapes sent by older clients."""
    return prop not in ("InternalShape", "SuppressedShape")


def encode(obj, prop):
    type_id = obj.getTypeIdOfProperty(prop)
    if prop == "Shape" and type_id.startswith("Part::PropertyPartShape"):
        return {"type_id": type_id, "value": {"enc": "brep", "data": obj.Shape.exportBrepToString()}}
    data = bytes(obj.dumpPropertyContent(prop))
    return {"type_id": type_id, "value": {"enc": "dump", "data": base64.b64encode(data).decode("ascii")}}


def decode_into(obj, prop, pv):
    if prop not in obj.PropertiesList:
        obj.addProperty(pv["type_id"], prop)
    value = pv["value"]
    enc = value.get("enc") if isinstance(value, dict) else None
    if enc == "brep":
        import Part

        shape = Part.Shape()
        shape.importBrepFromString(value["data"])
        setattr(obj, prop, shape)
    elif enc == "dump":
        obj.restorePropertyContent(prop, base64.b64decode(value["data"]))
    else:
        raise ValueError(f"unsupported value encoding {enc!r} for {obj.Name}.{prop}")


def object_state(obj, log=None):
    """`add_object` op carrying the object's full synced state."""
    props = {}
    for prop in obj.PropertiesList:
        if not is_synced(obj, prop):
            continue
        try:
            props[prop] = encode(obj, prop)
        except Exception as e:
            if log:
                log("warning", f"cannot encode {obj.Name}.{prop}: {e}")
    return {
        "op": "add_object",
        "name": obj.Name,
        "type_id": obj.TypeId,
        "label": obj.Label,
        "properties": props,
    }
