"""Dashboard access to saved editor drafts."""
import hashlib
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from .. import config, ctx, db, jobs, load, media, storage
from . import exports, firstcut, model, recommendations, store

router = APIRouter(prefix="/api/editor")


def folder(project_id):
    return config.user_dir(ctx.uid()) / "editor" / project_id


@router.get("/projects")
def list_projects():
    with db.engine.connect() as connection:
        rows = connection.execute(store.projects.select().where(store.projects.c.user_id == ctx.uid())
                                  .order_by(store.projects.c.updated.desc()).limit(200)).mappings()
        return [{"id": row["id"], "name": row["name"], "revision": row["revision"]} for row in rows]


@router.post("/projects", status_code=201)
def create_project(body: dict = Body(...)):
    op_id = body.get("op_id")
    if not isinstance(op_id, str) or not 8 <= len(op_id) <= 80:
        raise HTTPException(422, "Give this project a unique request ID.")
    project_id = hashlib.sha256((ctx.uid() + ":" + op_id).encode()).hexdigest()[:32]
    request_hash = hashlib.sha256(store.encode(body).encode()).hexdigest()
    with jobs._lock:
        with db.engine.connect() as connection:
            existing = connection.execute(store.projects.select().where(
                store.projects.c.id == project_id, store.projects.c.user_id == ctx.uid())).mappings().first()
        if existing:
            if json.loads(existing["origin"])["request_hash"] != request_hash:
                raise HTTPException(409, "This request ID was already used for another project.")
            return store.public(existing)
        job = jobs.get(body.get("job_id"))
        if not job:
            raise HTTPException(404, "No such source video.")
        clip_id = body.get("clip_id")
        clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
        if clip_id and clip is None:
            raise HTTPException(404, "No such source clip.")
        if job["status"] in ("queued", "running", "live") or (clip and clip.get("status") in ("queued", "rendering")):
            raise HTTPException(409, "Wait for the source video to finish before opening an editor draft.")
        source_name = (clip or {}).get("src") or job.get("source_path")
        source = Path(source_name) if source_name else None
        if source is None or not source.is_file():
            raise HTTPException(422, "The original source is missing. Restore it before creating an editable draft.")
        size = source.stat().st_size
        if load.disk_full() or shutil.disk_usage(config.DATA_DIR).free < size + 256 * 1024 * 1024:
            raise HTTPException(507, "There is not enough space to retain this project's source video.")
        if config.MAX_USER_GB and storage._size(config.user_dir(ctx.uid())) + size > config.MAX_USER_GB * 1024 ** 3:
            raise HTTPException(507, "This project's source would exceed your storage limit.")
        destination = folder(project_id)
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise HTTPException(409, "This project is being created. Retry the same request shortly.") from error
        try:
            info = media.probe(source)
            offset = (clip or {}).get("src_offset", 0)
            selected = clip or {"start": 0, "end": info["duration"], "layout": "blur"}
            words_path = Path(job["dir"]) / "words.json"
            words = (clip or {}).get("fixed_words")
            if words is None:
                words = json.loads(words_path.read_text(encoding="utf-8")) if words_path.exists() else []
            document = model.import_clip(selected, words, info["duration"], offset)
            if clip is None:
                document["canvas"] = {"width": info["width"], "height": info["height"]}
            shutil.copyfile(source, destination / "source.media")
            origin = {"job_id": job["id"], "clip_id": clip_id, "source_offset": offset,
                      "request_hash": request_hash, "clip_version": (clip or {}).get("version", 0)}
            return store.create(project_id, str(body.get("name") or (clip or {}).get("title") or job.get("title")
                                               or "Editor draft")[:200], document, origin)
        except (ValueError, RuntimeError) as error:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(422, str(error)) from error
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    with db.engine.connect() as connection:
        return store.public(store.get(connection, project_id))


