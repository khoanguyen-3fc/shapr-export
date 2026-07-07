"""Tessellation (.stl) export for Shapr3D projects.

Shapr3D writes a per-project tessellation cache (a SQLite database) when it
displays a model. Each surface is stored in the ``Faces`` table as a triangle
mesh: a ``Positions`` blob of float32 x/y/z vertices and an ``IndexBuffer`` blob
of uint32 triangle indices (a triangle list). This module reads the cached
meshes and writes a single binary STL.

Blobs are stored little-endian (Shapr3D runs on macOS / Windows), which matches
native byte order on the platforms this tool targets. Positions are in meters
and scaled to millimeters (the usual STL unit) on write.
"""

import array
import sqlite3
from pathlib import Path

Vertex = tuple[float, float, float]
Triangle = tuple[Vertex, Vertex, Vertex]

# Shapr3D stores geometry in meters; STL is conventionally millimeters.
_M_TO_MM = 1000.0


class TessellationError(RuntimeError):
    """Raised when a tessellation cache cannot be exported to STL."""


def _face_triangles(con: sqlite3.Connection) -> list[list[Triangle]]:
    """Decode each non-archived face's mesh into a list of triangles."""
    faces: list[list[Triangle]] = []
    for pos_blob, idx_blob in con.execute(
        "SELECT Positions, IndexBuffer FROM Faces WHERE IsArchived = 0"
    ):
        positions = array.array("f")
        positions.frombytes(bytes(pos_blob))
        verts: list[Vertex] = list(zip(*[iter(positions)] * 3))

        indices = array.array("I")
        indices.frombytes(bytes(idx_blob))

        tris = [
            (verts[indices[i]], verts[indices[i + 1]], verts[indices[i + 2]])
            for i in range(0, len(indices) - 2, 3)
        ]
        if tris:
            faces.append(tris)
    return faces


def _write_binary_stl(out_path: Path, triangles: list[Triangle]) -> None:
    buf = bytearray()
    buf += b"\0" * 80  # 80-byte header (unused)
    buf += len(triangles).to_bytes(4, "little")
    for v0, v1, v2 in triangles:
        buf += b"\0" * 12  # normal left zero; readers derive it from vertex winding
        for x, y, z in (v0, v1, v2):
            buf += array.array("f", (x * _M_TO_MM, y * _M_TO_MM, z * _M_TO_MM)).tobytes()
        buf += b"\0\0"  # attribute byte count
    Path(out_path).write_bytes(buf)


def export_stl(cache_db: Path, out_path: Path) -> dict:
    """Convert a Shapr3D tessellation cache database to a binary STL file.

    Exports every non-archived face. Returns a summary dict:
    {'out_path', 'faces', 'triangles'}. Raises TessellationError if the cache
    holds no exportable geometry.
    """
    con = sqlite3.connect(str(cache_db))
    try:
        faces = _face_triangles(con)
    finally:
        con.close()

    if not faces:
        raise TessellationError("tessellation cache has no faces to export")

    triangles = [tri for face in faces for tri in face]
    _write_binary_stl(Path(out_path), triangles)
    return {
        "out_path": Path(out_path),
        "faces": len(faces),
        "triangles": len(triangles),
    }
