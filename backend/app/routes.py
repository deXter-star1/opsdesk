"""HTTP routes. Deliberately thin: parse -> call a service -> return its result."""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config, db, schemas
from .errors import Forbidden, NotFound, Unprocessable
from .http import CSRF_HEADER, command, int_param, list_param, parse_body, query
from .jobs import queue
from .realtime import stream_endpoint
from .services import auth, items, notifications, queries
from .services.queries import ItemFilters


def _item_id(request: Request) -> int:
    return request.path_params["item_id"]


# --------------------------------------------------------------------------- auth

async def login(request: Request):
    body, _ = await parse_body(request, schemas.LoginIn)

    def run():
        with db.write_tx() as conn:
            return auth.login(conn, body.email, body.password)

    token, user = await run_in_threadpool(run)
    resp = JSONResponse({"user": {"id": user.id, "name": user.name, "email": user.email}})
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=config.settings.session_ttl_hours * 3600,
                    httponly=True, samesite="lax", secure=config.settings.cookie_secure, path="/")
    return resp


async def logout(request: Request):
    if request.headers.get(CSRF_HEADER) is None:
        raise Forbidden("Missing X-Requested-With header", code="csrf_check_failed")
    token = request.cookies.get(auth.SESSION_COOKIE)

    def run():
        if token:
            with db.write_tx() as conn:
                auth.logout(conn, token)

    await run_in_threadpool(run)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@query
def me(conn, user, request):
    teams = {t["id"]: t for t in queries.list_teams(conn, user)}
    return {
        "id": user.id, "name": user.name, "email": user.email, "is_admin": user.is_admin,
        "memberships": [{"team": {k: teams[tid][k] for k in ("id", "key", "name")}, "role": role}
                        for tid, role in sorted(user.roles.items(), key=lambda kv: teams[kv[0]]["name"])],
        "unread_notifications": notifications.unread_count(conn, user),
    }


# --------------------------------------------------------------------------- teams

@query
def teams(conn, user, request):
    return {"teams": queries.list_teams(conn, user)}


@query
def team_members(conn, user, request):
    return {"members": queries.team_members(conn, user, request.path_params["team_id"])}


# --------------------------------------------------------------------------- items: read

def _filters(request: Request) -> ItemFilters:
    qp = request.query_params
    try:
        return ItemFilters(
            q=qp.get("q") or None,
            view=qp.get("view") or None,
            team_ids=[int(x) for x in list_param(request, "team")],
            statuses=list_param(request, "status"),
            priorities=[int(x) for x in list_param(request, "priority")],
            kinds=list_param(request, "kind"),
            assignee=qp.get("assignee") or None,
            state=qp.get("state") or "active",
            sort=qp.get("sort") or "activity",
            cursor=qp.get("cursor") or None,
            limit=int_param(request, "limit", 25),
        )
    except ValueError:
        raise Unprocessable("team and priority filters must be integers", code="bad_param") from None


@query
def list_items(conn, user, request):
    return queries.list_items(conn, user, _filters(request))


@query
def count_items(conn, user, request):
    return queries.count_items(conn, user, _filters(request))


@query
def get_item(conn, user, request):
    return queries.item_detail(conn, user, _item_id(request))


@query
def item_events(conn, user, request):
    return queries.item_events(conn, user, _item_id(request), int_param(request, "before"),
                               int_param(request, "limit", 50))


@query
def dashboard(conn, user, request):
    return queries.dashboard(conn, user)


# --------------------------------------------------------------------------- items: write

@command(schemas.CreateItemIn, status_code=201)
def create_item(conn, user, request, body: schemas.CreateItemIn):
    return items.create_item(conn, user, body.model_dump())


@command(schemas.UpdateItemIn)
def update_item(conn, user, request, body: schemas.UpdateItemIn):
    changes = body.changes.provided()
    nulls = [k for k in schemas.ItemChanges.NON_NULLABLE if k in changes and changes[k] is None]
    if nulls:
        raise Unprocessable(f"Fields cannot be null: {nulls}", code="null_field")
    if not changes:
        raise Unprocessable("No changes supplied", code="empty_patch")
    return items.update_item(conn, user, _item_id(request), body.expected_version, changes)


