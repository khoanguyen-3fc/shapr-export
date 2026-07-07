"""Parasolid (.x_b) export for Shapr3D workspaces.

Pipeline:

    workspace DB
      -> select the final-model body partitions      (relational feature graph)
      -> recover each body's name                     (Metadata + name anchors)
      -> merge the partition blocks into one named    (ref-graph merge via the
         Parasolid assembly                            psparser read/write API)

`psparser` is a reader/writer for the Parasolid XT part format, vendored as a
git submodule at ``vendor/ps-parser``.

Selection and naming are the best-effort heuristics documented in the repo
analysis -- they are transparent (``select_final_bodies`` returns diagnostics)
rather than authoritative.  The one authoritative fact is that each block is a
valid Parasolid partition; the geometry is never guessed.
"""

import io
import re
import sqlite3
from pathlib import Path

import msgpack
import psparser
from psparser import (
    Document,
    FieldType,
    load_schema,
    read_document,
    write_document,
)

# psparser is installed editable from vendor/ps-parser (see requirements.txt);
# the XT schema ships in that repo's assets/ dir, next to the package.
SCHEMA_PATH = Path(psparser.__file__).resolve().parents[1] / "assets" / "sch_13006.s_t"


class ExportError(RuntimeError):
    """Raised when a workspace cannot be exported to Parasolid."""


# ============================================================================
# DB extraction: which partitions are the final bodies, and what are they named
# ============================================================================

def _blob(v) -> bytes:
    if v is None:
        return b""
    return v if isinstance(v, (bytes, bytearray)) else str(v).encode()


def _current_partitions(con: sqlite3.Connection) -> dict[int, bytes]:
    """Latest-phase, non-deleted, non-empty partition blocks, keyed by PartitionID."""
    rows = con.execute(
        """
        SELECT p.PartitionID, p.Block
        FROM BodyRevisionPartitions p
        JOIN (
          SELECT PartitionID, MAX(Phase) AS Phase
          FROM BodyRevisionPartitions
          GROUP BY PartitionID
        ) m ON p.PartitionID = m.PartitionID AND p.Phase = m.Phase
        WHERE p.IsDeleted = 0 AND p.Block IS NOT NULL
        """
    ).fetchall()
    return {int(pid): bytes(blk) for pid, blk in rows}


def _feature_graph(con: sqlite3.Connection, partition_ids: set[int]):
    """Parse PersistedCalls (msgpack) into feature-node -> referenced partitions.

    Returns (part_producers, part_refs):
      part_producers[pid] = {node_ids that emit pid as a body record}
      part_refs[pid]      = {all node_ids whose call mentions pid anywhere}
    """
    def emitted(obj, acc: set[int]) -> None:
        # a body record is a 2-list [pid, <record-list>]
        if isinstance(obj, (list, tuple)):
            if (
                len(obj) == 2
                and isinstance(obj[0], int)
                and obj[0] in partition_ids
                and isinstance(obj[1], (list, tuple))
            ):
                acc.add(obj[0])
            for x in obj:
                emitted(x, acc)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                emitted(k, acc)
                emitted(v, acc)

    def all_ints(obj, acc: list[int]) -> None:
        if isinstance(obj, int):
            acc.append(obj)
        elif isinstance(obj, (list, tuple)):
            for x in obj:
                all_ints(x, acc)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                all_ints(k, acc)
                all_ints(v, acc)

    part_producers: dict[int, set[int]] = {}
    part_refs: dict[int, set[int]] = {}
    for ck, cd in con.execute("SELECT CallKey, CallData FROM PersistedCalls"):
        try:
            key = msgpack.unpackb(bytes(ck), raw=False)
            data = msgpack.unpackb(bytes(cd), raw=False)
        except Exception:
            continue
        if not (isinstance(key, (list, tuple)) and key and isinstance(key[0], int)):
            continue
        node = key[0]
        out: set[int] = set()
        emitted(data, out)
        for pid in out:
            part_producers.setdefault(pid, set()).add(node)
        ints: list[int] = []
        all_ints(data, ints)
        for pid in (i for i in ints if i in partition_ids):
            part_refs.setdefault(pid, set()).add(node)
    return part_producers, part_refs


