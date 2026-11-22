"""Saved, reviewable text recommendations bound to a timeline revision."""
import json
import time

import sqlalchemy as sa
from fastapi import HTTPException

from .. import brain, ctx, db
from . import model, store

suggestions = sa.Table(
    "editor_suggestions", db.meta,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("project_id", sa.String(40), nullable=False, index=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("item_id", sa.String(80), nullable=False),
    sa.Column("before_text", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("reason", sa.String(500), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("created", sa.Float, nullable=False),
)


def public(row):
    return {key: row[key] for key in ("id", "revision", "item_id", "before_text", "text", "reason", "status")}


def generate(project_id, body):
    instruction = body.get("instruction", "Correct obvious caption spelling and improve title clarity.")
    selected = body.get("item_ids")
    if not isinstance(instruction, str) or len(instruction) > 1000:
        raise HTTPException(422, "Describe the text changes in up to 1,000 characters.")
    if selected is not None and (not isinstance(selected, list) or len(selected) > 100 or any(not isinstance(i, str) for i in selected)):
        raise HTTPException(422, "Select up to 100 text items.")
    with db.engine.connect() as connection:
        project = store.get(connection, project_id)
        dismissed = set(connection.execute(sa.select(suggestions.c.item_id, suggestions.c.before_text, suggestions.c.text)
            .where(suggestions.c.project_id == project_id, suggestions.c.user_id == ctx.uid(),
                   suggestions.c.status == "dismissed")).tuples())
    if body.get("revision") != project["revision"]:
        raise HTTPException(409, "Reload the saved revision before requesting suggestions.")
    document = json.loads(project["document"])
    locked = {t["id"] for t in document["tracks"] if t["locked"]}
    items = {i["id"]: i for i in document["items"] if i["kind"] in ("title", "caption")
             and i["id"] not in document["protected"] and i["track_id"] not in locked
             and (selected is None or i["id"] in selected)}
    if not items:
        return []
    if len(items) > 100 or sum(len(i["text"]) for i in items.values()) > 16000:
        raise HTTPException(422, "Select a smaller group of captions for this recommendation pass.")
    answer = brain.editor_text_suggestions(list(items.values()), instruction)
    if not isinstance(answer, list):
        raise HTTPException(502, "The AI returned an invalid recommendation list. Try again.")
    rows, seen = [], set()
    for proposal in answer[:12]:
        if not isinstance(proposal, dict):
            continue
        item_id = proposal.get("item_id")
        if not isinstance(item_id, str) or item_id not in items or item_id in seen:
            continue
        text = proposal.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            continue
        text = brain._text(text, 2000)
        if not text.strip() or text == items[item_id]["text"] or (item_id, items[item_id]["text"], text) in dismissed:
            continue
        model.apply(document, [{"type": "item.text", "item_id": item_id, "text": text}])
        rows.append(dict(id=model.identifier(), project_id=project_id, user_id=ctx.uid(), revision=project["revision"],
                         item_id=item_id, before_text=items[item_id]["text"], text=text,
                         reason=brain._text(proposal.get("reason", "Suggested text correction"), 500),
                         status="pending", created=time.time()))
        seen.add(item_id)
    with db.engine.begin() as connection:
        current = store.get(connection, project_id)
        if current["revision"] != project["revision"]:
            raise HTTPException(409, "The draft changed while the AI was working. Request suggestions for the new revision.")
        if rows:
            connection.execute(suggestions.insert(), rows)
    ctx.bump()
    return [public(row) for row in rows]


def decide(project_id, suggestion_id, decision):
    if decision not in ("apply", "dismiss"):
        raise HTTPException(422, "Choose apply or dismiss.")
    with db.engine.connect() as connection:
        store.get(connection, project_id)
        row = connection.execute(suggestions.select().where(suggestions.c.id == suggestion_id,
                                 suggestions.c.project_id == project_id, suggestions.c.user_id == ctx.uid())).mappings().first()
    if row is None:
        raise HTTPException(404, "No such recommendation.")
    if row["status"] != "pending":
        return {"status": row["status"]}
    if decision == "apply":
        store.change(project_id, {"base_revision": row["revision"], "op_id": "suggestion-" + suggestion_id,
                     "suggestion_id": suggestion_id,
                     "ops": [{"type": "item.text", "item_id": row["item_id"], "text": row["text"]}]})
    else:
        with db.engine.begin() as connection:
            connection.execute(suggestions.update().where(suggestions.c.id == suggestion_id,
                               suggestions.c.status == "pending").values(status="dismissed"))
    ctx.bump()
    with db.engine.connect() as connection:
        status = connection.execute(sa.select(suggestions.c.status).where(suggestions.c.id == suggestion_id)).scalar_one()
    return {"status": status}
