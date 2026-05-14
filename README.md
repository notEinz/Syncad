# MechDiff

MechDiff is a production-ready MVP for student mechatronic teams that need a visual CAD diff workflow without an external database. It stores uploaded revisions locally, tracks metadata in `revisions.json`, computes CAD differences on demand, and serves a dark themed browser viewer powered by three.js.

## Features

- Upload `.stl`, `.step`, `.obj`, and `.iges` files.
- Converts supported meshes to STL and stores normalized revisions locally.
- Decimates uploaded meshes to 100,000 faces when possible.
- Generates 512x512 thumbnails for each revision.
- Compares two revisions with centroid alignment.
- Uses boolean CAD diff when available and falls back to point-to-surface distance classification.
- Exports three PLY overlays:
  - gray unchanged geometry
  - green added material
  - red removed material
- Caches compare results in memory.
- Uses local folders and `revisions.json`; no external database.

## Project Structure

```text
app.py
requirements.txt
Procfile
README.md
static/
  index.html
  app.js
  style.css
  diff/
  thumbs/
uploads/
revisions/
revisions.json
```

The app auto-creates `static/diff`, `static/thumbs`, `uploads`, `revisions`, and `revisions.json` on startup.

## Local Run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Gunicorn

```bash
PORT=5000 gunicorn app:app --bind 0.0.0.0:$PORT
```

## Deploy

Use the included `Procfile`:

```text
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

Deploy to any Python platform that supports local disk writes and a `PORT` environment variable. For ephemeral platforms, uploaded revisions and generated diffs will reset when the instance filesystem resets.

## API

### POST `/upload`

Multipart form data:

- `file`: CAD file, required
- `tag`: optional label

Returns the stored revision metadata.

### GET `/revisions`

Returns all revisions sorted newest first.

### POST `/compare`

JSON body:

```json
{
  "id1": "revision-id-a",
  "id2": "revision-id-b"
}
```

Returns:

```json
{
  "id1": "revision-id-a",
  "id2": "revision-id-b",
  "method": "boolean",
  "warning": null,
  "urls": {
    "unchanged": "/static/diff/example_unchanged.ply",
    "added": "/static/diff/example_added.ply",
    "removed": "/static/diff/example_removed.ply"
  }
}
```
