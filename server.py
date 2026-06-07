"""
走廊 Corridor - 跨窗口状态同步 MCP Server

连接 claude.ai 里的卧室、书房、工坊等 project/chat：
- 横向切换：从一个房间 pack 便签，到另一个房间 arrive 接上状态。
- 纵向接续：同一房间新 chat 通过上次 session 记录接上温度。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP


# -- Config ---------------------------------------------------------------
DB_PATH = os.environ.get("CORRIDOR_DB", "/data/corridor.db")
TTL_MINUTES = int(os.environ.get("CORRIDOR_TTL", "60"))
PORT = int(os.environ.get("CORRIDOR_PORT", "8090"))
TRANSPORT = os.environ.get("CORRIDOR_TRANSPORT", "streamable-http")

mcp = FastMCP(
    "走廊 Corridor",
    instructions="跨窗口状态同步 - 连接你的卧室、书房和工坊",
    host="0.0.0.0",
    port=PORT,
)


# -- Database -------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _table_columns(c: sqlite3.Connection, table: str) -> set[str]:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _init() -> None:
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS handoffs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            from_room      TEXT NOT NULL,
            to_room        TEXT NOT NULL,
            recent_context TEXT NOT NULL DEFAULT '',
            note           TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            consumed       INTEGER DEFAULT 0,
            consumed_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            room       TEXT NOT NULL,
            summary    TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ho_to
            ON handoffs(to_room, consumed);
        CREATE INDEX IF NOT EXISTS idx_sess_room
            ON sessions(room, created_at);
        """
    )

    # Migrate v1 databases: handoffs used to have only note, no recent_context.
    if "recent_context" not in _table_columns(c, "handoffs"):
        c.execute("ALTER TABLE handoffs ADD COLUMN recent_context TEXT NOT NULL DEFAULT ''")
    c.execute("DROP TABLE IF EXISTS identity")
    c.execute("DROP TABLE IF EXISTS archives")

    c.commit()
    c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_time(value: str) -> str:
    return value[:16].replace("T", " ")


def _cleanup(c: sqlite3.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=TTL_MINUTES)).isoformat()
    c.execute("DELETE FROM handoffs WHERE created_at < ? AND consumed = 0", (cutoff,))