def _named_bodies(con: sqlite3.Connection) -> list[tuple[str, int | None]]:
    """(body name, anchor feature node) for every Metadata type-0 body name."""
    import json

    def anchor(data) -> int | None:
        s = _blob(data).decode("latin-1")
        try:
            return int((json.loads(s).get("callKey") or {}).get("nodeID"))
        except Exception:
            m = re.search(r'"nodeID":(\d+)', s)
            return int(m.group(1)) if m else None

    out = []
    for hn_data, _md in con.execute(
        """
        SELECT hn.Data, m.Data
        FROM MetadataAssignments ma
        JOIN Metadata m ON m.MetadataID = ma.MetadataID AND m.MetadataTypeID = 0
        JOIN HistoryNames hn ON hn.NameID = ma.NameID
        """
    ):
        out.append((_blob(_md).decode("latin-1"), anchor(hn_data)))
    return out


def select_final_bodies(con: sqlite3.Connection) -> list[dict]:
    """Return the final-model bodies as dicts with block bytes, name, diagnostics.

    A partition is a final body when it is the primary output of a multi-feature
    chain (>= 2 producing features, so one-shot tool / pattern-instance outputs
    are excluded) that is NOT consumed by any later feature -- i.e. it survives
    to the tip of the history instead of being merged away.  See repo analysis.
    """
    blocks = _current_partitions(con)
    if not blocks:
        raise ExportError("workspace has no body-revision partitions")

    producers, refs = _feature_graph(con, set(blocks))
    if not producers:
        raise ExportError(
            "could not read the feature graph (PersistedCalls) -- "
            "cannot identify the final bodies"
        )

    final_ids: list[int] = []
    for pid, nodes in producers.items():
        if len(nodes) < 2:
            continue  # one-shot side output (tool body / pattern instance)
        last_produced = max(nodes)
        last_referenced = max(refs.get(pid, {last_produced}))
        if last_referenced <= last_produced:  # never touched after last emit -> survives
            final_ids.append(pid)
    final_ids.sort()
    if not final_ids:
        raise ExportError("no surviving body partitions found in the feature graph")

    names = _recover_names(con, final_ids, producers)
    return [
        {
            "partition_id": pid,
            "block": blocks[pid],
            "name": names[pid],
            "features": sorted(producers[pid]),
        }
        for pid in final_ids
    ]


def _recover_names(
    con: sqlite3.Connection,
    final_ids: list[int],
    producers: dict[int, set[int]],
) -> dict[int, str]:
    """Best-effort PartitionID -> body name.

    A body takes the name whose anchor sits on the *terminal* feature of its
    producing chain; final bodies with no anchor on their chain get the
    remaining unused names (elimination), then a sequential fallback.
    """
    named = _named_bodies(con)
    result: dict[int, str] = {}
    used: set[str] = set()

    for pid in final_ids:
        chain = producers[pid]
        candidates = [(nm, a) for nm, a in named if a is not None and a in chain]
        if candidates:
            name = max(candidates, key=lambda t: t[1])[0]  # terminal-feature name
            result[pid] = name
            used.add(name)

    remaining = sorted(nm for nm, _ in named if nm not in used)
    r = 0
    for i, pid in enumerate(final_ids):
        if pid in result:
            continue
        if r < len(remaining):
            result[pid] = remaining[r]
            r += 1
        else:
            result[pid] = f"Body {i + 1:02d}"
    return result


# ============================================================================
# Parasolid merge: the ASSEMBLY + INSTANCE + name-attribute skeleton is built
# programmatically from the schema, and each body gets its real DB name.
# ============================================================================

def _pfields(layout) -> list[str]:
    return [f.name for f in layout if f.field_type is FieldType.POINTER]


def _read_refs(block: bytes, schema) -> Document:
    """read_document from partition-block bytes, resolving pointer ids to node refs."""
    doc = read_document(io.BytesIO(bytes(block)), schema)
    by_id = {n["id"]: n for n in doc.nodes}
    for n in doc.nodes:
        for name in _pfields(doc.layouts[n["node_type"]]):
            v = n[name]
            if isinstance(v, list):
                n[name] = [by_id.get(x) for x in v]
            else:
                n[name] = by_id.get(v)
    return doc


def _find(doc: Document, node_name: str) -> dict:
    return next(n for n in doc.nodes if n["node_name"] == node_name)


def _type_by_name(schema) -> dict[str, int]:
    m: dict[str, int] = {}
    for t, td in schema.types.items():
        m.setdefault(td.node_name, t)
    return m