@command()
def claim_item(conn, user, request, body):
    return items.claim(conn, user, _item_id(request))


@command(schemas.AssignIn)
def assign_item(conn, user, request, body: schemas.AssignIn):
    return items.assign(conn, user, _item_id(request), body.assignee_id, body.expected_version)


@command(schemas.TransitionIn)
def transition_item(conn, user, request, body: schemas.TransitionIn):
    return items.transition(conn, user, _item_id(request), body.action, body.expected_version, body.note)


@command(schemas.MoveIn)
def move_item(conn, user, request, body: schemas.MoveIn):
    return items.move(conn, user, _item_id(request), body.team_id, body.expected_version)


@command(schemas.CommentIn, status_code=201)
def comment_item(conn, user, request, body: schemas.CommentIn):
    return items.add_comment(conn, user, _item_id(request), body.body)


@command()
def watch_item(conn, user, request, body):
    return items.set_watch(conn, user, _item_id(request), request.method == "PUT")


# --------------------------------------------------------------------------- notifications

@query
def list_notifications(conn, user, request):
    return notifications.list_notifications(conn, user, int_param(request, "before"),
                                            request.query_params.get("unread") == "1",
                                            int_param(request, "limit", 30))


@command(schemas.MarkReadIn)
def mark_notifications_read(conn, user, request, body: schemas.MarkReadIn):
    return notifications.mark_read(conn, user, body.ids, body.all)


# --------------------------------------------------------------------------- admin

def _require_admin(user):
    if not user.is_admin:
        raise NotFound("Not found")


@query
def admin_jobs(conn, user, request):
    _require_admin(user)
    return {"stats": queue.stats(conn), "problem_jobs": queue.problem_jobs(conn)}


@command()
def admin_retry_job(conn, user, request, body):
    _require_admin(user)
    if not queue.retry_dead(conn, request.path_params["job_id"]):
        raise Unprocessable("Only dead jobs can be retried", code="not_dead")
    return {"ok": True}


async def health(request: Request):
    def run():
        db.get_conn().execute("SELECT 1").fetchone()
        return {"ok": True}
    return JSONResponse(await run_in_threadpool(run))


api_routes = [
    Route("/api/health", health, methods=["GET"]),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/me", me, methods=["GET"]),
    Route("/api/teams", teams, methods=["GET"]),
    Route("/api/teams/{team_id:int}/members", team_members, methods=["GET"]),
    Route("/api/dashboard", dashboard, methods=["GET"]),
    Route("/api/items", list_items, methods=["GET"]),
    Route("/api/items", create_item, methods=["POST"]),
    Route("/api/items/count", count_items, methods=["GET"]),
    Route("/api/items/{item_id:int}", get_item, methods=["GET"]),
    Route("/api/items/{item_id:int}", update_item, methods=["PATCH"]),
    Route("/api/items/{item_id:int}/events", item_events, methods=["GET"]),
    Route("/api/items/{item_id:int}/claim", claim_item, methods=["POST"]),
    Route("/api/items/{item_id:int}/assign", assign_item, methods=["POST"]),
    Route("/api/items/{item_id:int}/transitions", transition_item, methods=["POST"]),
    Route("/api/items/{item_id:int}/move", move_item, methods=["POST"]),
    Route("/api/items/{item_id:int}/comments", comment_item, methods=["POST"]),
    Route("/api/items/{item_id:int}/watch", watch_item, methods=["PUT", "DELETE"]),
    Route("/api/notifications", list_notifications, methods=["GET"]),
    Route("/api/notifications/read", mark_notifications_read, methods=["POST"]),
    Route("/api/stream", stream_endpoint, methods=["GET"]),
    Route("/api/admin/jobs", admin_jobs, methods=["GET"]),
    Route("/api/admin/jobs/{job_id:int}/retry", admin_retry_job, methods=["POST"]),
]
