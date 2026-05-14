from __future__ import annotations

import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv
import trimesh
from flask import Flask, jsonify, request, send_from_directory
from scipy.spatial import cKDTree
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
REVISION_DIR = BASE_DIR / "revisions"
DIFF_DIR = STATIC_DIR / "diff"
THUMB_DIR = STATIC_DIR / "thumbs"
REVISIONS_FILE = BASE_DIR / "revisions.json"

ALLOWED_EXTENSIONS = {".stl", ".step", ".obj", ".iges"}
MAX_FACES = 100_000
DIFF_SAMPLE_POINTS = 200_000
DEFAULT_TOLERANCE_RATIO = 0.002

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
executor = ThreadPoolExecutor(max_workers=2)
diff_cache: dict[str, dict[str, Any]] = {}


def ensure_storage() -> None:
    for directory in (STATIC_DIR, UPLOAD_DIR, REVISION_DIR, DIFF_DIR, THUMB_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not REVISIONS_FILE.exists():
        save_revisions([])


def json_error(message: str, status: int = 400):
    response = jsonify({"error": message})
    response.status_code = status
    return response


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_revisions() -> list[dict[str, Any]]:
    ensure_storage()
    try:
        return json.loads(REVISIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_revisions(revisions: list[dict[str, Any]]) -> None:
    temp_path = REVISIONS_FILE.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(revisions, indent=2), encoding="utf-8")
    temp_path.replace(REVISIONS_FILE)


def extension_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def load_any_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="mesh")
    if isinstance(loaded, trimesh.Scene):
        meshes = [geometry for geometry in loaded.geometry.values() if isinstance(geometry, trimesh.Trimesh)]
        if not meshes:
            raise ValueError("No mesh geometry found in file.")
        loaded = trimesh.util.concatenate(meshes)
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise ValueError("Could not load a valid mesh.")
    loaded.remove_unreferenced_vertices()
    return loaded


def decimate_mesh(mesh: trimesh.Trimesh, max_faces: int = MAX_FACES) -> trimesh.Trimesh:
    if len(mesh.faces) <= max_faces:
        return mesh

    simplified = None
    try:
        simplified = mesh.simplify_quadric_decimation(face_count=max_faces)
    except TypeError:
        try:
            simplified = mesh.simplify_quadric_decimation(max_faces)
        except Exception:
            simplified = None
    except Exception:
        simplified = None

    if isinstance(simplified, trimesh.Trimesh) and not simplified.is_empty:
        simplified.remove_unreferenced_vertices()
        return simplified

    rng = np.random.default_rng(42)
    face_indices = np.sort(rng.choice(len(mesh.faces), size=max_faces, replace=False))
    fallback = mesh.submesh([face_indices], append=True, repair=False)
    fallback.remove_unreferenced_vertices()
    return fallback


def center_mesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    aligned = mesh.copy()
    aligned.apply_translation(-aligned.centroid)
    return aligned


def default_tolerance(mesh_a: trimesh.Trimesh, mesh_b: trimesh.Trimesh) -> float:
    bounds = np.vstack([mesh_a.bounds, mesh_b.bounds])
    diagonal = float(np.linalg.norm(bounds.max(axis=0) - bounds.min(axis=0)))
    return max(diagonal * DEFAULT_TOLERANCE_RATIO, np.finfo(float).eps)


def color_mesh(mesh: trimesh.Trimesh | None, hex_color: str) -> trimesh.Trimesh | None:
    if mesh is None or mesh.is_empty or len(mesh.faces) == 0:
        return None
    colored = mesh.copy()
    rgb = tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    colored.visual.vertex_colors = np.tile([*rgb, 255], (len(colored.vertices), 1))
    return colored


def write_ply(mesh: trimesh.Trimesh | None, path: Path, hex_color: str) -> None:
    if mesh is None or mesh.is_empty or len(mesh.faces) == 0:
        empty = trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=np.int64))
        empty.export(path, file_type="ply")
        return
    colored = color_mesh(mesh, hex_color)
    assert colored is not None
    colored.export(path, file_type="ply")


