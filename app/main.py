"""HTTP routes and application factory."""
from __future__ import annotations

import os
from pathlib import Path

from litestar import Litestar, get, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from litestar.response import File
from litestar.static_files import StaticFilesConfig
from litestar.status_codes import HTTP_200_OK, HTTP_409_CONFLICT, HTTP_422_UNPROCESSABLE_ENTITY

from .db import (
    Database,
    MergeConflict,
    ServiceError,
    ValidationFailed,
    VersionConflict,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _db(app: Litestar) -> Database:
    return app.state.db


def _body(value: dict | None, name: str) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(detail=f"请求体必须是 JSON 对象，缺少 {name}", status_code=400)
    return value


@get("/api/health", status_code=HTTP_200_OK, tags=["api"], sync_to_thread=False)
def health() -> dict:
    return {"ok": True, "service": "spreadsheet-audit-console"}


@get("/api/workbook", tags=["api"], sync_to_thread=False)
def workbook(state: State) -> dict:
    return state.db.workbook()


@get("/api/branches", tags=["api"], sync_to_thread=False)
def branches(state: State) -> list[dict]:
    return state.db.branches()


@get("/api/versions", tags=["api"], sync_to_thread=False)
def versions(state: State, branch: str | None = None) -> list[dict]:
    return state.db.versions(branch)


@get("/api/versions/{version_id:int}", tags=["api"], sync_to_thread=False)
def version_detail(state: State, version_id: int) -> dict:
    try:
        version = state.db.version(version_id)
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc
    return {
        "version": version,
        "cells": state.db.cells(version_id),
        "edges": state.db.edges(version_id),
    }


@get("/api/versions/{version_id:int}/constraint-set", tags=["constraints"], sync_to_thread=False)
def version_constraint_set(state: State, version_id: int) -> dict:
    """审计查询：指定历史工作簿版本当时生效的约束集版本与完整规则。"""
    try:
        effective = state.db.effective_version_constraint_set(version_id)
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc
    return {"version_id": version_id, "constraint_set": effective}


@post("/api/validate", tags=["api"], status_code=HTTP_200_OK, sync_to_thread=False)
def validate(data: dict, state: State) -> dict:
    data = _body(data, "edits")
    try:
        return state.db.validate_edits(
            data.get("branch", "main"),
            data.get("edits", []),
            data.get("expected_version_number"),
        )
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(detail=str(exc), status_code=400) from exc


@post("/api/commits", tags=["api"], status_code=HTTP_200_OK, sync_to_thread=False)
def commit(data: dict, state: State) -> dict:
    data = _body(data, "edits")
    try:
        return state.db.commit_edits(
            data.get("branch", "main"),
            data.get("edits", []),
            data.get("message", "编辑提交"),
            data.get("expected_version_number"),
            data.get("merge_source_id"),
            data.get("timestamp"),
        )
    except ValidationFailed as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_422_UNPROCESSABLE_ENTITY) from exc
    except VersionConflict as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_409_CONFLICT) from exc
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(detail=str(exc), status_code=400) from exc


@post("/api/branches", tags=["api"], sync_to_thread=False)
def create_branch(data: dict, state: State) -> dict:
    data = _body(data, "name")
    from_version_id = data.get("from_version_id")
    if not isinstance(from_version_id, int):
        raise HTTPException(detail="from_version_id 必须是整数", status_code=400)
    try:
        return {"branch": state.db.create_branch(
            data.get("name", ""),
            from_version_id,
            data.get("timestamp"),
        )}
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(detail=str(exc), status_code=400) from exc


@post("/api/merges/preview", tags=["api"], status_code=HTTP_200_OK, sync_to_thread=False)
def merge_preview(data: dict, state: State) -> dict:
    data = _body(data, "target_branch")
    try:
        return state.db.merge_preview(data.get("target_branch", ""), data.get("source_branch", ""))
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc


@post("/api/merges", tags=["api"], status_code=HTTP_200_OK, sync_to_thread=False)
def merge(data: dict, state: State) -> dict:
    data = _body(data, "target_branch")
    expected = data.get("expected_version_number")
    if expected is not None and not isinstance(expected, int):
        raise HTTPException(detail="expected_version_number 必须是整数", status_code=400)
    try:
        return state.db.merge(
            data.get("target_branch", ""),
            data.get("source_branch", ""),
            data.get("message"),
            expected,
        )
    except MergeConflict as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_409_CONFLICT) from exc
    except ValidationFailed as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_422_UNPROCESSABLE_ENTITY) from exc
    except VersionConflict as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_409_CONFLICT) from exc
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc


@get("/api/constraint-sets", tags=["constraints"], sync_to_thread=False)
def constraint_sets(state: State) -> list[dict]:
    return state.db.constraint_sets()


@get("/api/constraint-sets/{name:str}", tags=["constraints"], sync_to_thread=False)
def constraint_set(state: State, name: str) -> dict:
    try:
        return state.db.constraint_set(name)
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc


