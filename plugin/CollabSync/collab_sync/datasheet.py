"""The document's datasheet: one spreadsheet holding every design parameter.

Layout of the Spreadsheet::Sheet (default name "Datasheet"):

    A: Parameter    B: Value          C: Description
    plate_length    =40 mm            Overall plate length
    hole_offset     =plate_length/8   Hole distance from the edge

Column B cells carry an alias equal to the parameter name, so any object can
use `Datasheet.plate_length` in an expression. Values may be quantities
("40 mm", "90 deg", "4") or formulas over other parameters ("=a/2").

Used by the agent (to enforce parametric modelling) and by the plugin's
CSV import/export commands. Needs FreeCAD only for the sheet object itself.
"""

import csv
import io
import re

DEFAULT_NAME = "Datasheet"
HEADER = ("Parameter", "Value", "Description")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
NUMBER_RE = re.compile(
    r"^\s*-?(\d+([.,]\d*)?|[.,]\d+)([eE][-+]?\d+)?\s*([A-Za-zµ°][A-Za-z0-9µ°/^*]*)?\s*$")
# Identifiers that may appear in expressions without being parameters.
EXPR_WORDS = {
    "mm", "cm", "m", "km", "um", "nm", "in", "ft", "thou", "mil", "yd", "deg", "rad", "gon",
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sqrt", "abs", "min", "max", "floor", "ceil",
    "round", "trunc", "mod", "pow", "exp", "log", "log10", "pi", "e", "hypot", "cath",
}
MAX_ROWS = 5000
EMPTY_RUN = 25


class DatasheetError(Exception):
    pass


def valid_name(name):
    return bool(PARAM_RE.match(name or "")) and name not in EXPR_WORDS


def valid_value(value):
    v = (value or "").strip()
    return bool(v) and (v.startswith("=") or bool(NUMBER_RE.match(v)))


def _normalize_value(value):
    v = str(value).strip()
    if v.startswith("="):
        return v
    # "40,5 mm" (German decimal comma) -> "40.5 mm"; store as a formula so units parse reliably.
    return "=" + re.sub(r"(?<=\d),(?=\d)", ".", v)


def get_sheet(doc, name=DEFAULT_NAME, create=False):
    sheet = doc.getObject(name)
    if sheet is None:
        if not create:
            return None
        sheet = doc.addObject("Spreadsheet::Sheet", name)
        sheet.Label = name
        for col, text in zip("ABC", HEADER):
            sheet.set(f"{col}1", "'" + text)
        try:
            sheet.setStyle("A1:C1", "bold")
            sheet.setColumnWidth("A", 160)
            sheet.setColumnWidth("C", 300)
        except Exception:
            pass
    elif sheet.TypeId != "Spreadsheet::Sheet":
        raise DatasheetError(f"{name} exists but is not a spreadsheet")
    return sheet


def _text(sheet, cell):
    try:
        t = sheet.getContents(cell) or ""
    except Exception:
        return ""
    return t[1:] if t.startswith("'") else t


def _alias(sheet, cell):
    try:
        return sheet.getAlias(cell) or ""
    except Exception:
        return ""


def _scan(sheet):
    """(row, alias) for every parameter row, plus the first free row."""
    rows, empty, last_used = [], 0, 1
    for r in range(2, MAX_ROWS):
        alias = _alias(sheet, f"B{r}")
        used = alias or _text(sheet, f"A{r}") or _text(sheet, f"B{r}")
        if used:
            empty, last_used = 0, r
            if alias:
                rows.append((r, alias))
        else:
            empty += 1
            if empty >= EMPTY_RUN:
                break
    return rows, last_used + 1


def _evaluated(sheet, alias):
    try:
        v = sheet.get(alias)
    except Exception:
        return None
    if hasattr(v, "UserString"):
        return v.UserString
    return v


def list_parameters(doc, name=DEFAULT_NAME):
    sheet = get_sheet(doc, name)
    if sheet is None:
        return []
    out = []
    for row, alias in _scan(sheet)[0]:
        raw = _text(sheet, f"B{row}")
        out.append({
            "name": alias,
            "value": raw[1:] if raw.startswith("=") and NUMBER_RE.match(raw[1:]) else raw,
            "evaluated": _evaluated(sheet, alias),
            "description": _text(sheet, f"C{row}"),
        })
    return out