@router.get("/projects/{project_id}/source")
def get_source(project_id: str):
    with db.engine.connect() as connection:
        store.get(connection, project_id)
    path = folder(project_id) / "source.media"
    if not path.is_file():
        raise HTTPException(404, "The saved source is missing.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


@router.post("/projects/{project_id}/commands")
def commands(project_id: str, body: dict = Body(...)):
    try:
        return store.change(project_id, body)
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/projects/{project_id}/history")
def get_history(project_id: str):
    with db.engine.connect() as connection:
        store.get(connection, project_id)
        rows = connection.execute(store.history.select().where(store.history.c.project_id == project_id)
                                  .order_by(store.history.c.revision.desc()).limit(200)).mappings()
        return [{"revision": r["revision"], "label": r["label"], "created": r["created"]} for r in rows]


@router.get("/projects/{project_id}/history/{revision}")
def get_revision(project_id: str, revision: int):
    with db.engine.connect() as connection:
        store.get(connection, project_id)
        row = connection.execute(store.history.select().where(store.history.c.project_id == project_id,
                                 store.history.c.revision == revision, store.history.c.user_id == ctx.uid())).mappings().first()
        if row is None:
            raise HTTPException(404, "No such revision.")
        return {"revision": row["revision"], "document": json.loads(row["document"])}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    with exports.lock, db.engine.begin() as connection:
        store.get(connection, project_id)
        running = connection.execute(exports.outputs.select().where(exports.outputs.c.project_id == project_id,
                                     exports.outputs.c.status.in_(("queued", "running")))).first()
        if running:
            raise HTTPException(409, "Wait for this project's render to finish before deleting it.")
        planning = connection.execute(firstcut.passes.select().where(firstcut.passes.c.project_id == project_id,
                                      firstcut.passes.c.status.in_(("queued", "running")))).first()
        if planning:
            raise HTTPException(409, "Wait for this project's pacing pass or cancel it before deleting it.")
        connection.execute(firstcut.passes.delete().where(firstcut.passes.c.project_id == project_id))
        connection.execute(exports.outputs.delete().where(exports.outputs.c.project_id == project_id))
        connection.execute(recommendations.suggestions.delete().where(recommendations.suggestions.c.project_id == project_id))
        connection.execute(store.history.delete().where(store.history.c.project_id == project_id))
        connection.execute(store.projects.delete().where(store.projects.c.id == project_id))
    shutil.rmtree(folder(project_id), ignore_errors=True)
    ctx.bump()
    return {"ok": True}


@router.post("/projects/{project_id}/exports", status_code=202)
def create_export(project_id: str, body: dict = Body(...)):
    try:
        return exports.submit(project_id, body.get("revision"))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/projects/{project_id}/exports")
def list_exports(project_id: str):
    with db.engine.connect() as connection:
        store.get(connection, project_id)
        rows = connection.execute(exports.outputs.select().where(exports.outputs.c.project_id == project_id,
                                  exports.outputs.c.user_id == ctx.uid()).order_by(exports.outputs.c.created.desc()).limit(100)).mappings()
        return [exports.public(row) for row in rows]


@router.get("/projects/{project_id}/exports/{output_id}")
def export_status(project_id: str, output_id: str):
    with db.engine.connect() as connection:
        return exports.public(exports.get(connection, project_id, output_id))


@router.get("/projects/{project_id}/exports/{output_id}/media")
def export_media(project_id: str, output_id: str, download: int = 0):
    with db.engine.connect() as connection:
        row = exports.get(connection, project_id, output_id)
    path = folder(project_id) / (row["id"] + ".mp4")
    if row["status"] != "done" or not path.is_file():
        raise HTTPException(404, "This render is not ready to download.")
    return FileResponse(path, media_type="video/mp4", filename=f"edit-revision-{row['revision']}.mp4" if download else None,
                        headers={"Cache-Control": "private, no-store"})


@router.post("/projects/{project_id}/suggestions")
def suggest_text(project_id: str, body: dict = Body(...)):
    try:
        return recommendations.generate(project_id, body)
    except RuntimeError as error:
        from .. import safe
        raise HTTPException(502, safe.friendly(error, 300)) from error


@router.get("/projects/{project_id}/suggestions")
def list_suggestions(project_id: str):
    with db.engine.connect() as connection:
        store.get(connection, project_id)
        rows = connection.execute(recommendations.suggestions.select().where(
            recommendations.suggestions.c.project_id == project_id,
            recommendations.suggestions.c.user_id == ctx.uid()).order_by(recommendations.suggestions.c.created.desc()).limit(100)).mappings()
        return [recommendations.public(row) for row in rows]


@router.post("/projects/{project_id}/suggestions/{suggestion_id}/decision")
def decide_suggestion(project_id: str, suggestion_id: str, body: dict = Body(...)):
    return recommendations.decide(project_id, suggestion_id, body.get("decision"))


@router.post("/projects/{project_id}/first-cuts", status_code=202)
def create_first_cut(project_id: str, body: dict = Body(...)):
    try:
        return firstcut.submit(project_id, body)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/projects/{project_id}/first-cuts")
def list_first_cuts(project_id: str):
    with db.engine.connect() as connection:
        store.get(connection, project_id)
        rows = connection.execute(firstcut.passes.select().where(firstcut.passes.c.project_id == project_id,
                                  firstcut.passes.c.user_id == ctx.uid())
                                  .order_by(firstcut.passes.c.created.desc()).limit(30)).mappings()
        return [firstcut.public(row) for row in rows]


@router.get("/projects/{project_id}/first-cuts/{pass_id}")
def first_cut_status(project_id: str, pass_id: str):
    with db.engine.connect() as connection:
        return firstcut.public(firstcut.get(connection, project_id, pass_id))


@router.post("/projects/{project_id}/first-cuts/{pass_id}/decision")
def first_cut_control(project_id: str, pass_id: str, body: dict = Body(...)):
    try:
        return firstcut.control(project_id, pass_id, body.get("decision"))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
