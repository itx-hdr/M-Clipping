"""Revision-bound exports scheduled with the owner's other heavy work."""
import hashlib
import json
import threading
import time

import sqlalchemy as sa
from fastapi import HTTPException

from .. import config, ctx, db, jobs, load, safe, storage
from . import compile, model, store

lock = threading.RLock()
outputs = sa.Table(
    "editor_outputs", db.meta,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("project_id", sa.String(40), nullable=False, index=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("progress", sa.Float, nullable=False),
    sa.Column("error", sa.String(500), nullable=False, default=""),
    sa.Column("digest", sa.String(64), nullable=False, default=""),
    sa.Column("created", sa.Float, nullable=False),
)


def folder(project_id):
    return config.user_dir(ctx.uid()) / "editor" / project_id


def public(row):
    return {key: row[key] for key in ("id", "revision", "status", "progress", "error", "digest")}


def get(connection, project_id, output_id):
    store.get(connection, project_id)
    row = connection.execute(outputs.select().where(outputs.c.project_id == project_id,
                             outputs.c.id == output_id, outputs.c.user_id == ctx.uid())).mappings().first()
    if row is None:
        raise HTTPException(404, "No such render.")
    return dict(row)


def active(connection, owner):
    return connection.execute(sa.select(outputs.c.id).where(outputs.c.user_id == owner,
                              outputs.c.status.in_(("queued", "running")))).first() is not None


def submit(project_id, revision):
    model.integer(revision)
    with lock, db.engine.begin() as connection:
        project = store.get(connection, project_id)
        if project["revision"] != revision:
            raise HTTPException(409, "Reload the draft before rendering its latest revision.")
        existing = connection.execute(outputs.select().where(outputs.c.project_id == project_id,
            outputs.c.revision == revision, outputs.c.status.in_(("queued", "running", "done")))
            .order_by(outputs.c.created.desc())).mappings().first()
        if existing:
            if existing["status"] != "done" or (folder(project_id) / (existing["id"] + ".mp4")).is_file():
                return public(existing)
            connection.execute(outputs.update().where(outputs.c.id == existing["id"])
                               .values(status="failed", error="The rendered file is missing. A replacement was queued."))
        if active(connection, ctx.uid()):
            raise HTTPException(429, "Wait for your current editor render to finish.")
        if compile.duration(json.loads(project["document"])) < 1:
            raise HTTPException(422, "Add content before rendering this sequence.")
        if not (folder(project_id) / "source.media").is_file():
            raise HTTPException(422, "Restore this project's source video before rendering.")
        if load.disk_full():
            raise HTTPException(507, storage.full_message())
        row = dict(id=model.identifier(), project_id=project_id, user_id=ctx.uid(), revision=revision,
                   document=project["document"], status="queued", progress=0, error="", digest="", created=time.time())
        connection.execute(outputs.insert().values(**row))
    jobs.submit_heavy(row["id"], lambda: run(row["id"]))
    ctx.bump()
    return public(row)


def run(output_id):
    with db.engine.begin() as connection:
        row = connection.execute(outputs.select().where(outputs.c.id == output_id,
                                 outputs.c.user_id == ctx.uid())).mappings().first()
        if row is None or row["status"] != "queued":
            return
        row = dict(row)
        changed = connection.execute(outputs.update().where(outputs.c.id == output_id, outputs.c.status == "queued")
                                     .values(status="running"))
        if changed.rowcount != 1:
            return
    stage = folder(row["project_id"]) / (output_id + ".part")
    final = stage.with_suffix(".mp4")

    def progress(value):
        if load.disk_full():
            raise RuntimeError(storage.full_message())
        with db.engine.begin() as connection:
            updated = connection.execute(outputs.update().where(outputs.c.id == output_id,
                                         outputs.c.status == "running").values(progress=value))
            if updated.rowcount != 1:
                raise RuntimeError("This render was cancelled.")
        ctx.bump()

    try:
        compile.render_sequence(folder(row["project_id"]) / "source.media", json.loads(row["document"]), stage, progress)
        if config.MAX_USER_GB and storage._size(config.user_dir(ctx.uid())) > config.MAX_USER_GB * 1024**3:
            raise RuntimeError("This render would exceed your storage limit.")
        digest = hashlib.sha256()
        with stage.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        with lock, db.engine.begin() as connection:
            current = get(connection, row["project_id"], output_id)
            if current["status"] != "running":
                raise RuntimeError("This render was cancelled.")
            stage.replace(final)
            connection.execute(outputs.update().where(outputs.c.id == output_id)
                               .values(status="done", progress=100, digest=digest.hexdigest()))
    except Exception as error:
        stage.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        with db.engine.begin() as connection:
            connection.execute(outputs.update().where(outputs.c.id == output_id, outputs.c.status == "running")
                               .values(status="failed", error=safe.friendly(error, 400)))
    ctx.bump()


def recover():
    """Interrupted exports can be retried. Queued exports retain their saved revision."""
    with db.engine.begin() as connection:
        connection.execute(outputs.update().where(outputs.c.status == "running").values(
            status="failed", error="Rendering stopped when the server restarted. Render this revision again."))
        waiting = list(connection.execute(outputs.select().where(outputs.c.status == "queued")
                                          .order_by(outputs.c.created)).mappings())
    for row in waiting:
        with ctx.as_user(row["user_id"]):
            jobs.submit_heavy(row["id"], lambda output_id=row["id"]: run(output_id))
            ctx.bump()