def _scalar_default(ft: FieldType):
    if ft in (FieldType.U8, FieldType.I16, FieldType.I32):
        return 0
    if ft is FieldType.LOGICAL:
        return False
    if ft in (FieldType.CHAR, FieldType.UTF16):
        return ""
    if ft in (FieldType.VECTOR, FieldType.H):
        return (None, None, None)
    if ft is FieldType.INTERVAL:
        return (None, None)
    if ft is FieldType.BOX:
        return ((None, None), (None, None), (None, None))
    return None  # POINTER -> null ref, F64 -> null sentinel


def _field_default(field):
    if field.n_elements > 1:
        if field.field_type in (FieldType.CHAR, FieldType.UTF16):
            return ""
        return [_scalar_default(field.field_type)] * field.n_elements
    return _scalar_default(field.field_type)


def _make_node(node_type: int, node_name: str, layouts, variable, **values) -> dict:
    """Build a node dict for `node_type`: every field of the merged layout is
    defaulted, then `values` overrides are applied. Pointer overrides hold node
    references (same shape as _read_refs), and `id` is assigned later at wiring."""
    node: dict = {"node_type": node_type, "node_name": node_name}
    if variable.get(node_type):
        node["count"] = values.pop("count", 1)
    node["id"] = None
    for field in layouts[node_type]:
        node[field.name] = (
            values.pop(field.name) if field.name in values else _field_default(field)
        )
    if values:
        raise ExportError(f"internal: unknown field(s) for {node_name}: {sorted(values)}")
    return node


# SDL/TYSA_NAME / SDL/TYSA_UNAME attribute definitions: Parasolid internal
# attribute type ids, per-action mask, owner mask and value-field code as used
# by Shapr3D exports. Baked in so the exporter needs no donor file.
_NAME_TYPE_ID = 8017
_UNAME_TYPE_ID = 8038
_NAME_FIELDS = 3
_UNAME_FIELDS = 10
_ATT_ACTIONS = [0] * 8
_ATT_LEGAL_OWNERS = [True] * 13 + [False, True, False, True]  # 17-entry owner mask


def _build_skeleton(parts, layouts, variable, tbn) -> dict:
    """Construct the ASSEMBLY + INSTANCE + name-definition template nodes that a
    bare partition block lacks. Pointer fields hold direct node references."""
    body0 = _find(parts[0], "BODY")

    def owner_mask(node_type: int) -> list[bool]:
        n = next(f.n_elements for f in layouts[node_type] if f.name == "legal_owners")
        mask = _ATT_LEGAL_OWNERS
        if n != len(mask):  # adapt to a schema with a different owner-mask width
            mask = (mask + [True] * n)[:n]
        return list(mask)

    adef, aid, attr = tbn["ATTRIB_DEF"], tbn["ATT_DEF_ID"], tbn["ATTRIBUTE"]

    name_id = _make_node(aid, "ATT_DEF_ID", layouts, variable,
                         count=len("SDL/TYSA_NAME"), string="SDL/TYSA_NAME")
    uname_id = _make_node(aid, "ATT_DEF_ID", layouts, variable,
                          count=len("SDL/TYSA_UNAME"), string="SDL/TYSA_UNAME")
    name_def = _make_node(adef, "ATTRIB_DEF", layouts, variable, count=1,
                          identifier=name_id, type_id=_NAME_TYPE_ID,
                          actions=list(_ATT_ACTIONS), legal_owners=owner_mask(adef),
                          fields=_NAME_FIELDS)
    uname_def = _make_node(adef, "ATTRIB_DEF", layouts, variable, count=1,
                           identifier=uname_id, type_id=_UNAME_TYPE_ID,
                           actions=list(_ATT_ACTIONS), legal_owners=owner_mask(adef),
                           fields=_UNAME_FIELDS)
    name_tpl = _make_node(attr, "ATTRIBUTE", layouts, variable, count=1,
                          definition=name_def)
    uname_tpl = _make_node(attr, "ATTRIBUTE", layouts, variable, count=1,
                           definition=uname_def)
    assembly = _make_node(tbn["ASSEMBLY"], "ASSEMBLY", layouts, variable,
                          res_size=body0["res_size"], res_linear=body0["res_linear"],
                          state=1, type=1)
    inst_tpl = _make_node(tbn["INSTANCE"], "INSTANCE", layouts, variable, type=1)
    return {
        "assembly": assembly, "inst_tpl": inst_tpl,
        "name_def": name_def, "uname_def": uname_def,
        "name_tpl": name_tpl, "uname_tpl": uname_tpl,
        "cv_type": tbn["CHAR_VALUES"], "uv_type": tbn["UNICODE_VALUES"],
    }