def trimesh_to_pyvista(mesh: trimesh.Trimesh) -> pv.PolyData:
    faces = np.hstack([np.full((len(mesh.faces), 1), 3), mesh.faces]).astype(np.int64).ravel()
    return pv.PolyData(mesh.vertices, faces)


def generate_thumbnail(mesh: trimesh.Trimesh, output_path: Path) -> None:
    try:
        pv.OFF_SCREEN = True
        plotter = pv.Plotter(off_screen=True, window_size=(512, 512))
        plotter.set_background("#101010")
        plotter.add_mesh(trimesh_to_pyvista(mesh), color="#c9d1d9", smooth_shading=True)
        plotter.view_isometric()
        plotter.camera.zoom(1.25)
        plotter.show(screenshot=str(output_path), auto_close=True)
    except Exception:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (512, 512), "#101010")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((96, 132, 416, 380), radius=18, outline="#00ff88", width=4)
        draw.text((154, 242), "STL preview", fill="#f2f2f2")
        image.save(output_path)


def repair_for_boolean(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    repaired = mesh.copy()
    if hasattr(repaired, "remove_duplicate_faces"):
        repaired.remove_duplicate_faces()
    else:
        repaired.update_faces(repaired.unique_faces())
    if hasattr(repaired, "remove_degenerate_faces"):
        repaired.remove_degenerate_faces()
    else:
        repaired.update_faces(repaired.nondegenerate_faces())
    repaired.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(repaired)
    return repaired


def nonempty_mesh(mesh: trimesh.Trimesh | None) -> bool:
    return bool(mesh is not None and isinstance(mesh, trimesh.Trimesh) and not mesh.is_empty and len(mesh.faces) > 0)


def try_boolean_diff(old_mesh: trimesh.Trimesh, new_mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh | None, trimesh.Trimesh | None, trimesh.Trimesh | None]:
    old_repaired = repair_for_boolean(old_mesh)
    new_repaired = repair_for_boolean(new_mesh)
    if not old_repaired.is_watertight or not new_repaired.is_watertight:
        raise ValueError("one or both meshes are not watertight")

    removed = trimesh.boolean.difference([old_repaired, new_repaired])
    added = trimesh.boolean.difference([new_repaired, old_repaired])
    unchanged = trimesh.boolean.intersection([old_repaired, new_repaired])
    if not any(nonempty_mesh(mesh) for mesh in (unchanged, added, removed)):
        raise ValueError("boolean engine returned empty results")
    return unchanged, added, removed


def sample_tree(mesh: trimesh.Trimesh, sample_points: int = DIFF_SAMPLE_POINTS) -> cKDTree:
    count = max(sample_points, len(mesh.vertices))
    points, _ = trimesh.sample.sample_surface(mesh, count)
    return cKDTree(np.vstack([mesh.vertices, points]))


def submesh(mesh: trimesh.Trimesh, face_indices: np.ndarray) -> trimesh.Trimesh | None:
    if len(face_indices) == 0:
        return None
    result = mesh.submesh([face_indices], append=True, repair=False)
    if isinstance(result, trimesh.Trimesh) and not result.is_empty:
        result.remove_unreferenced_vertices()
        return result
    return None


def fallback_diff(old_mesh: trimesh.Trimesh, new_mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh | None, trimesh.Trimesh | None, trimesh.Trimesh | None]:
    tolerance = default_tolerance(old_mesh, new_mesh)
    old_tree = sample_tree(old_mesh)
    new_tree = sample_tree(new_mesh)
    old_distances, _ = new_tree.query(old_mesh.triangles_center, workers=-1)
    new_distances, _ = old_tree.query(new_mesh.triangles_center, workers=-1)

    unchanged = submesh(old_mesh, np.flatnonzero(old_distances <= tolerance))
    removed = submesh(old_mesh, np.flatnonzero(old_distances > tolerance))
    added = submesh(new_mesh, np.flatnonzero(new_distances > tolerance))
    return unchanged, added, removed


def cache_key(id1: str, id2: str) -> str:
    return hashlib.sha256(f"{id1}:{id2}".encode("utf-8")).hexdigest()[:24]


def get_revision(revision_id: str) -> dict[str, Any] | None:
    return next((revision for revision in load_revisions() if revision["id"] == revision_id), None)


def compute_diff(id1: str, id2: str) -> dict[str, Any]:
    key = cache_key(id1, id2)
    if key in diff_cache:
        return diff_cache[key]

    rev1 = get_revision(id1)
    rev2 = get_revision(id2)
    if not rev1 or not rev2:
        raise ValueError("One or both revisions were not found.")

    old_mesh = center_mesh(load_any_mesh(BASE_DIR / rev1["stl_path"]))
    new_mesh = center_mesh(load_any_mesh(BASE_DIR / rev2["stl_path"]))

    method = "boolean"
    warning = None
    try:
        unchanged, added, removed = try_boolean_diff(old_mesh, new_mesh)
    except Exception as exc:
        method = "distance"
        warning = f"Boolean diff failed; used distance fallback. {exc}"
        unchanged, added, removed = fallback_diff(old_mesh, new_mesh)

    prefix = f"{key}"
    paths = {
        "unchanged": DIFF_DIR / f"{prefix}_unchanged.ply",
        "added": DIFF_DIR / f"{prefix}_added.ply",
        "removed": DIFF_DIR / f"{prefix}_removed.ply",
    }
    write_ply(unchanged, paths["unchanged"], "#808080")
    write_ply(added, paths["added"], "#00ff88")
    write_ply(removed, paths["removed"], "#ff4444")

    payload = {
        "id1": id1,
        "id2": id2,
        "method": method,
        "warning": warning,
        "urls": {name: f"/static/diff/{path.name}" for name, path in paths.items()},
    }
    diff_cache[key] = payload
    return payload


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.errorhandler(404)
def not_found(_error):
    return json_error("Not found.", 404)


@app.errorhandler(500)
def internal_error(error):
    return json_error(str(error), 500)


@app.post("/upload")
def upload():
    ensure_storage()
    upload_file = request.files.get("file")
    tag = request.form.get("tag", "").strip()
    if upload_file is None or not upload_file.filename:
        return json_error("Missing multipart file field 'file'.", 400)
    if not extension_allowed(upload_file.filename):
        return json_error("Unsupported file type. Use .stl, .step, .obj, or .iges.", 400)

    revision_id = uuid.uuid4().hex
    original_name = secure_filename(upload_file.filename)
    source_path = UPLOAD_DIR / f"{revision_id}_{original_name}"
    stl_path = REVISION_DIR / f"{revision_id}.stl"
    thumb_path = THUMB_DIR / f"{revision_id}.png"
    upload_file.save(source_path)

    try:
        mesh = load_any_mesh(source_path)
        mesh = decimate_mesh(mesh)
        mesh.export(stl_path, file_type="stl")
        generate_thumbnail(mesh, thumb_path)
    except Exception as exc:
        return json_error(f"Failed to process CAD file: {exc}", 422)

    revision = {
        "id": revision_id,
        "filename": original_name,
        "tag": tag or "untagged",
        "created_at": utc_now(),
        "faces": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "source_path": str(source_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "stl_path": str(stl_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "thumbnail_url": f"/static/thumbs/{thumb_path.name}",
    }
    revisions = load_revisions()
    revisions.append(revision)
    save_revisions(revisions)
    return jsonify(revision), 201


@app.get("/revisions")
def revisions():
    data = sorted(load_revisions(), key=lambda item: item["created_at"], reverse=True)
    return jsonify(data)


@app.post("/compare")
def compare():
    payload = request.get_json(silent=True) or {}
    id1 = payload.get("id1")
    id2 = payload.get("id2")
    if not id1 or not id2:
        return json_error("JSON body must include id1 and id2.", 400)
    if id1 == id2:
        return json_error("Choose two different revisions.", 400)

    try:
        future = executor.submit(compute_diff, id1, id2)
        return jsonify(future.result())
    except Exception as exc:
        return json_error(f"Compare failed: {exc}", 422)


ensure_storage()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
