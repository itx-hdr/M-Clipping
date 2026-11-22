"""Atomic project saves, retry handling and recoverable history."""
import hashlib
import json
import time

import sqlalchemy as sa
from fastapi import HTTPException

from .. import ctx, db
from . import model

projects = sa.Table(
    "editor_projects", db.meta,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
)
history = sa.Table(
    "editor_history", db.meta,
    sa.Column("project_id", sa.String(40), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("op_id", sa.String(80), nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("label", sa.String(200), nullable=False),
    sa.Column("created", sa.Float, nullable=False),
    sa.UniqueConstraint("project_id", "op_id"),
)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def get(connection, project_id):
    row = connection.execute(projects.select().where(
        projects.c.id == project_id, projects.c.user_id == ctx.uid())).mappings().first()
    if row is None:
        raise HTTPException(404, "No such editor project.")
    return dict(row)


def public(row):
    return {"id": row["id"], "name": row["name"], "revision": row["revision"],
            "document": json.loads(row["document"]), "origin": json.loads(row["origin"])}


def create(project_id, name, document, origin):
    now = time.time()
    row = dict(id=project_id, user_id=ctx.uid(), name=name, revision=0,
               document=encode(document), origin=encode(origin), created=now, updated=now)
    with db.engine.begin() as connection:
        connection.execute(projects.insert().values(**row))
        connection.execute(history.insert().values(project_id=project_id, revision=0, user_id=ctx.uid(),
                           op_id="initial", digest="", document=row["document"], label="Original assembly", created=now))
    ctx.bump()
    return public(row)


def change(project_id, body):
    op_id = body.get("op_id")
    if not isinstance(op_id, str) or not 8 <= len(op_id) <= 80:
        raise ValueError("Give this edit a unique request ID.")
    base = model.integer(body.get("base_revision"))
    digest = hashlib.sha256(encode(body).encode()).hexdigest()
    try:
        with db.engine.begin() as connection:
            row = get(connection, project_id)
            prior = connection.execute(history.select().where(
                history.c.project_id == project_id, history.c.op_id == op_id)).mappings().first()
            if prior:
                if prior["digest"] != digest:
                    raise HTTPException(409, "This request ID was already used for another edit.")
                return {"revision": prior["revision"], "document": json.loads(prior["document"])}
            if row["revision"] != base:
                raise HTTPException(409, "This project changed in another window. Reload before applying your edit.")
            if "suggestion_id" in body:
                from .recommendations import suggestions
                suggestion = connection.execute(suggestions.select().where(
                    suggestions.c.id == body["suggestion_id"], suggestions.c.project_id == project_id,
                    suggestions.c.user_id == ctx.uid())).mappings().first()
                expected = [{"type": "item.text", "item_id": suggestion["item_id"], "text": suggestion["text"]}] if suggestion else None
                if (suggestion is None or suggestion["status"] != "pending" or body.get("ops") != expected
                        or suggestion["revision"] != base or "restore_revision" in body):
                    raise HTTPException(409, "This recommendation is no longer available.")
                current_document = json.loads(row["document"])
                if suggestion["item_id"] in current_document["protected"]:
                    raise HTTPException(409, "This text is protected from AI changes.")
                accepted = connection.execute(suggestions.update().where(suggestions.c.id == suggestion["id"],
                    suggestions.c.status == "pending").values(status="applied"))
                if accepted.rowcount != 1:
                    raise HTTPException(409, "This recommendation was changed in another window.")
            if "restore_cut" in body:
                from . import firstcut
                document = firstcut.restore(connection, project_id, json.loads(row["document"]), body)
                label = "Restored one AI pacing cut"
            elif "first_cut_id" in body:
                from . import firstcut
                document = firstcut.accept(connection, project_id, base, json.loads(row["document"]), body)
                label = "AI pacing cut"
            elif "restore_revision" in body:
                revision = model.integer(body["restore_revision"])
                saved = connection.execute(history.select().where(history.c.project_id == project_id,
                                             history.c.revision == revision)).mappings().first()
                if saved is None:
                    raise ValueError("Choose an existing revision to restore.")
                document, label = json.loads(saved["document"]), f"Restored revision {revision}"
                if "restore_item_id" in body:
                    document = model.restore_item(json.loads(row["document"]), document, body["restore_item_id"])
                    label = f"Restored one item from revision {revision}"
            else:
                document = model.apply(json.loads(row["document"]), body.get("ops"))
                label = "Timeline edit"
            payload, now = encode(document), time.time()
            updated = connection.execute(projects.update().where(projects.c.id == project_id,
                                         projects.c.user_id == ctx.uid(), projects.c.revision == base)
                                         .values(revision=base + 1, document=payload, updated=now))
            if updated.rowcount != 1:
                raise HTTPException(409, "Another edit was saved first. Reload the project.")
            connection.execute(history.insert().values(project_id=project_id, revision=base + 1,
                               user_id=ctx.uid(), op_id=op_id, digest=digest, document=payload, label=label, created=now))
    except (sa.exc.IntegrityError, sa.exc.OperationalError) as error:
        raise HTTPException(409, "The project is busy. Reload and retry the edit.") from error
    ctx.bump()
    return {"revision": base + 1, "document": document}