@post("/api/constraint-sets", tags=["constraints"], status_code=HTTP_200_OK, sync_to_thread=False)
def create_constraint_set(data: dict, state: State) -> dict:
    data = _body(data, "rules")
    try:
        return {"constraint_set": state.db.save_constraint_set(
            data.get("name", ""),
            data.get("rules"),
            data.get("message", ""),
            data.get("expected_version_number"),
            data.get("timestamp"),
        )}
    except VersionConflict as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_409_CONFLICT) from exc
    except ValueError as exc:
        raise HTTPException(detail=str(exc), status_code=400) from exc


@post("/api/constraint-sets/{name:str}/versions", tags=["constraints"], status_code=HTTP_200_OK,
      sync_to_thread=False)
def update_constraint_set(data: dict, state: State, name: str) -> dict:
    data = _body(data, "rules")
    try:
        return {"constraint_set": state.db.save_constraint_set(
            name,
            data.get("rules"),
            data.get("message", ""),
            data.get("expected_version_number"),
            data.get("timestamp"),
        )}
    except VersionConflict as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_409_CONFLICT) from exc
    except ValueError as exc:
        raise HTTPException(detail=str(exc), status_code=400) from exc


@get("/api/constraint-bindings", tags=["constraints"], sync_to_thread=False)
def constraint_bindings(state: State) -> list[dict]:
    # 只返回分支绑定状态；版本钉版是另一类状态，见 /api/constraint-bindings/pins。
    return state.db.active_bindings()


@get("/api/constraint-bindings/history", tags=["constraints"], sync_to_thread=False)
def constraint_binding_history(state: State, branch: str = "main") -> list[dict]:
    try:
        return state.db.binding_history(branch)
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc


@get("/api/constraint-bindings/pins", tags=["constraints"], sync_to_thread=False)
def constraint_pin_history(state: State, branch: str = "main") -> list[dict]:
    """审计查询：分支归属的历史版本钉版记录（与分支绑定代数无关）。"""
    try:
        return state.db.pin_history(branch)
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc


@post("/api/constraint-bindings", tags=["constraints"], status_code=HTTP_200_OK, sync_to_thread=False)
def create_constraint_binding(data: dict, state: State) -> dict:
    data = _body(data, "constraint_set")
    set_version = data.get("constraint_set_version")
    if not isinstance(set_version, int):
        raise HTTPException(detail="constraint_set_version 必须是整数", status_code=400)
    version_id = data.get("version_id")
    if version_id is not None and not isinstance(version_id, int):
        raise HTTPException(detail="version_id 必须是整数", status_code=400)
    branch = data.get("branch")
    if not version_id and not branch:
        raise HTTPException(detail="必须提供 branch 或 version_id", status_code=400)
    if branch is not None and not isinstance(branch, str):
        raise HTTPException(detail="branch 必须是字符串", status_code=400)
    expected = data.get("expected_version_number")
    try:
        if version_id is not None:
            # 版本钉版：只改变目标版本的规则关联，不追加分支绑定、不改变
            # 分支当前规则与绑定代数；expected_version_number 校验目标版本
            # 的独立钉版修订号（未钉版为 0）。
            pin = state.db.create_pin(
                version_id,
                data.get("constraint_set", ""),
                set_version,
                expected,
                data.get("timestamp"),
                branch_name=branch,
            )
            return {"pin": pin, "binding": pin}
        # 分支换绑：沿用原绑定代数。
        binding = state.db.create_binding(
            branch,
            data.get("constraint_set", ""),
            set_version,
            expected,
            data.get("timestamp"),
        )
        return {"binding": binding}
    except VersionConflict as exc:
        raise HTTPException(detail=exc.detail, status_code=HTTP_409_CONFLICT) from exc
    except KeyError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(detail=str(exc), status_code=400) from exc


@get("/", include_in_schema=False, sync_to_thread=False)
def index() -> File:
    return File(path=STATIC_DIR / "index.html", filename="index.html")


def create_app(db_path: str | None = None, seed: bool = True) -> Litestar:
    if db_path is not None:
        path = db_path
    elif os.environ.get("SPREADSHEET_DB"):
        path = os.environ["SPREADSHEET_DB"]
    else:
        # 默认容器路径不存在时退回内存库，保证导入与本地测试不依赖卷挂载；
        # Docker Compose 始终通过 SPREADSHEET_DB 指向 /data/spreadsheet.db。
        default_path = "/data/spreadsheet.db"
        path = default_path if os.path.isdir("/data") else ":memory:"
    db = Database(path, seed=seed)

    app = Litestar(
        route_handlers=[
            health, workbook, branches, versions, version_detail, version_constraint_set,
            validate, commit, create_branch, merge_preview, merge,
            constraint_sets, constraint_set, create_constraint_set, update_constraint_set,
            constraint_bindings, constraint_binding_history, constraint_pin_history,
            create_constraint_binding,
            index,
        ],
        static_files_config=[
            StaticFilesConfig(path="/static", directories=[STATIC_DIR]),
        ],
        on_app_init=[],
        debug=False,
    )
    app.state.db = db
    return app


app = create_app()
