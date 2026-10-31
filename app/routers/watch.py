"""自选股路由：分组管理与成分维护。

设计取舍
--------
* **分组必存在于每个账户**：新账户首次访问时自动创建「默认分组」，
  避免客户端要先判断「有没有分组」再决定要不要建，减少一次往返与一类空态分支。
* **一只股票可以同时在多个分组里**：加入自选时按分组逐条记录成员关系，
  重复加入同一分组是幂等的；取消自选才会清除该股票的全部成员关系。
  （上一版是「一支股票只属于一个分组」，加入别的分组等于移动，
  那样客户端无法做到「一次选多个分组」，长按多选就只能退化成单选。）
* **删除分组不删股票**：若该股票还属于其他分组，只丢掉这一条成员关系；
  否则挪到默认分组，防止误删自选。
* 列表返回时**顺带把实时行情拼好**，客户端一个请求就能渲染带涨跌幅的自选列表，
  不需要再按代码逐个查详情（N+1 请求在移动端很致命）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import get_session
from ..engine.market import market_engine
from ..models import Stock, User, WatchGroup, WatchItem
from ..services.deps import current_user, signed_request

router = APIRouter(prefix="/api/watch", tags=["watch"])

DEFAULT_GROUP_NAME = "默认分组"


def _ensure_default_group(session: Session, user_id: int) -> WatchGroup:
    """保证账户至少有一个分组。"""
    group = session.exec(
        select(WatchGroup).where(WatchGroup.user_id == user_id).order_by(WatchGroup.sort, WatchGroup.id)
    ).first()
    if group is None:
        group = WatchGroup(user_id=user_id, name=DEFAULT_GROUP_NAME, sort=0)
        session.add(group)
        session.commit()
        session.refresh(group)
    return group


def _group_view(g: WatchGroup, count: int) -> dict:
    return {"id": g.id, "name": g.name, "sort": g.sort, "count": count}


@router.get("/groups", dependencies=[Depends(signed_request)])
def list_groups(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    """分组列表（含每组的自选数量）。账号首次访问自动建默认分组。"""
    _ensure_default_group(session, user.id or 0)
    groups = session.exec(
        select(WatchGroup).where(WatchGroup.user_id == user.id).order_by(WatchGroup.sort, WatchGroup.id)
    ).all()
    counts: dict[int, int] = {}
    for item in session.exec(select(WatchItem).where(WatchItem.user_id == user.id)).all():
        counts[item.group_id] = counts.get(item.group_id, 0) + 1
    return {"items": [_group_view(g, counts.get(g.id or 0, 0)) for g in groups]}


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=12)


@router.post("/groups", dependencies=[Depends(signed_request)])
def create_group(
    payload: GroupIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """新建分组。同名分组视为「已存在」，直接返回既有分组而不是报错。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, detail={"code": "BAD_NAME", "message": "分组名称不能为空"})
    exists = session.exec(
        select(WatchGroup).where(WatchGroup.user_id == user.id, WatchGroup.name == name)
    ).first()
    if exists:
        return {"ok": True, "group": _group_view(exists, 0), "existed": True}

    total = len(session.exec(select(WatchGroup).where(WatchGroup.user_id == user.id)).all())
    group = WatchGroup(user_id=user.id or 0, name=name, sort=total)
    session.add(group)
    session.commit()
    session.refresh(group)
    return {"ok": True, "group": _group_view(group, 0), "existed": False}