def parameter_names(doc, name=DEFAULT_NAME):
    sheet = get_sheet(doc, name)
    return {alias for _, alias in _scan(sheet)[0]} if sheet is not None else set()


def set_parameters(doc, params, name=DEFAULT_NAME):
    """params: [{"name", "value", "description"?}]. Returns (written, errors)."""
    sheet = get_sheet(doc, name, create=True)
    rows, free = _scan(sheet)
    by_alias = {a: r for r, a in rows}
    written, errors = [], {}
    for p in params:
        pname = str(p.get("name", "")).strip()
        value = p.get("value")
        if not valid_name(pname):
            errors[pname or "?"] = "names are snake_case: a-z, 0-9, _ (start with a letter, max 40)"
            continue
        if value is None or not valid_value(str(value)):
            errors[pname] = 'value must be a number with optional unit ("40 mm", "90 deg", "4") or "=formula"'
            continue
        row = by_alias.get(pname)
        try:
            if row is None:
                row, free = free, free + 1
                sheet.set(f"A{row}", "'" + pname)
                sheet.setAlias(f"B{row}", pname)
                by_alias[pname] = row
            sheet.set(f"B{row}", _normalize_value(value))
            if p.get("description") is not None:
                sheet.set(f"C{row}", "'" + str(p["description"]).replace("\n", " ")[:300])
            written.append(pname)
        except Exception as e:
            errors[pname] = f"{type(e).__name__}: {e}"
    sheet.recompute()
    return written, errors


def parse_csv(text):
    """Rows from CSV text: header-aware (name/parameter, value, unit, description) or
    headerless name,value[,description]. Accepts , or ; as separator."""
    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [c.strip().lower() for c in rows[0]]
    if head and head[0] in ("name", "parameter", "param", "alias"):
        idx = {k: head.index(k) for k in head}
        col = lambda *keys: next((idx[k] for k in keys if k in idx), None)  # noqa: E731
        ci, cv, cu, cd = col("name", "parameter", "param", "alias"), col("value", "wert"), \
            col("unit", "einheit"), col("description", "beschreibung", "comment", "note")
        body = rows[1:]
    else:
        ci, cv, cu, cd = 0, 1, None, 2
        body = rows
    out = []
    for r in body:
        get = lambda i: r[i].strip() if i is not None and i < len(r) else ""  # noqa: E731
        value = get(cv)
        if cu is not None and get(cu) and not value.startswith("="):
            value = f"{value} {get(cu)}"
        out.append({"name": get(ci), "value": value, "description": get(cd) or None})
    return out


def import_csv(doc, text, name=DEFAULT_NAME):
    return set_parameters(doc, parse_csv(text), name)


def export_csv(doc, name=DEFAULT_NAME):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["name", "value", "description"])
    for p in list_parameters(doc, name):
        w.writerow([p["name"], p["value"], p["description"]])
    return buf.getvalue()


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_SAFE = re.compile(r"^[A-Za-z0-9_ .,+\-*/()^%]+$")


def to_expression(expr, params, name=DEFAULT_NAME, require_param=True):
    """Translate "plate_length / 2" into "Datasheet.plate_length / 2".

    Raises DatasheetError for unknown identifiers or (when require_param) for
    expressions that don't use any parameter, i.e. bare numbers.
    """
    text = str(expr).strip()
    if not text or not _SAFE.match(text):
        raise DatasheetError(f"invalid expression {expr!r}")
    used, unknown = [], []

    def repl(m):
        tok = m.group(0)
        if "." in tok:
            head, tail = tok.split(".", 1)
            if head == name and tail in params:
                used.append(tail)
                return tok
            unknown.append(tok)
            return tok
        if tok in params:
            used.append(tok)
            return f"{name}.{tok}"
        if tok in EXPR_WORDS:
            return tok
        unknown.append(tok)
        return tok

    out = _TOKEN.sub(repl, text)
    if unknown:
        raise DatasheetError(
            f"unknown parameter(s) {', '.join(sorted(set(unknown)))}; define them with set_parameters first")
    if require_param and not used:
        raise DatasheetError(
            f"{expr!r} is a plain value; put it in the datasheet with set_parameters and use the parameter name")
    return out