# -- Formatting -----------------------------------------------------------
def _loads_summary(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_list(label: str, values: Any) -> list[str]:
    if not values:
        return []
    if not isinstance(values, list):
        values = [str(values)]
    lines = [f"  {label}:"]
    lines.extend(f"    - {v}" for v in values)
    return lines


def _format_examples(examples: Any) -> list[str]:
    if not examples:
        return []
    if not isinstance(examples, list):
        examples = [examples]

    lines = ["  examples:"]
    for item in examples:
        if isinstance(item, dict):
            user = item.get("user")
            claude = item.get("claude")
            if user:
                lines.append(f'    - 用户: "{user}"')
            if claude:
                lines.append(f'    - Claude: "{claude}"')
        else:
            lines.append(f"    - {item}")
    return lines


def _format_session_summary(raw: str) -> str:
    data = _loads_summary(raw)
    if data is None:
        return raw

    lines: list[str] = []
    for key in ("mood", "intensity"):
        if data.get(key):
            lines.append(f"  {key}: {data[key]}")
    lines.extend(_format_list("key_events", data.get("key_events")))
    lines.extend(_format_list("unfinished", data.get("unfinished")))
    if data.get("style"):
        lines.append(f"  style: {data['style']}")
    lines.extend(_format_list("vibe", data.get("vibe")))
    lines.extend(_format_examples(data.get("examples")))
    return "\n".join(lines) if lines else raw


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def _coerce_examples(value: Any) -> list[dict[str, str] | str]:
    examples = _coerce_list(value)
    cleaned: list[dict[str, str] | str] = []
    for item in examples:
        if isinstance(item, dict):
            cleaned.append(
                {
                    "user": str(item.get("user", "")),
                    "claude": str(item.get("claude", "")),
                }
            )
        else:
            cleaned.append(str(item))
    return cleaned


# -- Tools ----------------------------------------------------------------
@mcp.tool()
def pack(from_room: str, to_room: str, recent_context: str, note: str) -> str:
    """写便签。用户说“我去书房了”“回卧室了”等房间切换时调用。

    Args:
        from_room: 当前房间，如“卧室”
        to_room: 目标房间，如“书房”
        recent_context: 最近几轮对话的压缩转述，要带语气和温度
        note: 状态摘要，写心情、要做什么、注意事项
    """
    c = _conn()
    c.execute(
        """
        INSERT INTO handoffs
            (from_room, to_room, recent_context, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (from_room, to_room, recent_context, note, _now()),
    )
    c.commit()
    c.close()
    return f"✓ 便签已放在{to_room}门口，来自{from_room}。"


@mcp.tool()
def arrive(room: str) -> str:
    """到达房间。读取门口待读便签，读完即焚。

    只返回便签内容。没有便签时返回空字符串。

    Args:
        room: 当前房间，如“卧室”“书房”“工坊”
    """
    c = _conn()
    _cleanup(c)

    parts: list[str] = []

    handoffs = c.execute(
        """
        SELECT id, from_room, recent_context, note, created_at
        FROM handoffs
        WHERE to_room = ? AND consumed = 0
        ORDER BY created_at DESC
        """,
        (room,),
    ).fetchall()

    if handoffs:
        now = _now()
        ids = [r["id"] for r in handoffs]
        placeholders = ",".join("?" * len(ids))
        c.execute(
            f"UPDATE handoffs SET consumed = 1, consumed_at = ? WHERE id IN ({placeholders})",
            [now, *ids],
        )
        for r in handoffs:
            lines = [
                f"📌 来自【{r['from_room']}】({_display_time(r['created_at'])}):",
            ]
            if r["recent_context"]:
                lines.append(f"  【过渡】{r['recent_context']}")
            lines.append(f"  【状态】{r['note']}")
            parts.append("\n".join(lines))

    c.commit()
    c.close()

    if not parts:
        return ""
    return "\n\n".join(parts)


@mcp.tool()
def wrap_up(
    room: str,
    mood: str,
    intensity: str,
    key_events: str,
    unfinished: str,
    style: str,
    vibe: str,
    examples: str,
) -> str:
    """收工存档。把本次 chat 结构化保存为 session 记录。

    Args:
        room: 当前房间
        mood: 一句话情绪状态
        intensity: 感情浓度，高/中/低 或“中偏高”等组合
        key_events: JSON 数组字符串，2-3 件重要事件，如 ["事件1", "事件2"]
        unfinished: JSON 数组字符串，没聊完、下次要接的事
        style: 一句话对话风格概括
        vibe: JSON 数组字符串，1-2 件有温度的小事
        examples: JSON 数组字符串，1-2 轮真实对话，每个对象包含 user 和 claude
    """
    summary = {
        "mood": mood,
        "intensity": intensity,
        "key_events": _coerce_list(key_events),
        "unfinished": _coerce_list(unfinished),
        "style": style,
        "vibe": _coerce_list(vibe),
        "examples": _coerce_examples(examples),
    }

    c = _conn()
    c.execute(
        "INSERT INTO sessions (room, summary, created_at) VALUES (?, ?, ?)",
        (room, json.dumps(summary, ensure_ascii=False), _now()),
    )
    c.commit()
    c.close()
    return f"✓ {room}的 session 记录已保存。"


@mcp.tool()
def peek(room: str, limit: int = 1) -> str:
    """看隔壁。查看某个房间最近的 session 记录。

    Args:
        room: 要查看的房间名，必须传
        limit: 返回条数，默认 1
    """
    c = _conn()
    rows = c.execute(
        """
        SELECT room, summary, created_at
        FROM sessions
        WHERE room = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (room, limit),
    ).fetchall()
    c.close()

    if not rows:
        return "📭 暂无记录。"

    entries = [
        f"📋 【{r['room']}】{_display_time(r['created_at'])}\n{_format_session_summary(r['summary'])}"
        for r in rows
    ]
    return "\n\n".join(entries)


# -- Entrypoint -----------------------------------------------------------
_init()

if __name__ == "__main__":
    mcp.run(transport=TRANSPORT)
