"""The Mind-map routes, on the board log and the public core.

Every route sits behind the server's guard (session, and own Origin for anything that is not
GET), so nothing here checks either again. Boards live in ``omli.boards``; every model call
goes through ``omli.boards.assist`` (Agent.run between the public gates). A save makes no
model call; a checkpoint is its own explicit action.

    GET  /api/mindmap/list                          boards, optionally searched (?q=)
    GET  /api/mindmap/boards/{board}                the board replayed from its log
    POST /api/mindmap/save                          a whole board -> operations, one write
    GET  /api/mindmap/boards/{board}/attribution    last editor and edit counts per node
    GET  /api/mindmap/boards/{board}/checkpoints    the checkpoint lineage
    POST /api/mindmap/boards/{board}/checkpoints    make a checkpoint
    POST /api/mindmap/boards/{board}/restore        append a restore to an earlier operation
    POST /api/mindmap/expand | analyze | interpret  one gated model call
    POST /api/mindmap/icon                          an icon from pack rules
    POST /api/mindmap/dump                          a dropped file -> text -> a sorted node
    GET  /api/mindmap/dump/config                   the dump vocabularies
    GET  /api/mindmap/rag/catalog                   the user's folders and model roles
    POST /api/mindmap/rag/validate                  a pure check of a ladder
    POST /api/mindmap/rag/run                       retrieval in listed folders + one summary
    POST /api/mindmap/autodesign/question | build  offline only
    GET  /api/mindmap/assist/status                 is a model configured; if not, how to add one
    GET  /mindmap                                   the page
    GET  /mindmap/{asset}                           a file the page loads, from an exact list

The page and its files are package data (``omli/ui/static/mindmap``). They are read once at
registration into an exact list keyed by their path inside that folder, and a request is
answered only by an exact match, so no path from a request ever reaches the file system.

The web stack is imported inside ``register``, which only ``omli.ui.server`` calls, so
importing this module loads no web package. (No ``from __future__ import annotations``: the
route handlers' ``Request`` annotations must be real objects for FastAPI.)
"""

import base64
import binascii
from typing import Any, Callable, Optional

from importlib import resources

from omli.boards import (BoardConflict, BoardLog, BoardLogError, install_starter, list_boards,
                         node_stats)
from omli.boards import autodesign, extract, ladder, retrieve
from omli.boards.assist import Assistant, AssistResult, NoModelConfigured, board_sensitivity
from omli.boards.checkpoint import create_checkpoint, list_checkpoints
from omli.boards.log import content_head
from omli.boards.diff import check_board, ops_for
from omli.boards.icon import icon_for
from omli.boards.vocab import plain, section
from omli.sensitivity import CORE_LABELS, LOCAL_ONLY_TAG
from omli.ui.home import ui_home

MAX_TEXT = 4096
MAX_QUERY = 2000
PAGE_PACKAGE = "omli.ui.static.mindmap"
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def page_files() -> dict:
    """Every file the page ships, keyed by its path inside the page folder, with its bytes.
    Python files are the package's own markers and are never served."""
    out = {}

    def walk(node, prefix):
        for child in node.iterdir():
            rel = prefix + child.name
            if child.is_dir():
                if child.name != "__pycache__":
                    walk(child, rel + "/")
            elif not child.name.endswith((".py", ".pyc")):
                out[rel] = child.read_bytes()
    walk(resources.files(PAGE_PACKAGE), "")
    return out


