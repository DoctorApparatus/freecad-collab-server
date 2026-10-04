"""Tools the model can use on the shared FreeCAD document.

Every function here runs on the main thread (FreeCAD is not thread-safe) and
receives a ToolContext. Changes are picked up by the session's document
observer and synced to everyone like any other edit.
"""

import io
import json
import math
import re
import traceback
from contextlib import redirect_stdout

from collab_sync import datasheet as ds

# PascalCase words joined by optional underscores: BasePlate, BasePlate_Sketch, M3Hole_Pocket.
DEFAULT_NAME_PATTERN = r"^[A-Z][A-Za-z0-9]*(_[A-Z0-9][A-Za-z0-9]*)*$"
GENERIC_NAMES = re.compile(
    r"^(Box|Cube|Cylinder|Sphere|Cone|Torus|Prism|Wedge|Body|Part|Sketch|Pad|Pocket|Extrude|Extrusion|"
    r"Revolution|Groove|Loft|Sweep|Cut|Fusion|Fuse|Common|Fillet|Chamfer|Thickness|Hole|Mirrored|"
    r"LinearPattern|PolarPattern|MultiTransform|Spreadsheet|Sheet|Shape|Object|Feature|Unnamed|Clone|"
    r"Compound|Array|Draft|Line|Wire|Circle|Rectangle|Polygon|ToolFusion)_?\d*$", re.IGNORECASE)
NUMERIC_TYPES = ("Length", "Distance", "Angle", "Float", "Integer", "Quantity", "Area", "Volume",
                 "Percent", "Precision", "Speed", "Acceleration", "Force", "Pressure")
# Properties checked by the audit: dimensions that must come from the datasheet.
DIM_PROPS = {
    "Part::Box": ["Length", "Width", "Height"], "Part::Cylinder": ["Radius", "Height"],
    "Part::Sphere": ["Radius"], "Part::Cone": ["Radius1", "Radius2", "Height"],
    "Part::Torus": ["Radius1", "Radius2"], "Part::Prism": ["Circumradius", "Height"],
    "Part::Extrusion": ["LengthFwd", "LengthRev"],
    "PartDesign::Pad": ["Length", "Length2"], "PartDesign::Pocket": ["Length", "Length2"],
    "PartDesign::Revolution": ["Angle"], "PartDesign::Groove": ["Angle"],
    "PartDesign::Fillet": ["Radius"], "PartDesign::Chamfer": ["Size", "Size2"],
    "PartDesign::Thickness": ["Value"],
    "PartDesign::AdditiveBox": ["Length", "Width", "Height"], "PartDesign::SubtractiveBox": ["Length", "Width", "Height"],
    "PartDesign::AdditiveCylinder": ["Radius", "Height"], "PartDesign::SubtractiveCylinder": ["Radius", "Height"],
    "PartDesign::LinearPattern": ["Length", "Occurrences"], "PartDesign::PolarPattern": ["Angle", "Occurrences"],
}
DIM_CONSTRAINTS = {"Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"}

MAX_RESULT_CHARS = 12000
SKIP_DESCRIBE = {"Proxy", "ExpressionEngine", "Label2", "Visibility", "_Version"}


class ToolError(Exception):
    pass


class Policy:
    """Modelling rules the tools enforce."""

    def __init__(self, datasheet="Datasheet", enforce_names=True, name_pattern=None):
        self.datasheet = datasheet  # None: plain numbers allowed
        self.enforce_names = enforce_names
        self.name_re = re.compile(name_pattern or DEFAULT_NAME_PATTERN)


class ToolContext:
    def __init__(self, doc, can_edit, allow_python, touched, policy=None):
        self.doc = doc
        self.can_edit = can_edit
        self.allow_python = allow_python
        self.touched = touched  # list of names edited this turn (shown as the agent's selection)
        self.policy = policy or Policy()

    @property
    def sheet_name(self):
        return self.policy.datasheet or ds.DEFAULT_NAME

    def params(self):
        return ds.parameter_names(self.doc, self.sheet_name)


def _app():
    import FreeCAD as App

    return App


def _obj(ctx, name):
    obj = ctx.doc.getObject(name)
    if obj is None:
        by_label = [o for o in ctx.doc.Objects if o.Label == name]
        if len(by_label) == 1:
            return by_label[0]
        raise ToolError(f"no object named {name!r}; call get_document_overview for names")
    return obj


def _require_edit(ctx):
    if not ctx.can_edit:
        raise ToolError("I only have viewer access to this document, so I can't change it")


def _touch(ctx, *objs):
    for o in objs:
        if o is not None and o.Name not in ctx.touched:
            ctx.touched.append(o.Name)


# ------------------------------------------------------------------ describing


def _num(x):
    return round(float(x), 4)