def _wire(ref_or_none, live: set) -> int | None:
    if ref_or_none is None:
        return None
    if id(ref_or_none) not in live:
        raise ValueError(
            f"pointer to a node outside the merged set: "
            f"{ref_or_none.get('node_name')}#{ref_or_none.get('id')}"
        )
    return ref_or_none["id"]


def merge_bodies(
    bodies: list[tuple[bytes, str]],
    schema,
) -> bytes:
    """Merge partition blocks into one named Parasolid assembly, returning .x_b bytes.

    `bodies` is a list of (partition block bytes, body name).  The ASSEMBLY +
    INSTANCE + SDL/TYSA_NAME / SDL/TYSA_UNAME scaffolding is built by
    ``_build_skeleton`` from the schema; framing (header/terminator) is reused
    from the first partition.
    """
    if not bodies:
        raise ExportError("nothing to export: no bodies selected")

    tbn = _type_by_name(schema)
    parts: list[Document] = [_read_refs(block, schema) for block, _ in bodies]

    # Merged schema: each partition's own (embedded) layouts win; node types that
    # only the skeleton introduces are filled from the base schema and emitted
    # with a 0xFF "use-base-schema" blob.
    layouts: dict[int, list] = {}
    variable: dict[int, bool] = {}
    blobs: dict[int, bytes] = {}
    for p in parts:
        for t, v in p.layouts.items():
            layouts.setdefault(t, v)
        for t, v in p.variable.items():
            variable.setdefault(t, v)
        for t, v in p.schema_blobs.items():
            blobs.setdefault(t, v)
    for nm in ("ASSEMBLY", "INSTANCE", "ATTRIB_DEF", "ATT_DEF_ID",
               "ATTRIBUTE", "CHAR_VALUES", "UNICODE_VALUES"):
        t = tbn[nm]
        if t not in layouts:
            layouts[t] = schema.types[t].fields
            variable[t] = schema.types[t].variable
            blobs[t] = b"\xff"  # 255 = use the base schema for this type

    sk = _build_skeleton(parts, layouts, variable, tbn)
    assembly = sk["assembly"]
    inst_tpl = sk["inst_tpl"]
    name_def, uname_def = sk["name_def"], sk["uname_def"]
    name_tpl, uname_tpl = sk["name_tpl"], sk["uname_tpl"]
    cv_type, uv_type = sk["cv_type"], sk["uv_type"]

    merged: list[dict] = [assembly]
    kept_defs: dict[str, dict] = {}

    for d in (name_def, uname_def):
        kept_defs.setdefault(d["identifier"]["string"], d)
    for d in kept_defs.values():
        merged += [d, d["identifier"]]

    kdefs = list(kept_defs.values())
    for i, d in enumerate(kdefs):
        d["next"] = kdefs[i + 1] if i + 1 < len(kdefs) else None

    def attach_name(owner, text, id_name, id_uname):
        """Prepend SDL/TYSA_NAME + SDL/TYSA_UNAME = `text` to owner's attribute
        chain and return (name_attr, uname_attr, cv, uv)."""
        old_head = owner["attributes_features"]
        cv = {"node_type": cv_type, "node_name": "CHAR_VALUES",
              "count": len(text), "id": None, "values": text}
        uv = {"node_type": uv_type, "node_name": "UNICODE_VALUES",
              "count": len(text), "id": None, "values": text}
        name_attr = dict(name_tpl)
        uname_attr = dict(uname_tpl)
        name_attr.update(count=1, node_id=id_name, definition=name_def, owner=owner,
                         next=old_head, previous=uname_attr,
                         next_of_type=None, previous_of_type=None, fields=cv)
        uname_attr.update(count=1, node_id=id_uname, definition=uname_def, owner=owner,
                          next=name_attr, previous=None,
                          next_of_type=None, previous_of_type=None, fields=uv)
        if old_head is not None:
            old_head["previous"] = name_attr
        owner["attributes_features"] = uname_attr
        return name_attr, uname_attr, cv, uv

    # name the root assembly "root" (matches native Shapr3D exports); its node_id
    # namespace holds the two name attributes (1, 2) then the instances (3 ...).
    a_name, a_uname, a_cv, a_uv = attach_name(assembly, "root", 1, 2)
    merged += [a_cv, a_uv, a_name, a_uname]

    instances: list[dict] = []
    for i, (_block, body_name) in enumerate(bodies):
        p = parts[i]
        world = _find(p, "WORLD")
        body = _find(p, "BODY")

        # dedup this partition's ATTRIB_DEFs against everything kept so far
        drop: set[int] = set()
        replace: dict[int, dict] = {}
        for n in p.nodes:
            if n["node_name"] != "ATTRIB_DEF":
                continue
            s = n["identifier"]["string"]
            if s in kept_defs:
                keep = kept_defs[s]
                replace[id(n)] = keep
                replace[id(n["identifier"])] = keep["identifier"]
                drop |= {id(n), id(n["identifier"])}
            else:
                kept_defs[s] = n
        if replace:
            for n in p.nodes:
                for name in _pfields(p.layouts[n["node_type"]]):
                    v = n[name]
                    if isinstance(v, list):
                        n[name] = [replace.get(id(x), x) for x in v]
                    elif isinstance(v, dict) and id(v) in replace:
                        n[name] = replace[id(v)]

        inst = dict(inst_tpl)
        inst["node_id"] = 3 + i  # assembly namespace, after the two root name attrs
        inst["attributes_features"] = None
        inst["part"] = body
        inst["assembly"] = assembly
        inst["transform"] = None
        inst["next_in_part"] = inst["prev_in_part"] = None
        inst["next_of_part"] = inst["prev_of_part"] = None
        instances.append(inst)

        body["owner"] = None
        body["ref_instance"] = inst
        body["next"] = body["previous"] = None

        # name attributes = body_name, in the body's own node_id namespace
        h = body["highest_node_id"]
        name_attr, uname_attr, cv, uv = attach_name(body, body_name, h + 1, h + 2)
        body["highest_node_id"] = h + 2

        blist = body["attribute_chains"]
        if blist is not None:
            blk = blist["list_block"]
            while blk is not None and blk["n_entries"] + 2 > len(blk["entries"]):
                blk = blk["next_block"]
            if blk is None:
                raise ExportError(
                    "attribute list full; block chaining unsupported for "
                    f"body '{body_name}'"
                )
            n = blk["n_entries"]
            blk["entries"][n] = name_attr
            blk["entries"][n + 1] = uname_attr
            blk["n_entries"] = n + 2
            blist["list_length"] += 2

        for n in p.nodes:
            if n is world or id(n) in drop:
                continue
            merged.append(n)
        merged += [inst, cv, uv, name_attr, uname_attr]

    for k, inst in enumerate(instances):
        inst["next_in_part"] = instances[k + 1] if k + 1 < len(instances) else None
        inst["prev_in_part"] = instances[k - 1] if k > 0 else None
    assembly["sub_instance"] = instances[0]
    assembly["highest_node_id"] = 2 + len(instances)

    # reassign wire ids (stream index + 1; root ASSEMBLY = id 2)
    for idx, n in enumerate(merged):
        n["id"] = idx + 2

    live = {id(n) for n in merged}
    for n in merged:
        for name in _pfields(layouts[n["node_type"]]):
            v = n[name]
            if isinstance(v, list):
                n[name] = [_wire(x, live) for x in v]
            else:
                n[name] = _wire(v, live)

    doc = Document(
        header=parts[0].header, nodes=merged, layouts=layouts, variable=variable,
        schema_blobs=blobs, terminator=parts[0].terminator,
    )
    return write_document(doc)


# ============================================================================
# Top-level entry point
# ============================================================================

def export_workspace(
    workspace_db: Path,
    out_path: Path,
    *,
    schema_path: Path = SCHEMA_PATH,
) -> dict:
    """Export a Shapr3D workspace DB to a Parasolid .x_b assembly file.

    Returns a summary dict: {'out_path', 'bytes', 'bodies': [names]}.
    Raises ExportError on any recoverable problem.
    """
    if not schema_path.exists():
        raise ExportError(
            f"Parasolid schema not found: {schema_path} "
            "(is the ps-parser submodule initialized?)"
        )

    schema = load_schema(str(schema_path))
    con = sqlite3.connect(str(workspace_db))
    try:
        selected = select_final_bodies(con)
    finally:
        con.close()

    bodies = [(b["block"], b["name"]) for b in selected]
    data = merge_bodies(bodies, schema)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return {
        "out_path": out_path,
        "bytes": len(data),
        "bodies": [b["name"] for b in selected],
        "partition_ids": [b["partition_id"] for b in selected],
    }