@router.patch("/groups/{group_id}", dependencies=[Depends(signed_request)])
def rename_group(
    group_id: int,
    payload: GroupIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """重命名分组。与原组同名视为无操作，与其他分组重名则拒绝。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, detail={"code": "BAD_NAME", "message": "分组名称不能为空"})

    group = session.get(WatchGroup, group_id)
    if group is None or group.user_id != user.id:
        raise HTTPException(404, detail={"code": "GROUP_NOT_FOUND", "message": "分组不存在"})

    if group.name == name:
        return {"ok": True, "group": _group_view(group, 0), "changed": False}

    clash = session.exec(
        select(WatchGroup).where(
            WatchGroup.user_id == user.id, WatchGroup.name == name, WatchGroup.id != group_id
        )
    ).first()
    if clash is not None:
        raise HTTPException(400, detail={"code": "NAME_TAKEN", "message": "已存在同名分组"})

    group.name = name
    session.add(group)
    session.commit()
    session.refresh(group)
    count = len(
        session.exec(select(WatchItem).where(WatchItem.group_id == group.id)).all()
    )
    return {"ok": True, "group": _group_view(group, count), "changed": True}


@router.delete("/groups/{group_id}", dependencies=[Depends(signed_request)])
def delete_group(
    group_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """删除分组：不丢股票，至少保留一个分组。

    一只股票可能同时属于多个分组，所以不能无脑把成分挪到别的分组 ——
    那会让同时存在于目标分组里的股票变成两条成员关系（列表里重复出现）。
    这里的规则是：该股票若已在其他分组，直接丢掉这一条；否则挪到回退分组。
    """
    group = session.get(WatchGroup, group_id)
    if group is None or group.user_id != user.id:
        raise HTTPException(404, detail={"code": "GROUP_NOT_FOUND", "message": "分组不存在"})

    groups = session.exec(select(WatchGroup).where(WatchGroup.user_id == user.id)).all()
    if len(groups) <= 1:
        raise HTTPException(400, detail={"code": "LAST_GROUP", "message": "至少保留一个分组"})

    fallback = next((g for g in groups if g.id != group_id), None)
    # 该账户下每个股票已经存在于哪些分组，用于判断「是否还需要保留一条」
    other = session.exec(
        select(WatchItem).where(WatchItem.user_id == user.id, WatchItem.group_id != group_id)
    ).all()
    kept_elsewhere = {r.code for r in other}

    moved = 0
    dropped = 0
    for item in session.exec(select(WatchItem).where(WatchItem.group_id == group_id)).all():
        if item.code in kept_elsewhere or fallback is None:
            session.delete(item)
            dropped += 1
            continue
        item.group_id = fallback.id or 0
        session.add(item)
        moved += 1
    session.delete(group)
    session.commit()
    return {
        "ok": True,
        "moved": moved,
        "dropped": dropped,
        "fallback_group_id": fallback.id if fallback else 0,
    }


class GroupOrderIn(BaseModel):
    """分组顺序：按列表下标依次写入 sort。"""

    ids: list[int] = Field(min_length=1)


@router.patch("/groups/order", dependencies=[Depends(signed_request)])
def reorder_groups(
    payload: GroupOrderIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """调整分组顺序（拖拽排序）。

    * 只接受属于当前账户的分组；请求里出现别人的分组一律忽略，不做任何改动，
      避免越权把别人的分组顺序写坏。
    * 未出现在列表里的分组保留原有 sort —— 客户端可能只提交了当前可见的一屏，
      缺席的分组不应被重排到前面去。
    * sort 按下标重排为 0..n-1，列表接口本就按 (sort, id) 排序，读回来就是拖拽后的顺序。
    """
    owned = {g.id: g for g in session.exec(select(WatchGroup).where(WatchGroup.user_id == user.id)).all()}
    seen: set[int] = set()
    order = 0
    for gid in payload.ids:
        g = owned.get(gid)
        if g is None or gid in seen:
            continue
        seen.add(gid)
        g.sort = order
        session.add(g)
        order += 1
    session.commit()
    return {"ok": True, "updated": len(seen)}


class ItemIn(BaseModel):
    code: str = Field(min_length=6, max_length=12)
    group_id: int | None = None


def _resolve_group(session: Session, user: User, group_id: int | None) -> WatchGroup:
    """把请求里的 group_id 解析成一个属于当前账户的分组；缺省用第一个分组。

    第一个分组即分组列表的首项（按 sort, id 排序）——「加入自选默认进第一个分组」
    就是在这里落的，客户端不传 group_id 时不需要自己再查一遍。
    """
    if group_id:
        target = session.get(WatchGroup, group_id)
        if target is None or target.user_id != user.id:
            raise HTTPException(404, detail={"code": "GROUP_NOT_FOUND", "message": "分组不存在"})
        return target
    return _ensure_default_group(session, user.id or 0)


def _add_membership(session: Session, user_id: int, group_id: int, code: str) -> bool:
    """在指定分组里加入成员关系，已存在则跳过。返回是否真的新建。"""
    exists = session.exec(
        select(WatchItem).where(
            WatchItem.user_id == user_id, WatchItem.group_id == group_id, WatchItem.code == code
        )
    ).first()
    if exists is not None:
        return False
    session.add(WatchItem(user_id=user_id, group_id=group_id, code=code))
    return True


@router.post("/items", dependencies=[Depends(signed_request)])
def add_item(
    payload: ItemIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """加为自选（可指定分组，缺省为第一个分组）。

    幂等：同一分组里重复加入不会产生重复行，客户端可以放心连点。
    已在其他分组时不会被移走 —— 一只股票允许同时出现在多个分组
    （长按加自选的多选就用这套语义）。
    """
    if session.get(Stock, payload.code) is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})

    group = _resolve_group(session, user, payload.group_id)
    created = _add_membership(session, user.id or 0, group.id or 0, payload.code)
    session.commit()
    return {"ok": True, "watching": True, "group_id": group.id, "created": created}


class BatchItemIn(BaseModel):
    """一次加入多个分组（长按加自选的多选提交）。"""

    code: str = Field(min_length=6, max_length=12)
    group_ids: list[int] = Field(min_length=1)


@router.post("/items/batch", dependencies=[Depends(signed_request)])
def add_item_batch(
    payload: BatchItemIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """把一只股票一次加入多个分组。

    整体要么都加、要么都不加：先校验标的与所有分组都合法，再写库。
    否则「选了三组、只有前两组成功」会让用户看到半截状态，很难自己发现。
    空列表视为加入第一个分组，与单击加自选的行为保持一致。
    """
    if session.get(Stock, payload.code) is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})

    targets: list[WatchGroup] = []
    seen: set[int] = set()
    for gid in payload.group_ids:
        g = _resolve_group(session, user, gid)
        if (g.id or 0) in seen:
            continue
        seen.add(g.id or 0)
        targets.append(g)

    for g in targets:
        _add_membership(session, user.id or 0, g.id or 0, payload.code)
    session.commit()
    return {"ok": True, "watching": True, "group_ids": [g.id for g in targets]}


@router.delete("/items/{code}", dependencies=[Depends(signed_request)])
def remove_item(
    code: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """取消自选：清除该股票在所有分组里的成员关系。不存在也返回成功（幂等）。

    一只股票可能同时属于多个分组，取消自选的含义是「我不再关注它」，
    因此一次清干净，而不是只摘掉某一个分组里的那一条。
    """
    rows = session.exec(
        select(WatchItem).where(WatchItem.user_id == user.id, WatchItem.code == code)
    ).all()
    for r in rows:
        session.delete(r)
    if rows:
        session.commit()
    return {"ok": True, "watching": False, "removed": len(rows)}


@router.get("/items", dependencies=[Depends(signed_request)])
def list_items(
    group_id: int | None = Query(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """自选列表（默认只返回指定分组；不传 group_id 则返回全部分组的扁平结果）。

    返回里已拼好实时行情，客户端直接渲染即可。
    """
    rows = session.exec(select(WatchItem).where(WatchItem.user_id == user.id)).all()
    if group_id:
        rows = [r for r in rows if r.group_id == group_id]
    stocks = {s.code: s for s in session.exec(select(Stock)).all()}
    quotes = market_engine.quotes()

    items = []
    for r in rows:
        meta = stocks.get(r.code)
        q = quotes.get(r.code)
        if meta is None:
            continue
        items.append(
            {
                "code": r.code,
                "name": meta.name,
                "group_id": r.group_id,
                "industry": meta.industry,
                "board": meta.board,
                # 行情可能因引擎未注册而缺失，此时给出价格占位而不是丢行
                "price": (q or {}).get("price", meta.prev_close),
                "change": (q or {}).get("change", 0.0),
                "change_pct": (q or {}).get("change_pct", 0.0),
                "amount": (q or {}).get("amount", 0.0),
                "prev_close": (q or {}).get("prev_close", meta.prev_close),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/status/{code}", dependencies=[Depends(signed_request)])
def watch_status(
    code: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """查询单个标的是否已在自选，以及它在哪些分组里。

    `group_ids` 供「长按加自选」的多选抽屉回显已有分组 —— 已经加过的分组
    默认勾上并置灰，用户不会以为自己漏加了。
    """
    rows = session.exec(
        select(WatchItem).where(WatchItem.user_id == user.id, WatchItem.code == code)
    ).all()
    gids = sorted({r.group_id for r in rows if r.group_id})
    return {"code": code, "watching": bool(rows), "group_ids": gids}
