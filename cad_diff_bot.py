#!/usr/bin/env python3
"""
cad_diff_bot.py

Usage:
  python cad_diff_bot.py --old path/to/old.stl --new path/to/new.stl
  python cad_diff_bot.py --old old.stl --new new.stl --output-dir cad_diff_output --tolerance 0.05

Requirements:
  - Python 3.10+
  - Install dependencies with: pip install -r requirements.txt
  - Optional Slack posting: set SLACK_WEBHOOK_URL in your environment or a local .env file.

This script loads two STL meshes, aligns them by centroid, computes added/removed/
unchanged regions, renders four 1920x1080 comparison images, and posts them to
Slack through an incoming webhook. Since Slack incoming webhooks cannot upload
local image files directly, the script uploads images to tmpfiles.org and posts
public image links. If upload fails, the images remain available in output-dir.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pyvista as pv
import requests
import trimesh
from dotenv import load_dotenv
from scipy.spatial import cKDTree


LOGGER = logging.getLogger("cad_diff_bot")

IMAGE_SIZE = (1920, 1080)
DEFAULT_OUTPUT_DIR = "cad_diff_output"
DEFAULT_SAMPLE_POINTS = 250_000
DEFAULT_TOLERANCE_RATIO = 0.002


@dataclass
class DifferenceMeshes:
    unchanged: trimesh.Trimesh | None
    removed: trimesh.Trimesh | None
    added: trimesh.Trimesh | None
    method: str
    warning: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render and notify a visual STL diff for CAD iterations."
    )
    parser.add_argument("--old", required=True, type=Path, help="Path to the old STL file.")
    parser.add_argument("--new", required=True, type=Path, help="Path to the new STL file.")
    parser.add_argument(
        "--output-dir",
        default=Path(DEFAULT_OUTPUT_DIR),
        type=Path,
        help=f"Directory for rendered images. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--tolerance",
        default=None,
        type=float,
        help=(
            "Distance threshold in model units for unchanged geometry. "
            "Default: 0.2%% of the aligned bounding-box diagonal."
        ),
    )
    parser.add_argument(
        "--sample-points",
        default=DEFAULT_SAMPLE_POINTS,
        type=int,
        help=(
            "Surface sample count used by distance fallback. "
            f"Default: {DEFAULT_SAMPLE_POINTS}"
        ),
    )
    parser.add_argument(
        "--skip-slack",
        action="store_true",
        help="Render images but do not post a Slack message.",
    )
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )


def load_mesh(path: Path) -> trimesh.Trimesh:
    if not path.exists():
        raise FileNotFoundError(f"STL file does not exist: {path}")
    loaded = trimesh.load_mesh(path, file_type="stl", force="mesh")
    if isinstance(loaded, trimesh.Scene):
        loaded = trimesh.util.concatenate(tuple(loaded.dump()))
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise ValueError(f"Could not load a valid mesh from {path}")
    loaded.remove_unreferenced_vertices()
    return loaded


def align_by_centroid(old_mesh: trimesh.Trimesh, new_mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    old_aligned = old_mesh.copy()
    new_aligned = new_mesh.copy()
    old_aligned.apply_translation(-old_aligned.centroid)
    new_aligned.apply_translation(-new_aligned.centroid)
    return old_aligned, new_aligned


def default_tolerance(old_mesh: trimesh.Trimesh, new_mesh: trimesh.Trimesh) -> float:
    combined_bounds = np.vstack([old_mesh.bounds, new_mesh.bounds])
    diagonal = float(np.linalg.norm(combined_bounds.max(axis=0) - combined_bounds.min(axis=0)))
    return max(diagonal * DEFAULT_TOLERANCE_RATIO, np.finfo(float).eps)


def nonempty_mesh(mesh: trimesh.Trimesh | None) -> bool:
    return bool(mesh is not None and isinstance(mesh, trimesh.Trimesh) and not mesh.is_empty and len(mesh.faces) > 0)


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


def try_boolean_diff(old_mesh: trimesh.Trimesh, new_mesh: trimesh.Trimesh) -> DifferenceMeshes | None:
    old_repaired = repair_for_boolean(old_mesh)
    new_repaired = repair_for_boolean(new_mesh)

    if not old_repaired.is_watertight or not new_repaired.is_watertight:
        raise ValueError("one or both meshes are not watertight")

    removed = trimesh.boolean.difference([old_repaired, new_repaired])
    added = trimesh.boolean.difference([new_repaired, old_repaired])
    unchanged = trimesh.boolean.intersection([old_repaired, new_repaired])

    if not any(nonempty_mesh(mesh) for mesh in (removed, added, unchanged)):
        raise ValueError("boolean backend returned empty results")

    return DifferenceMeshes(
        unchanged=unchanged if nonempty_mesh(unchanged) else None,
        removed=removed if nonempty_mesh(removed) else None,
        added=added if nonempty_mesh(added) else None,
        method="boolean",
    )


def sample_surface_tree(mesh: trimesh.Trimesh, sample_points: int) -> cKDTree:
    count = max(sample_points, len(mesh.vertices))
    sampled, _ = trimesh.sample.sample_surface_even(mesh, count=count)
    if len(sampled) == 0:
        sampled, _ = trimesh.sample.sample_surface(mesh, count=count)
    points = np.vstack([mesh.vertices, sampled])
    return cKDTree(points)


def face_center_distances(source: trimesh.Trimesh, target_tree: cKDTree) -> np.ndarray:
    centers = source.triangles_center
    distances, _ = target_tree.query(centers, workers=-1)
    return distances


def submesh_from_faces(mesh: trimesh.Trimesh, face_indices: np.ndarray) -> trimesh.Trimesh | None:
    if len(face_indices) == 0:
        return None
    parts = mesh.submesh([face_indices], append=True, repair=False)
    if isinstance(parts, trimesh.Trimesh) and not parts.is_empty:
        parts.remove_unreferenced_vertices()
        return parts
    return None


def distance_based_diff(
    old_mesh: trimesh.Trimesh,
    new_mesh: trimesh.Trimesh,
    tolerance: float,
    sample_points: int,
    warning: str | None,
) -> DifferenceMeshes:
    LOGGER.info("Computing point-to-surface distance fallback...")
    old_tree = sample_surface_tree(old_mesh, sample_points)
    new_tree = sample_surface_tree(new_mesh, sample_points)

    old_to_new = face_center_distances(old_mesh, new_tree)
    new_to_old = face_center_distances(new_mesh, old_tree)

    old_unchanged_faces = np.flatnonzero(old_to_new <= tolerance)
    old_removed_faces = np.flatnonzero(old_to_new > tolerance)
    new_added_faces = np.flatnonzero(new_to_old > tolerance)

    unchanged = submesh_from_faces(old_mesh, old_unchanged_faces)
    removed = submesh_from_faces(old_mesh, old_removed_faces)
    added = submesh_from_faces(new_mesh, new_added_faces)

    return DifferenceMeshes(
        unchanged=unchanged,
        removed=removed,
        added=added,
        method="distance",
        warning=warning,
    )


def compute_differences(
    old_mesh: trimesh.Trimesh,
    new_mesh: trimesh.Trimesh,
    tolerance: float,
    sample_points: int,
) -> DifferenceMeshes:
    LOGGER.info("Computing differences...")
    try:
        result = try_boolean_diff(old_mesh, new_mesh)
        if result:
            LOGGER.info("Boolean mesh difference succeeded.")
            return result
    except Exception as exc:
        warning = (
            "Boolean difference failed; falling back to distance-based coloring. "
            f"Possible visual artifacts near thin features or tight gaps. Reason: {exc}"
        )
        LOGGER.warning(warning)
        return distance_based_diff(old_mesh, new_mesh, tolerance, sample_points, warning)

    return distance_based_diff(old_mesh, new_mesh, tolerance, sample_points, None)


def trimesh_to_pyvista(mesh: trimesh.Trimesh) -> pv.PolyData:
    faces = np.hstack(
        [
            np.full((len(mesh.faces), 1), 3, dtype=np.int64),
            mesh.faces.astype(np.int64),
        ]
    ).ravel()
    return pv.PolyData(mesh.vertices, faces)


def add_mesh_if_present(
    plotter: pv.Plotter,
    mesh: trimesh.Trimesh | None,
    color: str,
    opacity: float = 1.0,
) -> None:
    if not nonempty_mesh(mesh):
        return
    plotter.add_mesh(
        trimesh_to_pyvista(mesh),
        color=color,
        opacity=opacity,
        smooth_shading=True,
        specular=0.25,
        specular_power=20,
    )


def set_camera_view(plotter: pv.Plotter, view_name: str) -> None:
    if view_name == "front":
        plotter.view_xz()
    elif view_name == "top":
        plotter.view_xy()
    elif view_name == "right":
        plotter.view_yz()
    elif view_name == "iso":
        plotter.view_isometric()
    else:
        raise ValueError(f"Unknown camera view: {view_name}")
    plotter.camera.zoom(1.15)


def render_views(diff: DifferenceMeshes, output_dir: Path) -> list[Path]:
    LOGGER.info("Rendering 1920x1080 images...")
    output_dir.mkdir(parents=True, exist_ok=True)
    pv.OFF_SCREEN = True

    views = ("front", "top", "right", "iso")
    image_paths: list[Path] = []

    for name in views:
        path = output_dir / f"cad_diff_{name}.png"
        plotter = pv.Plotter(off_screen=True, window_size=IMAGE_SIZE)
        plotter.set_background("white")
        add_mesh_if_present(plotter, diff.unchanged, "lightgray", opacity=0.55)
        add_mesh_if_present(plotter, diff.removed, "red", opacity=0.95)
        add_mesh_if_present(plotter, diff.added, "limegreen", opacity=0.95)
        plotter.add_axes(line_width=2, labels_off=False)
        plotter.reset_camera()
        set_camera_view(plotter, name)
        plotter.show(screenshot=str(path), auto_close=True)
        image_paths.append(path)
        LOGGER.info("Rendered %s", path)

    return image_paths


def tmpfiles_direct_url(url: str) -> str:
    return url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/")


def upload_to_tmpfiles(path: Path) -> str:
    LOGGER.info("Uploading %s to tmpfiles.org...", path.name)
    with path.open("rb") as file_handle:
        response = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": (path.name, file_handle, "image/png")},
            timeout=60,
        )
    response.raise_for_status()
    payload = response.json()
    url = payload.get("data", {}).get("url")
    if not url:
        raise ValueError(f"tmpfiles.org did not return a file URL: {payload}")
    return tmpfiles_direct_url(url)


def build_slack_payload(image_urls: Iterable[str], diff: DifferenceMeshes) -> dict:
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "New CAD iteration detected. Here are the geometric changes.",
            },
        }
    ]
    if diff.warning:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":warning: {diff.warning}"}],
            }
        )
    for index, url in enumerate(image_urls, start=1):
        blocks.append(
            {
                "type": "image",
                "image_url": url,
                "alt_text": f"CAD geometric diff view {index}",
            }
        )

    return {
        "text": "New CAD iteration detected. Here are the geometric changes.",
        "blocks": blocks,
    }


def post_to_slack(image_paths: list[Path], diff: DifferenceMeshes) -> None:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        LOGGER.warning("SLACK_WEBHOOK_URL is not set. Skipping Slack notification.")
        return

    LOGGER.info("Posting to Slack...")
    image_urls = [upload_to_tmpfiles(path) for path in image_paths]
    payload = build_slack_payload(image_urls, diff)
    response = requests.post(
        webhook_url,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    LOGGER.info("Slack notification posted.")


def main() -> int:
    configure_logging()
    load_dotenv()
    args = parse_args()

    try:
        LOGGER.info("Loading STLs...")
        old_mesh = load_mesh(args.old)
        new_mesh = load_mesh(args.new)

        LOGGER.info("Aligning meshes by centroid...")
        old_mesh, new_mesh = align_by_centroid(old_mesh, new_mesh)

        tolerance = args.tolerance if args.tolerance is not None else default_tolerance(old_mesh, new_mesh)
        LOGGER.info("Using unchanged tolerance: %.6g model units", tolerance)

        diff = compute_differences(old_mesh, new_mesh, tolerance, args.sample_points)
        LOGGER.info("Difference method: %s", diff.method)

        image_paths = render_views(diff, args.output_dir)

        if args.skip_slack:
            LOGGER.info("Skipping Slack notification by request.")
        else:
            try:
                post_to_slack(image_paths, diff)
            except Exception as exc:
                LOGGER.error("Slack notification failed: %s", exc)
                LOGGER.info("Rendered images are still available in %s", args.output_dir)

        LOGGER.info("Done.")
        return 0
    except Exception as exc:
        LOGGER.exception("CAD diff failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