def _media_type(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[1] if "." in name else ""
    return _TYPES.get(suffix, "text/plain; charset=utf-8")


async def offload(fn: Callable[..., Any], *args: Any) -> Any:
    """Run blocking work (file reads and writes, folder walks, model calls) in the server's
    thread pool, so one slow request never holds up the others."""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(fn, *args)


def _error(status: int, message: str, **extra):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": message, **extra}, status_code=status)


async def _body(request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _assisted(result: AssistResult, **extra):
    from fastapi.responses import JSONResponse
    if result.ok:
        return JSONResponse({"text": result.text, "input_id": result.input_id,
                             "output_id": result.output_id, **extra})
    status = 502 if result.stage == "model" else 422
    return JSONResponse({"refused": result.stage, "reason": result.reason}, status_code=status)


def register(app, *, home, projects) -> None:
    """Add the Mind-map routes to ``app`` (a FastAPI app). ``projects`` is the user's
    ProjectStore."""
    from fastapi import Request

    assistant = Assistant(home=home)
    data_home = ui_home(home)  # retrieval never reads the UI's own data
    install_starter(home)  # the first-run board; an existing board of that name is kept
    files = page_files()

    def board_call(fn: Callable[[], Any]):
        try:
            return fn()
        except BoardLogError as exc:  # a torn or malformed log: name the line, change nothing
            return _error(409, str(exc))
        except KeyError as exc:
            return _error(404, str(exc.args[0]) if exc.args else "not found")
        except ValueError as exc:
            return _error(400, str(exc))

    def assist(action: str, fields: dict, sensitivity=(), refs=(), **extra):
        try:
            return _assisted(assistant.run(action, fields, sensitivity=sensitivity,
                                           source_refs=refs), **extra)
        except NoModelConfigured as exc:
            return _error(409, str(exc))
        except ValueError as exc:  # an unknown sensitivity tag fails closed
            return _error(400, str(exc))

    def existing(board: Any):
        log = BoardLog(board, home)
        records = log.ops()
        if not records:
            raise KeyError(f"there is no board {board!r}")
        return log, records

    # --- boards -------------------------------------------------------------------------

    @app.get("/api/mindmap/list")
    async def mindmap_list(q: Optional[str] = None):
        return await offload(board_call, lambda: {"boards": list_boards(_text(q, 200) or None, home=home)})

    @app.get("/api/mindmap/boards/{board}")
    async def mindmap_load(board: str):
        def load():
            log, records = existing(board)
            return {"board": board, **log.replay(), "head": content_head(records),
                    "relations": plain(section("relations"))}
        return await offload(board_call, load)

    @app.post("/api/mindmap/save")
    async def mindmap_save(request: Request):
        body = await _body(request)
        by = request.state.session.pubkey_id

        def save():
            # Every save names the version it was based on ("base": the board's last operation
            # id, or null for a new board). The check, the diff and the write happen under the
            # board's lock, and the head returned is the one seen there, so a save never lands
            # on top of an edit it has not seen.
            log = BoardLog(body.get("board"), home)
            wanted = {"nodes": body.get("nodes", {}), "links": body.get("links", {})}
            try:
                written, head = log.save(lambda state: ops_for(state, wanted), by=by,
                                         expect_head=body.get("base"))
            except BoardConflict as exc:
                return _error(409, str(exc), head=exc.head)
            return {"written": len(written), "head": head}
        return await offload(board_call, save)

    @app.get("/api/mindmap/boards/{board}/attribution")
    async def mindmap_attribution(board: str):
        def stats():
            log, _ = existing(board)
            return {"board": board, "nodes": node_stats(log)}
        return await offload(board_call, stats)

    @app.get("/api/mindmap/boards/{board}/checkpoints")
    async def mindmap_checkpoints(board: str):
        return await offload(board_call, lambda: {"checkpoints": list_checkpoints(board, home=home)})

    @app.post("/api/mindmap/boards/{board}/checkpoints")
    async def mindmap_checkpoint(board: str, request: Request):
        label = (await _body(request)).get("label")
        by = request.state.session.pubkey_id
        return await offload(board_call, lambda: create_checkpoint(board, label, by=by, home=home))

    @app.post("/api/mindmap/boards/{board}/restore")
    async def mindmap_restore(board: str, request: Request):
        body = await _body(request)
        to = body.get("to")
        by = request.state.session.pubkey_id

        def restore():
            # Like a save, a restore names the version it was based on and is refused (409)
            # unless that is the board's current end, so it never undoes an edit not seen.
            log, _ = existing(board)
            if not isinstance(to, str) or not to:
                raise ValueError("restore needs the id of an earlier operation")
            try:
                _, head = log.save(lambda state: [{"kind": "restore", "to": to}], by=by,
                                   expect_head=body.get("base"))
            except BoardConflict as exc:
                return _error(409, str(exc), head=exc.head)
            return {"head": head, **log.replay(upto=head)}
        return await offload(board_call, restore)

    # --- model calls: one Agent.run each -------------------------------------------------

    async def focused(request: Request, action: str):
        body = await _body(request)

        def build():
            log, _ = existing(body.get("board"))
            state = log.replay()
            node = body.get("node")
            if not isinstance(node, str) or node not in state["nodes"]:
                raise KeyError("that node is not on the board")
            near = [lk["to"] if lk["from"] == node else lk["from"]
                    for lk in state["links"].values() if node in (lk["from"], lk["to"])]
            fields = {"focus": _text(state["nodes"][node].get("text")),
                      "neighbours": [t for t in (_text(state["nodes"][n].get("text"))
                                                 for n in near) if t]}
            return assist(action, fields, board_sensitivity(state),
                          (f"board:{body.get('board')}", f"node:{node}"))
        return await offload(board_call, build)

    @app.post("/api/mindmap/expand")
    async def mindmap_expand(request: Request):
        return await focused(request, "expand")

    @app.post("/api/mindmap/analyze")
    async def mindmap_analyze(request: Request):
        return await focused(request, "analyze")

    @app.post("/api/mindmap/interpret")
    async def mindmap_interpret(request: Request):
        body = await _body(request)

        def build():
            log, _ = existing(body.get("board"))
            state = log.replay()
            text = {nid: _text(n.get("text")) for nid, n in state["nodes"].items()}
            lines = [f"- {t}" for t in text.values() if t]
            lines += [f"{text[lk['from']]} -> {text[lk['to']]}"
                      for lk in state["links"].values() if text[lk["from"]] and text[lk["to"]]]
            return assist("interpret", {"board": "\n".join(lines)}, board_sensitivity(state),
                          (f"board:{body.get('board')}",))
        return await offload(board_call, build)

    # --- icon, dump config, ladder check ------------------------------------------------

    @app.post("/api/mindmap/icon")
    async def mindmap_icon(request: Request):
        body = await _body(request)
        return {"icon": icon_for(_text(body.get("text")), _text(body.get("glyph"), 8))}

    @app.get("/api/mindmap/dump/config")
    async def mindmap_dump_config():
        dump = plain(section("dump"))
        return {"entity_types": dump["entity_types"], "status": dump["status"],
                "tags": dump["tags"], "editable": dump["editable"],
                "file_types": sorted(dump["file_types"]), "max_bytes": dump["max_bytes"],
                "sensitivity": sorted(CORE_LABELS) + [LOCAL_ONLY_TAG]}

    @app.post("/api/mindmap/dump")
    async def mindmap_dump(request: Request):
        body = await _body(request)
        name = _text(body.get("filename"), 255)
        content = body.get("content")
        limit = extract.limits()["max_bytes"]
        if not isinstance(content, str) or not name:
            return _error(400, "dump needs a filename and base64 content")
        if len(content) > (limit * 4) // 3 + 8:
            return _error(413, f"the file is over the {limit} byte limit")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            return _error(400, "the content is not valid base64")
        try:
            got = await offload(extract.extract, data, name)
        except extract.UnsupportedType as exc:
            return _error(415, str(exc))
        except extract.TooLarge as exc:
            return _error(413, str(exc))
        except extract.ExtractError as exc:  # includes a missing document library, named
            return _error(422, str(exc))
        sensitivity = body.get("sensitivity") or []
        if not isinstance(sensitivity, (list, str)):
            return _error(400, "sensitivity is a tag or a list of tags")
        tags = (sensitivity,) if isinstance(sensitivity, str) else tuple(sensitivity)
        return await offload(lambda: assist("dump", {"filename": name, "text": got.text}, tags,
                                            (f"file:{name}",), method=got.method,
                                            truncated=got.truncated))

    def ladder_board(body: dict):
        if "board" in body:
            log, _ = existing(body["board"])
            return log.replay()
        return check_board({"nodes": body.get("nodes", {}), "links": body.get("links", {})})

    @app.post("/api/mindmap/rag/validate")
    async def mindmap_rag_validate(request: Request):
        body = await _body(request)
        return await offload(board_call, lambda: ladder.validate_ladder(ladder_board(body)))

    # --- retrieval over the user's own folders ------------------------------------------

    @app.get("/api/mindmap/rag/catalog")
    async def mindmap_rag_catalog():
        return await offload(board_call, lambda: retrieve.catalog(projects.list(), assistant.registry()))

    @app.post("/api/mindmap/rag/run")
    async def mindmap_rag_run(request: Request):
        body = await _body(request)
        query = _text(body.get("query"), MAX_QUERY)

        def run():
            graph = ladder_board(body)
            checked = ladder.validate_ladder(graph)
            if not checked["ok"]:
                return _error(400, "the ladder is not well formed", errors=checked["errors"])
            steps = ladder.rungs(graph)
            folders = projects.list()
            try:
                roots = [retrieve.source_root(steps[i].get("catalog_ref", {}).get("path"),
                                              folders)
                         for i in checked["order"] if steps[i]["kind"] == "source"]
            except retrieve.OutsideProjects as exc:
                return _error(403, str(exc))
            ks = [s.get("params", {}).get("k") for s in steps.values()
                  if s["kind"] == "retriever"]
            k = next((x for x in ks if isinstance(x, int) and not isinstance(x, bool)
                      and 0 < x <= 50), None)
            hits = retrieve.retrieve(query, roots, k, exclude=[data_home])
            snippets = "\n".join(f"[{h['file']}] {h['snippet']}" for h in hits)
            return assist("rag", {"question": query, "snippets": snippets},
                          board_sensitivity(graph), tuple(f"folder:{r}" for r in roots),
                          hits=hits, order=checked["order"])
        return await offload(board_call, run)

    @app.get("/api/mindmap/assist/status")
    async def mindmap_assist_status():
        return await offload(assistant.status)

    # --- the page -----------------------------------------------------------------------

    def _file(rel: str):
        from fastapi.responses import Response
        data = files.get(rel)
        if data is None:
            return _error(404, "not found")
        return Response(data, media_type=_media_type(rel))

    @app.get("/mindmap")
    async def mindmap_page():
        return _file("index.html")

    @app.get("/mindmap/{asset:path}")
    async def mindmap_asset(asset: str):
        return _file(asset)

    # --- offline auto-design ------------------------------------------------------------

    @app.post("/api/mindmap/autodesign/question")
    async def mindmap_autodesign_question(request: Request):
        answers = (await _body(request)).get("answers")
        return {**autodesign.next_question(answers if isinstance(answers, dict) else {}),
                "offline": True}

    @app.post("/api/mindmap/autodesign/build")
    async def mindmap_autodesign_build(request: Request):
        answers = (await _body(request)).get("answers")
        return {"board": autodesign.build_board(answers if isinstance(answers, dict) else {}),
                "offline": True}