def describe_value(v, depth=0):
    """Readable, JSON-friendly rendering of a property value."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return _num(v)
    cls = type(v).__name__
    if cls == "Quantity":
        return v.UserString
    if cls == "Vector":
        return [_num(v.x), _num(v.y), _num(v.z)]
    if cls == "Rotation":
        axis = v.Axis
        return {"axis": [_num(axis.x), _num(axis.y), _num(axis.z)], "angle_deg": _num(math.degrees(v.Angle))}
    if cls == "Placement":
        return {"base": describe_value(v.Base), **describe_value(v.Rotation)}
    if hasattr(v, "Name") and hasattr(v, "TypeId") and hasattr(v, "Document"):
        return v.Name
    if cls in ("Shape", "Solid", "Compound", "Face", "Edge", "Wire", "Shell") or hasattr(v, "BoundBox") and hasattr(v, "Volume"):
        return shape_summary(v)
    if isinstance(v, (list, tuple)):
        if depth > 2:
            return f"[{len(v)} items]"
        items = [describe_value(i, depth + 1) for i in list(v)[:40]]
        if len(v) > 40:
            items.append(f"… {len(v) - 40} more")
        return items
    if isinstance(v, dict):
        return {str(k): describe_value(x, depth + 1) for k, x in list(v.items())[:40]}
    text = str(v)
    return text if len(text) <= 200 else text[:200] + "…"


def shape_summary(shape):
    try:
        if shape.isNull():
            return {"empty": True}
        bb = shape.BoundBox
        out = {
            "bbox_min": [_num(bb.XMin), _num(bb.YMin), _num(bb.ZMin)],
            "bbox_max": [_num(bb.XMax), _num(bb.YMax), _num(bb.ZMax)],
            "solids": len(shape.Solids), "faces": len(shape.Faces), "edges": len(shape.Edges),
        }
        if shape.Solids:
            out["volume_mm3"] = _num(shape.Volume)
            out["area_mm2"] = _num(shape.Area)
        return out
    except Exception as e:  # broken geometry
        return {"error": str(e)}


def _state(obj):
    try:
        state = list(obj.State)
    except Exception:
        state = []
    bad = [s for s in state if s in ("Invalid", "Error", "Touched")]
    if not bad:
        return None
    msg = ""
    try:
        msg = obj.getStatusString()
    except Exception:
        pass
    return f"{', '.join(bad)}{': ' + msg if msg and msg != 'Valid' else ''}"


def _parents(obj):
    try:
        return [p.Name for p in obj.InList]
    except Exception:
        return []


def document_overview(doc, limit=200, sheet_name=ds.DEFAULT_NAME):
    objs = []
    for o in doc.Objects[:limit]:
        entry = {"name": o.Name, "label": o.Label, "type": o.TypeId}
        if hasattr(o, "Shape") and o.TypeId not in ("App::Origin", "App::Line", "App::Plane"):
            try:
                if not o.Shape.isNull():
                    s = shape_summary(o.Shape)
                    entry["bbox"] = [s.get("bbox_min"), s.get("bbox_max")]
                    if "volume_mm3" in s:
                        entry["volume_mm3"] = s["volume_mm3"]
            except Exception:
                pass
        for key in ("Length", "Width", "Height", "Radius", "Angle", "Length2", "Size", "Depth"):
            if key in o.PropertiesList:
                entry[key] = describe_value(getattr(o, key))
        try:
            uses = [d.Name for d in o.OutList if d.TypeId not in ("App::Origin", "App::Line", "App::Plane")]
            if uses:
                entry["uses"] = uses[:20]
        except Exception:
            pass
        problem = _state(o)
        if problem:
            entry["state"] = problem
        objs.append(entry)
    out = {"document": doc.Label, "object_count": len(doc.Objects), "objects": objs,
           "truncated": len(doc.Objects) > limit}
    if sheet_name:
        out["datasheet"] = {"name": sheet_name,
                            "parameters": [{k: p[k] for k in ("name", "value", "description")}
                                           for p in ds.list_parameters(doc, sheet_name)]}
    return out


def recompute_report(ctx):
    ctx.doc.recompute()
    problems = {}
    for o in ctx.doc.Objects:
        p = _state(o)
        if p and "Touched" not in p:
            problems[o.Name] = p
    return problems


def _result(data):
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "… (truncated)"
    return text


# ------------------------------------------------------------------ parameters & names


def _is_numeric_type(type_id):
    return any(k in type_id for k in NUMERIC_TYPES) and not any(
        k in type_id for k in ("List", "Enumeration", "Placement", "Vector", "Link"))


def _dim(ctx, value, what):
    """Resolve a dimension given by the model.

    Datasheet mode: value must be a parameter name or an expression over
    parameters (0 is allowed as a neutral value). Returns (expression or None, float).
    """
    if isinstance(value, bool):
        raise ToolError(f"{what}: expected a dimension, got {value!r}")
    if ctx.policy.datasheet:
        if value in (0, 0.0, "0"):
            return None, 0.0
        if not isinstance(value, str):
            raise ToolError(f"{what}: plain number {value!r} not allowed. Add it to the datasheet with "
                            "set_parameters and pass the parameter name (e.g. \"plate_length\").")
        try:
            expr = ds.to_expression(value, ctx.params(), ctx.sheet_name)
        except ds.DatasheetError as e:
            raise ToolError(f"{what}: {e}") from None
        return expr, _evaluate(ctx, expr)
    if isinstance(value, (int, float)):
        return None, float(value)
    params = ctx.params()
    try:
        expr = ds.to_expression(value, params, ctx.sheet_name, require_param=bool(params))
    except ds.DatasheetError:
        return None, float(_app().Units.Quantity(value).Value)
    return expr, _evaluate(ctx, expr)


def _evaluate(ctx, expr):
    sheet = ds.get_sheet(ctx.doc, ctx.sheet_name)
    try:
        v = sheet.evalExpression(expr)
    except Exception:
        try:  # bare "Datasheet.alias"
            v = sheet.get(expr.split(".", 1)[1])
        except Exception:
            return 10.0  # recompute fixes the value through the expression
    return float(getattr(v, "Value", v))


def _set_dim(ctx, obj, path, value, what=None):
    expr, number = _dim(ctx, value, what or f"{obj.Name}.{path}")
    if expr:
        obj.setExpression(path, expr)
    elif "." not in path:
        setattr(obj, path, number)
    return number


def _humanize(name):
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").split()
    return " ".join([words[0]] + [w if w.isupper() else w.lower() for w in words[1:]]) if words else name


def _check_new_name(ctx, name, default):
    if not name:
        if ctx.policy.enforce_names:
            raise ToolError("name is required: give a descriptive PascalCase name like BasePlate or "
                            "BasePlate_Sketch (what it is, not what type it is)")
        return default
    if ctx.policy.enforce_names:
        if not ctx.policy.name_re.match(name):
            raise ToolError(f"name {name!r} doesn't follow the naming rule {ctx.policy.name_re.pattern}")
        if GENERIC_NAMES.match(name):
            raise ToolError(f"name {name!r} is too generic; say what the part is (e.g. MountingHole_Pocket)")
    elif not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ToolError(f"name {name!r} must be letters, digits and underscores")
    if ctx.doc.getObject(name) is not None:
        raise ToolError(f"an object named {name!r} already exists; pick another name")
    return name


def _new_object(ctx, type_id, name, label=None, body=None):
    final = _check_new_name(ctx, name, type_id.split("::")[-1])
    obj = body.newObject(type_id, final) if body is not None else ctx.doc.addObject(type_id, final)
    if ctx.policy.enforce_names and obj.Name != final:
        ctx.doc.removeObject(obj.Name)
        raise ToolError(f"could not use the name {final!r}; pick another name")
    obj.Label = label or _humanize(obj.Name)
    _touch(ctx, obj)
    return obj


# ------------------------------------------------------------------ value conversion


def _vec(v):
    App = _app()
    if isinstance(v, dict):
        v = [v.get("x", 0), v.get("y", 0), v.get("z", 0)]
    if not (isinstance(v, (list, tuple)) and len(v) == 3):
        raise ToolError(f"expected [x, y, z], got {v!r}")
    return App.Vector(*[float(x) for x in v])


def _placement(v):
    App = _app()
    if not isinstance(v, dict):
        raise ToolError('placement must look like {"base": [x,y,z], "axis": [0,0,1], "angle_deg": 0}')
    base = _vec(v.get("base", [0, 0, 0]))
    if "ypr_deg" in v:
        y, p, r = [float(x) for x in v["ypr_deg"]]
        rot = App.Rotation(y, p, r)
    else:
        rot = App.Rotation(_vec(v.get("axis", [0, 0, 1])), float(v.get("angle_deg", 0)))
    return App.Placement(base, rot)


def convert_value(obj, prop, value):
    """Turn JSON from the model into what the property expects."""
    type_id = obj.getTypeIdOfProperty(prop)
    if "PropertyPlacement" in type_id:
        return _placement(value)
    if "PropertyVector" in type_id and "List" not in type_id:
        return _vec(value)
    if type_id in ("App::PropertyLink", "App::PropertyLinkChild", "App::PropertyLinkGlobal", "App::PropertyLinkHidden"):
        return None if value in (None, "") else _obj_by(obj.Document, value)
    if type_id.startswith("App::PropertyLinkList"):
        return [_obj_by(obj.Document, n) for n in value]
    if type_id.startswith("App::PropertyLinkSub") and "List" not in type_id:
        if value in (None, ""):
            return None
        name, subs = value[0], value[1] if len(value) > 1 else []
        return (_obj_by(obj.Document, name), list(subs))
    if isinstance(value, str) and any(k in type_id for k in ("Length", "Distance", "Angle", "Quantity", "Area",
                                                               "Volume", "Speed", "Acceleration", "Force")):
        return _app().Units.Quantity(value)
    return value


def _obj_by(doc, name):
    o = doc.getObject(name)
    if o is None:
        matches = [x for x in doc.Objects if x.Label == name]
        if len(matches) != 1:
            raise ToolError(f"no object named {name!r}")
        o = matches[0]
    return o


def _set_placement(ctx, obj, prop, value):
    App = _app()
    if not isinstance(value, dict):
        raise ToolError('placement must look like {"base": [x,y,z], "axis": [0,0,1], "angle_deg": a}')
    axis = value.get("axis", [0, 0, 1])
    if not (isinstance(axis, (list, tuple)) and len(axis) == 3 and all(isinstance(a, (int, float)) for a in axis)):
        raise ToolError("axis is a direction like [0,0,1] (plain numbers)")
    base = value.get("base", [0, 0, 0])
    if not (isinstance(base, (list, tuple)) and len(base) == 3):
        raise ToolError("base must be [x, y, z]")
    angle = value.get("angle_deg", 0)
    resolved = [_dim(ctx, c, f"{obj.Name}.{prop}.Base.{k}") for c, k in zip(base, "xyz")]
    a_expr, a_val = _dim(ctx, angle, f"{obj.Name}.{prop}.Angle")
    setattr(obj, prop, App.Placement(App.Vector(*[r[1] for r in resolved]),
                                     App.Rotation(App.Vector(*[float(a) for a in axis]), a_val)))
    for (expr, _), k in zip(resolved, "xyz"):
        if expr:
            obj.setExpression(f"{prop}.Base.{k}", expr)
    if a_expr:
        obj.setExpression(f"{prop}.Rotation.Angle", a_expr)


def _apply_properties(ctx, obj, props):
    applied, errors = [], {}
    for prop, value in (props or {}).items():
        try:
            if prop == "Label":
                obj.Label = str(value)
            elif prop not in obj.PropertiesList:
                raise ToolError(f"{obj.TypeId} has no property {prop}")
            else:
                type_id = obj.getTypeIdOfProperty(prop)
                if "PropertyPlacement" in type_id:
                    _set_placement(ctx, obj, prop, value)
                elif "PropertyVector" in type_id and "List" not in type_id:
                    if not (isinstance(value, (list, tuple)) and len(value) == 3):
                        raise ToolError("expected [x, y, z]")
                    App = _app()
                    res = [_dim(ctx, c, f"{obj.Name}.{prop}.{k}") for c, k in zip(value, "xyz")]
                    setattr(obj, prop, App.Vector(*[r[1] for r in res]))
                    for (expr, _), k in zip(res, "xyz"):
                        if expr:
                            obj.setExpression(f"{prop}.{k}", expr)
                elif _is_numeric_type(type_id):
                    _set_dim(ctx, obj, prop, value)
                else:
                    setattr(obj, prop, convert_value(obj, prop, value))
            applied.append(prop)
        except ToolError as e:
            errors[prop] = str(e)
        except Exception as e:
            errors[prop] = f"{type(e).__name__}: {e}"
    return applied, errors


# ------------------------------------------------------------------ tools


def t_get_document_overview(ctx):
    return _result(document_overview(ctx.doc, sheet_name=ctx.sheet_name))


def t_get_object(ctx, name):
    o = _obj(ctx, name)
    props = {}
    for p in o.PropertiesList:
        if p in SKIP_DESCRIBE:
            continue
        try:
            props[p] = {"type": o.getTypeIdOfProperty(p).replace("App::Property", ""),
                        "value": describe_value(getattr(o, p))}
        except Exception as e:
            props[p] = {"error": str(e)}
    out = {"name": o.Name, "label": o.Label, "type": o.TypeId, "properties": props,
           "used_by": _parents(o)}
    try:
        exprs = o.ExpressionEngine
        if exprs:
            out["expressions"] = {path: expr for path, expr in exprs}
    except Exception:
        pass
    if o.TypeId == "Sketcher::SketchObject":
        bound = _bound_paths(o)
        out["constraints"] = [
            {"index": i, "type": c.Type, "name": c.Name or None,
             "value": _num(math.degrees(c.Value)) if c.Type == "Angle" else _num(c.Value),
             "bound": f"Constraints[{i}]" in bound or bool(c.Name and f"Constraints.{c.Name}" in bound)}
            for i, c in enumerate(o.Constraints) if c.Type in DIM_CONSTRAINTS]
    problem = _state(o)
    if problem:
        out["state"] = problem
    return _result(out)


def t_create_object(ctx, type_id, name=None, label=None, properties=None, body=None):
    _require_edit(ctx)
    if type_id == "Spreadsheet::Sheet":
        raise ToolError(f"use set_parameters; all values live in the {ctx.sheet_name} spreadsheet")
    b = None
    if body:
        b = _obj(ctx, body)
        if b.TypeId != "PartDesign::Body":
            raise ToolError(f"{body} is not a PartDesign body")
    elif type_id.startswith("PartDesign::") and type_id != "PartDesign::Body":
        raise ToolError("PartDesign features must be created inside a body: pass body=<name>")
    obj = _new_object(ctx, type_id, name, label, b)
    applied, errors = _apply_properties(ctx, obj, properties)
    if errors:  # all or nothing: don't leave half-configured objects behind
        ctx.doc.removeObject(obj.Name)
        ctx.touched.remove(obj.Name)
        raise ToolError(f"{type_id} not created: {json.dumps(errors, ensure_ascii=False)}")
    problems = recompute_report(ctx)
    return _result({"created": obj.Name, "label": obj.Label, "applied": applied, "errors": errors,
                    "problems": problems})


def t_set_properties(ctx, name, properties):
    _require_edit(ctx)
    o = _obj(ctx, name)
    applied, errors = _apply_properties(ctx, o, properties)
    _touch(ctx, o)
    if errors:
        recompute_report(ctx)
        raise ToolError(f"not applied: {json.dumps(errors, ensure_ascii=False)}"
                        + (f"; applied: {', '.join(applied)}" if applied else ""))
    return _result({"object": o.Name, "applied": applied, "errors": errors, "problems": recompute_report(ctx)})


def t_set_expression(ctx, name, property, expression=None):
    _require_edit(ctx)
    o = _obj(ctx, name)
    if expression and ctx.policy.datasheet:
        try:
            expression = ds.to_expression(expression, ctx.params(), ctx.sheet_name)
        except ds.DatasheetError as e:
            raise ToolError(str(e)) from None
    try:
        o.setExpression(property, expression or None)
    except Exception as e:
        raise ToolError(f"cannot set expression: {e}") from None
    _touch(ctx, o)
    return _result({"object": o.Name, "property": property, "expression": expression,
                    "problems": recompute_report(ctx)})


def t_delete_objects(ctx, names):
    _require_edit(ctx)
    deleted, errors = [], {}
    for n in names:
        try:
            o = _obj(ctx, n)
            ctx.doc.removeObject(o.Name)
            deleted.append(n)
        except Exception as e:
            errors[n] = str(e)
    return _result({"deleted": deleted, "errors": errors, "problems": recompute_report(ctx)})


def t_create_body(ctx, name=None, label=None):
    _require_edit(ctx)
    b = _new_object(ctx, "PartDesign::Body", name, label)
    ctx.doc.recompute()
    return _result({"created": b.Name, "label": b.Label})


_PLANES = {"XY": ("XY_Plane", (0, 0, 1), None), "XZ": ("XZ_Plane", (1, 0, 0), 90), "YZ": ("YZ_Plane", (1, 1, 1), 120)}


def _attach_sketch(ctx, sketch, body, plane, offset):
    App = _app()
    if isinstance(plane, dict):  # face of an existing feature
        target = _obj(ctx, plane["object"])
        support = [(target, plane.get("face", "Face1"))]
        mode = "FlatFace"
    else:
        key = str(plane or "XY").upper()
        if key not in _PLANES:
            raise ToolError('plane must be "XY", "XZ", "YZ" or {"object": name, "face": "Face6"}')
        role, axis, angle = _PLANES[key]
        if body is None:
            rot = App.Rotation() if angle is None else App.Rotation(App.Vector(*axis), angle)
            sketch.Placement = App.Placement(App.Vector(), rot)
            if offset:
                normal = rot.multVec(App.Vector(0, 0, 1))
                expr, dist = _dim(ctx, offset, f"{sketch.Name} offset")
                sketch.Placement = App.Placement(normal * dist, rot)
                for k, comp in zip("xyz", (normal.x, normal.y, normal.z)):
                    if expr and abs(comp) > 1e-9:
                        sketch.setExpression(f"Placement.Base.{k}", f"({expr}) * {round(comp, 9)}")
            return
        feature = next((f for f in body.Origin.OriginFeatures if getattr(f, "Role", "") == role), None)
        if feature is None:
            raise ToolError(f"body {body.Name} has no {role}")
        support = [(feature, "")]
        mode = "FlatFace"
    prop = "AttachmentSupport" if "AttachmentSupport" in sketch.PropertiesList else "Support"
    setattr(sketch, prop, support)
    sketch.MapMode = mode
    if offset:
        expr, dist = _dim(ctx, offset, f"{sketch.Name} offset")
        sketch.AttachmentOffset = App.Placement(App.Vector(0, 0, dist), App.Rotation())
        if expr:
            sketch.setExpression("AttachmentOffset.Base.z", expr)


def _constrain(sketch, constraint, expr):
    i = sketch.addConstraint(constraint)
    if expr:
        sketch.setExpression(f"Constraints[{i}]", expr)
    return i


def _fix_point(ctx, sketch, geo, pos, xy, what):
    """Pin a sketch point to the origin by DistanceX/DistanceY bound to parameters."""
    import Sketcher

    (ex, x), (ey, y) = [_dim(ctx, c, f"{what} {k}") for c, k in zip(xy, "xy")]
    if not ex and x == 0 and not ey and y == 0:
        sketch.addConstraint(Sketcher.Constraint("Coincident", -1, 1, geo, pos))
        return x, y
    if not ex and x == 0:
        sketch.addConstraint(Sketcher.Constraint("PointOnObject", geo, pos, -2))
    else:
        _constrain(sketch, Sketcher.Constraint("DistanceX", -1, 1, geo, pos, x), ex)
    if not ey and y == 0:
        sketch.addConstraint(Sketcher.Constraint("PointOnObject", geo, pos, -1))
    else:
        _constrain(sketch, Sketcher.Constraint("DistanceY", -1, 1, geo, pos, y), ey)
    return x, y


def _point(ctx, xy, what):
    if not (isinstance(xy, (list, tuple)) and len(xy) == 2):
        raise ToolError(f"{what}: expected [x, y]")
    return [_dim(ctx, c, f"{what} {k}") for c, k in zip(xy, "xy")]


def _add_geometry(ctx, sketch, geometry, constrain):
    import Part
    import Sketcher

    App = _app()
    V = lambda x, y: App.Vector(x, y, 0)  # noqa: E731
    count = 0
    for n, g in enumerate(geometry):
        kind, what = g.get("type"), f"geometry[{n}]"
        if kind == "line":
            (_, x1), (_, y1) = _point(ctx, g["start"], f"{what}.start")
            (_, x2), (_, y2) = _point(ctx, g["end"], f"{what}.end")
            i = sketch.addGeometry(Part.LineSegment(V(x1, y1), V(x2, y2)), False)
            if constrain:
                _fix_point(ctx, sketch, i, 1, g["start"], f"{what}.start")
                _fix_point(ctx, sketch, i, 2, g["end"], f"{what}.end")
            count += 1
        elif kind in ("circle", "arc"):
            (_, cx), (_, cy) = _point(ctx, g["center"], f"{what}.center")
            r_expr, r = _dim(ctx, g["radius"], f"{what}.radius")
            if kind == "circle":
                i = sketch.addGeometry(Part.Circle(V(cx, cy), App.Vector(0, 0, 1), r), False)
            else:
                _, a0 = _dim(ctx, g["start_angle_deg"], f"{what}.start_angle_deg")
                _, a1 = _dim(ctx, g["end_angle_deg"], f"{what}.end_angle_deg")
                c = Part.Circle(V(cx, cy), App.Vector(0, 0, 1), r)
                i = sketch.addGeometry(Part.ArcOfCircle(c, math.radians(a0), math.radians(a1)), False)
            if constrain:
                _fix_point(ctx, sketch, i, 3, g["center"], f"{what}.center")
                _constrain(sketch, Sketcher.Constraint("Radius", i, r), r_expr)
            count += 1
        elif kind in ("rectangle", "polygon"):
            if kind == "rectangle":
                (ex, x), (ey, y) = _point(ctx, g["corner"], f"{what}.corner")
                w_expr, w = _dim(ctx, g["width"], f"{what}.width")
                h_expr, h = _dim(ctx, g["height"], f"{what}.height")
                pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            else:
                if len(g.get("points", [])) < 3:
                    raise ToolError(f"{what}: a polygon needs at least 3 points")
                resolved = [_point(ctx, p, f"{what}.points[{k}]") for k, p in enumerate(g["points"])]
                pts = [(px[1], py[1]) for px, py in resolved]
            first, m = sketch.GeometryCount, len(pts)
            for k in range(m):
                sketch.addGeometry(Part.LineSegment(V(*pts[k]), V(*pts[(k + 1) % m])), False)
            if constrain:
                for k in range(m):
                    sketch.addConstraint(Sketcher.Constraint("Coincident", first + k, 2, first + (k + 1) % m, 1))
                if kind == "rectangle":
                    for k, t in enumerate(("Horizontal", "Vertical", "Horizontal", "Vertical")):
                        sketch.addConstraint(Sketcher.Constraint(t, first + k))
                    _constrain(sketch, Sketcher.Constraint("DistanceX", first, 1, first, 2, w), w_expr)
                    _constrain(sketch, Sketcher.Constraint("DistanceY", first + 1, 1, first + 1, 2, h), h_expr)
                    _fix_point(ctx, sketch, first, 1, g["corner"], f"{what}.corner")
                else:
                    for k, p in enumerate(g["points"]):
                        _fix_point(ctx, sketch, first + k, 1, p, f"{what}.points[{k}]")
            count += m
        else:
            raise ToolError(f"unknown geometry type {kind!r}")
    return count


def t_create_sketch(ctx, geometry, name=None, plane="XY", body=None, offset=0, label=None, constrain=True):
    _require_edit(ctx)
    if ctx.policy.datasheet and not constrain:
        raise ToolError("sketches must be constrained so their dimensions follow the datasheet")
    b = _obj(ctx, body) if body else None
    sketch = _new_object(ctx, "Sketcher::SketchObject", name, label, b)
    try:
        _attach_sketch(ctx, sketch, b, plane, offset)
        n = _add_geometry(ctx, sketch, geometry, constrain)
    except Exception:
        ctx.doc.removeObject(sketch.Name)
        ctx.touched.remove(sketch.Name)
        raise
    problems = recompute_report(ctx)
    return _result({"created": sketch.Name, "geometry_added": n, "problems": problems})


def _set_side(feat, midplane):
    """FreeCAD 1.1 replaced Midplane with the SideType enumeration."""
    if "SideType" in feat.PropertiesList:
        if midplane:
            options = feat.getEnumerationsOfProperty("SideType") or []
            feat.SideType = next((o for o in options if "ymmetric" in o), "Symmetric to plane")
        return
    feat.Midplane = bool(midplane)


def _body_of(obj):
    try:
        parent = obj.getParentGeoFeatureGroup()
    except Exception:
        return None
    return parent if parent is not None and parent.TypeId == "PartDesign::Body" else None


def t_extrude_sketch(ctx, sketch, length, name=None, operation="pad", reversed=False, midplane=False,
                     label=None):
    _require_edit(ctx)
    s = _obj(ctx, sketch)
    body = _body_of(s)
    op = operation.lower()
    _dim(ctx, length, "length")  # validate before creating anything
    if body is not None:
        if op not in ("pad", "pocket"):
            raise ToolError("inside a body use operation 'pad' or 'pocket'")
        feat = _new_object(ctx, "PartDesign::Pad" if op == "pad" else "PartDesign::Pocket", name, label, body)
        feat.Profile = s
        _set_dim(ctx, feat, "Length", length)
        feat.Reversed = bool(reversed)
        _set_side(feat, midplane)
    else:
        if op == "pocket":
            raise ToolError("pockets need the sketch to be inside a PartDesign body; use boolean cut instead")
        feat = _new_object(ctx, "Part::Extrusion", name, label)
        feat.Base = s
        feat.DirMode = "Normal"
        _set_dim(ctx, feat, "LengthFwd", length)
        feat.Solid = True
        feat.Reversed = bool(reversed)
        feat.Symmetric = bool(midplane)
    try:
        s.Visibility = False
    except Exception:
        pass
    _touch(ctx, feat)
    problems = recompute_report(ctx)
    shape = shape_summary(feat.Shape) if hasattr(feat, "Shape") else {}
    return _result({"created": feat.Name, "type": feat.TypeId, "shape": shape, "problems": problems})


def t_boolean(ctx, operation, base, tools, name=None, label=None):
    _require_edit(ctx)
    op = operation.lower()
    b = _obj(ctx, base)
    ts = [_obj(ctx, t) for t in tools]
    if not ts:
        raise ToolError("tools must name at least one object")
    if op not in ("cut", "fuse", "union", "common", "intersect"):
        raise ToolError("operation must be cut, fuse or common")
    if op == "cut":
        feat = _new_object(ctx, "Part::Cut", name, label)
        if len(ts) == 1:
            feat.Base, feat.Tool = b, ts[0]
        else:
            fuse = _new_object(ctx, "Part::MultiFuse", f"{feat.Name}_Tools" if name else None, None)
            fuse.Shapes = ts
            feat.Base, feat.Tool = b, fuse
    elif op in ("fuse", "union"):
        feat = _new_object(ctx, "Part::MultiFuse", name, label)
        feat.Shapes = [b] + ts
    else:
        feat = _new_object(ctx, "Part::MultiCommon", name, label)
        feat.Shapes = [b] + ts
    _touch(ctx, feat)
    problems = recompute_report(ctx)
    return _result({"created": feat.Name, "shape": shape_summary(feat.Shape), "problems": problems})


def t_dress_up(ctx, kind, object, size, name=None, edges="all", label=None):
    _require_edit(ctx)
    k = kind.lower()
    if k not in ("fillet", "chamfer"):
        raise ToolError("kind must be fillet or chamfer")
    o = _obj(ctx, object)
    _dim(ctx, size, "size")
    shape = o.Shape
    if edges == "all":
        names = [f"Edge{i + 1}" for i in range(len(shape.Edges))]
    else:
        names = list(edges)
    body = _body_of(o)
    if body is not None:
        feat = _new_object(ctx, "PartDesign::Fillet" if k == "fillet" else "PartDesign::Chamfer", name, label, body)
        feat.Base = (o, names)
        _set_dim(ctx, feat, "Radius" if k == "fillet" else "Size", size)
    else:
        if ctx.policy.datasheet:
            raise ToolError("fillets/chamfers outside a PartDesign body can't follow the datasheet; "
                            "build the part in a body (create_body) instead")
        _, r = _dim(ctx, size, "size")
        feat = _new_object(ctx, "Part::Fillet" if k == "fillet" else "Part::Chamfer", name, label)
        feat.Base = o
        feat.Edges = [(int(n.replace("Edge", "")), r, r) for n in names]
        try:
            o.Visibility = False
        except Exception:
            pass
    _touch(ctx, feat)
    problems = recompute_report(ctx)
    return _result({"created": feat.Name, "edges": len(names), "problems": problems})


def t_run_python(ctx, code):
    _require_edit(ctx)
    if not ctx.allow_python:
        raise ToolError("running Python is disabled on this agent (COLLAB_AGENT_ALLOW_PYTHON=0)")
    App = _app()
    import Part  # noqa: F401

    before = {o.Name for o in ctx.doc.Objects}
    env = {"App": App, "FreeCAD": App, "doc": ctx.doc, "Part": Part, "__name__": "collab_agent_exec"}
    for mod in ("Sketcher", "Draft"):
        try:
            env[mod] = __import__(mod)
        except Exception:
            pass
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(code, "<agent>", "exec"), env)  # noqa: S102 - opt-in, documented
    except Exception:
        return _result({"error": traceback.format_exc(limit=3), "output": buf.getvalue()[-4000:]})
    new = [n for n in (o.Name for o in ctx.doc.Objects) if n not in before]
    for n in new:
        _touch(ctx, ctx.doc.getObject(n))
    problems = recompute_report(ctx)
    return _result({"output": buf.getvalue()[-4000:], "new_objects": new, "problems": problems,
                    "policy_issues": audit(ctx, new)})


# ------------------------------------------------------------------ datasheet tools


def t_get_parameters(ctx):
    params = ds.list_parameters(ctx.doc, ctx.sheet_name)
    users = {}
    for o in ctx.doc.Objects:
        for _, expr in _expressions(o):
            for p in params:
                if f"{ctx.sheet_name}.{p['name']}" in expr:
                    users.setdefault(p["name"], set()).add(o.Label)
    for p in params:
        p["used_by"] = sorted(users.get(p["name"], []))
    return _result({"datasheet": ctx.sheet_name, "parameters": params})


def t_set_parameters(ctx, parameters):
    _require_edit(ctx)
    if not isinstance(parameters, list) or not parameters:
        raise ToolError('parameters: a list like [{"name": "plate_length", "value": "40 mm", "description": "…"}]')
    written, errors = ds.set_parameters(ctx.doc, parameters, ctx.sheet_name)
    sheet = ds.get_sheet(ctx.doc, ctx.sheet_name)
    if sheet is not None:
        _touch(ctx, sheet)
    return _result({"written": written, "errors": errors, "problems": recompute_report(ctx)})


def t_import_parameters_csv(ctx, csv_text):
    _require_edit(ctx)
    rows = ds.parse_csv(csv_text)
    if not rows:
        raise ToolError("no rows found; expected name,value[,description] lines")
    written, errors = ds.set_parameters(ctx.doc, rows, ctx.sheet_name)
    _touch(ctx, ds.get_sheet(ctx.doc, ctx.sheet_name))
    return _result({"imported": written, "errors": errors, "problems": recompute_report(ctx)})


def t_export_parameters_csv(ctx):
    return ds.export_csv(ctx.doc, ctx.sheet_name)


def t_rename_object(ctx, name, label):
    _require_edit(ctx)
    o = _obj(ctx, name)
    label = str(label).strip()
    if not label:
        raise ToolError("label must not be empty")
    if ctx.policy.enforce_names and GENERIC_NAMES.match(label.replace(" ", "")):
        raise ToolError(f"label {label!r} is too generic; say what the part is")
    o.Label = label
    _touch(ctx, o)
    return _result({"object": o.Name, "label": o.Label})


def _quantity_text(value, type_id=""):
    if isinstance(value, bool):
        raise ToolError("not a dimension")
    num = float(getattr(value, "Value", value))
    unit = "deg" if "Angle" in type_id else ("mm" if any(k in type_id for k in ("Length", "Distance")) else "")
    return f"{round(num, 6):g} {unit}".strip()


def t_bind_to_parameter(ctx, object, property, parameter, value=None, description=None):
    """Move a literal dimension into the datasheet and drive it from there."""
    _require_edit(ctx)
    o = _obj(ctx, object)
    path = property.lstrip(".")
    if value is None and parameter not in ctx.params():
        m = re.match(r"^Constraints\[(\d+)\]$", path)
        if m:
            c = o.Constraints[int(m.group(1))]
            value = f"{round(math.degrees(c.Value), 6):g} deg" if c.Type == "Angle" else f"{round(c.Value, 6):g} mm"
        elif path.startswith("Placement.Base."):
            value = f"{round(getattr(o.Placement.Base, path[-1]), 6):g} mm"
        elif path == "Placement.Rotation.Angle":
            value = f"{round(math.degrees(o.Placement.Rotation.Angle), 6):g} deg"
        elif path in o.PropertiesList:
            value = _quantity_text(getattr(o, path), o.getTypeIdOfProperty(path))
        else:
            raise ToolError(f"{o.Name} has no {property}; give value explicitly")
    if value is not None:
        written, errors = ds.set_parameters(
            ctx.doc, [{"name": parameter, "value": str(value), "description": description}], ctx.sheet_name)
        if errors:
            raise ToolError(f"cannot write parameter: {errors}")
    try:
        o.setExpression(path, f"{ctx.sheet_name}.{parameter}")
    except Exception as e:
        raise ToolError(f"cannot bind {o.Name}.{path}: {e}") from None
    _touch(ctx, o)
    return _result({"object": o.Name, "property": path, "parameter": parameter,
                    "problems": recompute_report(ctx)})


def _expressions(o):
    try:
        return list(o.ExpressionEngine or [])
    except Exception:
        return []


def _bound_paths(o):
    return {path.lstrip(".") for path, _ in _expressions(o)}


def _attached(o):
    try:
        if getattr(o, "MapMode", "Deactivated") != "Deactivated":
            return True
    except Exception:
        pass
    return False


SKIP_AUDIT = {"App::Origin", "App::Line", "App::Plane", "App::Point", "Spreadsheet::Sheet", "App::DocumentObjectGroup"}


def audit(ctx, names=None):
    """Policy violations: dimensions not driven by the datasheet, generic names."""
    objs = [ctx.doc.getObject(n) for n in names] if names else list(ctx.doc.Objects)
    issues = []
    for o in objs:
        if o is None or o.TypeId in SKIP_AUDIT:
            continue
        try:
            if ctx.policy.enforce_names and (GENERIC_NAMES.match(o.Label.replace(" ", ""))
                                             or GENERIC_NAMES.match(o.Label)):
                issues.append({"object": o.Name, "label": o.Label, "issue": "generic label",
                               "fix": "rename_object with a descriptive label"})
            if not ctx.policy.datasheet:
                continue
            bound = _bound_paths(o)
            for prop in DIM_PROPS.get(o.TypeId, []):
                if prop in o.PropertiesList and prop not in bound:
                    v = getattr(o, prop)
                    if not isinstance(v, bool) and float(getattr(v, "Value", v)) != 0:
                        issues.append({"object": o.Name, "property": prop, "value": describe_value(v),
                                       "issue": "dimension not from datasheet", "fix": "bind_to_parameter"})
            if o.TypeId == "Sketcher::SketchObject":
                for i, c in enumerate(o.Constraints):
                    if c.Type in DIM_CONSTRAINTS and getattr(c, "Driving", True) \
                            and f"Constraints[{i}]" not in bound and not (c.Name and f"Constraints.{c.Name}" in bound):
                        issues.append({"object": o.Name, "property": f"Constraints[{i}]", "constraint": c.Type,
                                       "issue": "sketch dimension not from datasheet", "fix": "bind_to_parameter"})
            if "Placement" in o.PropertiesList and not _attached(o) and \
                    (not o.TypeId.startswith("PartDesign::") or o.TypeId == "PartDesign::Body"):
                pl = o.Placement
                for k in "xyz":
                    if abs(getattr(pl.Base, k)) > 1e-9 and f"Placement.Base.{k}" not in bound:
                        issues.append({"object": o.Name, "property": f"Placement.Base.{k}",
                                       "issue": "position not from datasheet", "fix": "bind_to_parameter"})
                if abs(pl.Rotation.Angle) > 1e-9 and "Placement.Rotation.Angle" not in bound:
                    issues.append({"object": o.Name, "property": "Placement.Rotation.Angle",
                                   "issue": "rotation not from datasheet", "fix": "bind_to_parameter"})
        except Exception:
            continue  # objects we can't inspect are not reported
    return issues


def t_audit_model(ctx, objects=None):
    issues = audit(ctx, objects)
    return _result({"ok": not issues, "issues": issues[:200], "total": len(issues)})


# ------------------------------------------------------------------ registry

_STR = {"type": "string"}
_NUM = {"type": "number"}
_DIM = {"type": ["string", "number"],
        "description": "datasheet parameter name or expression over parameters, e.g. \"plate_length\" or \"plate_length / 2\""}
_NAME = {"type": "string", "description": "descriptive PascalCase name, e.g. BasePlate or BasePlate_Sketch"}
_LABEL = {"type": "string", "description": "human-readable label, e.g. \"Base plate\""}

TOOLS = [
    ("get_document_overview", t_get_document_overview,
     "Every object with type, key dimensions, bounding box, what it uses and errors, plus the datasheet.",
     {"type": "object", "properties": {}}),
    ("get_object", t_get_object,
     "All properties, expressions and geometry facts of one object (by internal name or unique label). "
     "Sketch constraints are listed with their index for bind_to_parameter (Constraints[i]).",
     {"type": "object", "properties": {"name": _STR}, "required": ["name"]}),
    ("get_parameters", t_get_parameters,
     "List the datasheet parameters with values, descriptions and which objects use them.",
     {"type": "object", "properties": {}}),
    ("set_parameters", t_set_parameters,
     "Create or update datasheet parameters. name: snake_case; value: \"40 mm\", \"90 deg\", \"4\" or a "
     "formula over other parameters like \"=plate_length / 2\"; description: what it controls.",
     {"type": "object", "properties": {"parameters": {"type": "array", "items": {
         "type": "object", "properties": {"name": _STR, "value": _STR, "description": _STR},
         "required": ["name", "value"]}}}, "required": ["parameters"]}),
    ("import_parameters_csv", t_import_parameters_csv,
     "Import parameters from CSV text (columns name,value[,unit][,description]; header optional).",
     {"type": "object", "properties": {"csv_text": _STR}, "required": ["csv_text"]}),
    ("export_parameters_csv", t_export_parameters_csv,
     "Return the datasheet as CSV text (name,value,description).",
     {"type": "object", "properties": {}}),
    ("create_object", t_create_object,
     "Create a FreeCAD object by type id, e.g. Part::Box, Part::Cylinder, Part::Sphere, Part::Cone, App::Part. "
     "PartDesign features need body=<name>. Dimensions are parameter names/expressions. Placement: "
     "{\"base\": [x,y,z], \"axis\": [0,0,1], \"angle_deg\": a} with x/y/z/a as parameters (0 allowed).",
     {"type": "object", "properties": {
         "type_id": _STR, "name": _NAME, "label": _LABEL, "body": _STR,
         "properties": {"type": "object", "description": "property name -> value"}},
      "required": ["type_id", "name"]}),
    ("set_properties", t_set_properties,
     "Change properties of an object. Dimensions are parameter names/expressions; links take object names.",
     {"type": "object", "properties": {"name": _STR, "properties": {"type": "object"}},
      "required": ["name", "properties"]}),
    ("set_expression", t_set_expression,
     "Bind a property to an expression over datasheet parameters (e.g. 'plate_length * 2'), or clear it with null.",
     {"type": "object", "properties": {"name": _STR, "property": _STR,
                                        "expression": {"type": ["string", "null"]}},
      "required": ["name", "property"]}),
    ("bind_to_parameter", t_bind_to_parameter,
     "Drive an existing value from the datasheet: property like 'Length', 'Placement.Base.x' or "
     "'Constraints[3]' (sketch). Creates the parameter from the current value unless value is given.",
     {"type": "object", "properties": {"object": _STR, "property": _STR, "parameter": _STR,
                                        "value": _STR, "description": _STR},
      "required": ["object", "property", "parameter"]}),
    ("rename_object", t_rename_object,
     "Give an existing object a descriptive label (internal names can't change after creation).",
     {"type": "object", "properties": {"name": _STR, "label": _LABEL}, "required": ["name", "label"]}),
    ("audit_model", t_audit_model,
     "Find dimensions not driven by the datasheet and generic names, for given objects or the whole model.",
     {"type": "object", "properties": {"objects": {"type": "array", "items": _STR}}}),
    ("delete_objects", t_delete_objects,
     "Delete objects by name. Only delete what the user asked for.",
     {"type": "object", "properties": {"names": {"type": "array", "items": _STR}}, "required": ["names"]}),
    ("create_body", t_create_body,
     "Create an empty PartDesign body to hold sketches and features for one solid part.",
     {"type": "object", "properties": {"name": _NAME, "label": _LABEL}, "required": ["name"]}),
    ("create_sketch", t_create_sketch,
     "Create a fully constrained sketch on plane XY/XZ/YZ (or {\"object\": name, \"face\": \"Face6\"}). "
     "All coordinates and sizes are parameters/expressions (0 allowed). Geometry items: "
     "{type: line, start:[x,y], end:[x,y]}, {type: circle, center:[x,y], radius}, "
     "{type: arc, center, radius, start_angle_deg, end_angle_deg}, {type: rectangle, corner:[x,y], width, height}, "
     "{type: polygon, points:[[x,y],...]}. Closed profiles are needed for pads/pockets.",
     {"type": "object", "properties": {
         "name": _NAME, "label": _LABEL, "geometry": {"type": "array", "items": {"type": "object"}},
         "plane": {"type": ["string", "object"]}, "body": _STR, "offset": _DIM},
      "required": ["name", "geometry"]}),
    ("extrude_sketch", t_extrude_sketch,
     "Turn a closed sketch into a solid: 'pad' or 'pocket' inside a body, 'extrude' outside a body.",
     {"type": "object", "properties": {
         "sketch": _STR, "name": _NAME, "label": _LABEL, "length": _DIM,
         "operation": {"type": "string", "enum": ["pad", "pocket", "extrude"]},
         "reversed": {"type": "boolean"}, "midplane": {"type": "boolean"}},
      "required": ["sketch", "name", "length"]}),
    ("boolean", t_boolean,
     "Combine solids: cut tools from base, fuse, or keep the common volume.",
     {"type": "object", "properties": {
         "operation": {"type": "string", "enum": ["cut", "fuse", "common"]}, "name": _NAME, "label": _LABEL,
         "base": _STR, "tools": {"type": "array", "items": _STR}},
      "required": ["operation", "name", "base", "tools"]}),
    ("dress_up", t_dress_up,
     "Fillet or chamfer edges of a solid in a body. edges: 'all' or names like ['Edge1','Edge5'].",
     {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["fillet", "chamfer"]}, "object": _STR, "name": _NAME,
         "label": _LABEL, "size": _DIM, "edges": {"type": ["string", "array"]}},
      "required": ["kind", "object", "name", "size"]}),
    ("run_python", t_run_python,
     "Run FreeCAD Python for anything the other tools can't do. Variables: App, doc, Part, Sketcher, Draft. "
     "Follow the same rules: values via setExpression('Prop', 'Datasheet.param'), descriptive names. "
     "print() output is returned.",
     {"type": "object", "properties": {"code": _STR}, "required": ["code"]}),
]

MUTATING = {"create_object", "set_properties", "set_expression", "delete_objects", "create_body",
            "create_sketch", "extrude_sketch", "boolean", "dress_up", "run_python", "set_parameters",
            "import_parameters_csv", "bind_to_parameter", "rename_object"}


def tool_specs(allow_python, can_edit):
    specs = []
    for name, _, desc, params in TOOLS:
        if name == "run_python" and not allow_python:
            continue
        if name in MUTATING and not can_edit:
            continue
        specs.append({"name": name, "description": desc, "parameters": params})
    return specs


def call_tool(ctx, name, args):
    """Returns (content, is_error)."""
    fn = next((f for n, f, _, _ in TOOLS if n == name), None)
    if fn is None:
        return f"unknown tool {name}", True
    try:
        return fn(ctx, **(args or {})), False
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return f"bad arguments for {name}: {e}", True
    except Exception as e:
        return f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}", True
