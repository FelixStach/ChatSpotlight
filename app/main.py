import asyncio
import hmac
import json
import logging
import os
import secrets
import ssl
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from contextlib import suppress

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

# Core paths and files
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_DIR = BASE_DIR / "config"
USERNAME_FILE = CONFIG_DIR / "username.txt"
CHANNEL_FILE = CONFIG_DIR / "channel.txt"
TOKEN_FILE = CONFIG_DIR / "oauth_token.txt"
PASSWORD_FILE = CONFIG_DIR / "password.txt"

# Twitch / chat settings
IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697
HISTORY_LIMIT = 100
RETRY_DELAY_SECONDS = 5
USE_FAKE_STREAM = False
FAKE_CHANNEL_NAME = "demo"
TOTAL_QUEUE_SLOTS = 3
IRC_MESSAGE_LIMIT = 380

# Routing and session settings
PATH_PREFIX = ""
PREFIX = f"/{PATH_PREFIX}" if PATH_PREFIX else ""
SESSION_TTL_SECONDS = 43200
SESSION_HEADER = "X-Session-Token"
SESSION_COOKIE = "chatspotlight_session"
SECURE_COOKIES = False
PROTECTED_EVENTS = {"highlight", "clearHighlight", "pin", "unpin", "rumble"}

CHANNEL_NAME: Optional[str] = None

app = FastAPI(title="Twitch Chat Helper", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if PREFIX:
    # Serve static files under the prefixed path for reverse-proxy setups.
    app.mount(f"{PREFIX}/static", StaticFiles(directory=STATIC_DIR), name="static-prefixed")

recent_messages: Deque[Dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
message_lookup: Dict[str, Dict[str, Any]] = {}
highlight_queue: List[Dict[str, Any]] = []
active_sessions: Dict[str, float] = {}
ADMIN_PASSWORD: Optional[str] = None
twitch_send_queue: Optional[asyncio.Queue[str]] = None


def required_file(path: Path, label: str) -> str:
    example_path = path.with_name(f"{path.stem}.example{path.suffix}")
    try:
        content = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        message = (
            f"Missing required {label} file at {path}. "
            f"Create it (copy {example_path}) so the server can start."
        )
        logging.critical(message)
        raise RuntimeError(message)
    except OSError as err:
        message = f"Unable to read {label} file at {path}: {err}"
        logging.critical(message)
        raise RuntimeError(message) from err

    if not content:
        message = f"{label} file at {path} is empty."
        logging.critical(message)
        raise RuntimeError(message)

    return content


def load_admin_password() -> str:
    global ADMIN_PASSWORD
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD
    ADMIN_PASSWORD = required_file(PASSWORD_FILE, "moderator password")
    return ADMIN_PASSWORD


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _prune_sessions() -> None:
    now = _now_ts()
    expired = [token for token, exp in active_sessions.items() if exp <= now]
    for token in expired:
        active_sessions.pop(token, None)


def _issue_session() -> Tuple[str, float]:
    token = secrets.token_urlsafe(32)
    expires_at = _now_ts() + SESSION_TTL_SECONDS
    active_sessions[token] = expires_at
    return token, expires_at


def _validate_session(token: Optional[str]) -> bool:
    if not token or not isinstance(token, str):
        return False
    _prune_sessions()
    expires_at = active_sessions.get(token)
    if expires_at is None or expires_at <= _now_ts():
        active_sessions.pop(token, None)
        return False
    return True


def _extract_session_token(request: Request) -> Optional[str]:
    header_token = request.headers.get(SESSION_HEADER)
    cookie_token = request.cookies.get(SESSION_COOKIE)
    return header_token or cookie_token


class ConnectionManager:
    def __init__(self) -> None:
        self.active: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active.add(websocket)
        if recent_messages:
            await websocket.send_json({"type": "history", "messages": list(recent_messages)})
        await websocket.send_json({"type": "highlightQueue", "items": queue_snapshot()})

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.active.discard(websocket)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        stale: List[WebSocket] = []
        async with self._lock:
            for ws in self.active:
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.active.discard(ws)


manager = ConnectionManager()


@app.get("/", response_class=FileResponse)
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if PREFIX:
    # Mirror the root route under the path prefix so proxied deployments work.
    app.add_api_route(f"{PREFIX}/", root, methods=["GET"], include_in_schema=False)


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_client_event(event)
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)
        raise


if PREFIX:
    # Expose the websocket under the prefixed path for reverse proxies.
    app.router.add_websocket_route(f"{PREFIX}/ws/chat", websocket_endpoint)


def build_message_payload(user: str, text: str, emotes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "type": "chat",
        "id": str(uuid.uuid4()),
        "user": user,
        "text": text,
        "emotes": emotes or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def remember_message(payload: Dict[str, Any]) -> None:
    evicted: Optional[Dict[str, Any]] = None
    if recent_messages.maxlen and len(recent_messages) == recent_messages.maxlen:
        evicted = recent_messages[0]
    recent_messages.append(payload)
    if evicted:
        evicted_id = evicted.get("id")
        if evicted_id:
            # Keep lookup entries for messages that are still displayed in the highlight queue
            # so re-highlighting an old slot keeps working even after chat eviction.
            if not any(entry.get("id") == evicted_id for entry in highlight_queue):
                message_lookup.pop(evicted_id, None)
    message_lookup[payload["id"]] = payload


def _ensure_twitch_queue() -> asyncio.Queue[str]:
    global twitch_send_queue
    if twitch_send_queue is None:
        twitch_send_queue = asyncio.Queue(maxsize=100)
    return twitch_send_queue


def _prepare_irc_message(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    return cleaned[:IRC_MESSAGE_LIMIT]


async def enqueue_twitch_message(text: str) -> None:
    if USE_FAKE_STREAM:
        logging.info("Twitch send skipped; fake stream enabled")
        return

    queue = _ensure_twitch_queue()
    prepared = _prepare_irc_message(text)
    if not prepared:
        return

    try:
        queue.put_nowait(prepared)
    except asyncio.QueueFull:
        logging.warning("Twitch send queue full; dropping message")


async def _drain_twitch_queue(writer: asyncio.StreamWriter, channel: str) -> None:
    queue = _ensure_twitch_queue()
    while True:
        text = await queue.get()
        try:
            command = f"PRIVMSG #{channel} :{text}\r\n"
            writer.write(command.encode("utf-8"))
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logging.error("Failed to send Twitch message: %s", err)
            break


def queue_snapshot() -> List[Dict[str, Any]]:
    snapshot: List[Dict[str, Any]] = []
    for item in highlight_queue:
        obj = dict(item)
        obj["highlighted"] = bool(item.get("highlighted", False))
        obj["pinned"] = bool(item.get("pinned", False))
        snapshot.append(obj)
    return snapshot


def _split_queue() -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    pinned: Optional[Dict[str, Any]] = None
    unpinned: List[Dict[str, Any]] = []
    for entry in highlight_queue:
        if entry.get("pinned") and pinned is None:
            pinned = entry
        else:
            unpinned.append(entry)
    return pinned, unpinned


def _rebuild_queue(pinned: Optional[Dict[str, Any]], unpinned: List[Dict[str, Any]]) -> None:
    highlight_queue.clear()
    if pinned:
        pinned = dict(pinned)
        pinned["pinned"] = True
        highlight_queue.append(pinned)

    capacity = max(0, TOTAL_QUEUE_SLOTS - len(highlight_queue))
    highlight_queue.extend(unpinned[:capacity])


def _ensure_single_highlight(target_id: str) -> None:
    for entry in highlight_queue:
        entry["highlighted"] = entry.get("id") == target_id


def push_highlight_queue(message: Dict[str, Any], *, move_to_front: bool = False) -> None:
    message_id = message.get("id")
    if not message_id:
        return

    pinned, unpinned = _split_queue()

    # Update or create the unpinned entry for this message.
    existing_idx = next((i for i, entry in enumerate(unpinned) if entry.get("id") == message_id), None)
    if existing_idx is not None:
        entry = unpinned.pop(existing_idx)
        entry.update(message)
    else:
        entry = dict(message)

    entry["highlighted"] = True
    entry["pinned"] = False

    if move_to_front or existing_idx is None:
        unpinned.insert(0, entry)
    else:
        unpinned.insert(existing_idx, entry)

    _rebuild_queue(pinned, unpinned)
    _ensure_single_highlight(message_id)


def pin_message(message_id: str) -> None:
    if not message_id:
        return

    message = message_lookup.get(message_id) or next(
        (entry for entry in highlight_queue if entry.get("id") == message_id), None
    )
    if not message:
        return

    _, unpinned = _split_queue()
    unpinned = [dict(entry) for entry in unpinned if entry.get("id") != message_id]

    pinned_entry = dict(message)
    pinned_entry["pinned"] = True
    pinned_entry["highlighted"] = True

    _rebuild_queue(pinned_entry, unpinned)
    _ensure_single_highlight(message_id)


def unpin_message(message_id: str) -> None:
    if not message_id:
        return

    pinned, unpinned = _split_queue()
    if not pinned or pinned.get("id") != message_id:
        return

    demoted = dict(pinned)
    demoted["pinned"] = False
    unpinned.insert(0, demoted)

    _rebuild_queue(None, unpinned)
    _ensure_single_highlight(message_id)


def remove_highlight(message_id: Optional[str]) -> None:
    if not message_id:
        for entry in highlight_queue:
            entry["highlighted"] = False
        return

    for entry in highlight_queue:
        if entry.get("id") == message_id:
            entry["highlighted"] = False
            break


async def broadcast_message(payload: Dict[str, Any]) -> None:
    remember_message(payload)
    await manager.broadcast(payload)


async def broadcast_queue() -> None:
    await manager.broadcast({"type": "highlightQueue", "items": queue_snapshot()})


async def handle_client_event(event: Any) -> None:
    """Process messages originating from connected browsers."""
    if not isinstance(event, dict):
        return

    event_type = event.get("type")
    if event_type in PROTECTED_EVENTS and not _validate_session(event.get("session")):
        return
    if event_type == "highlight":
        message_id = event.get("id")
        if not isinstance(message_id, str):
            return
        pinned, unpinned = _split_queue()
        pinned_id = pinned.get("id") if pinned else None
        if pinned_id and message_id != pinned_id:
            source = event.get("source") or "queue"
            move_to_front = source == "chat"
            message = message_lookup.get(message_id) or next(
                (entry for entry in highlight_queue if entry.get("id") == message_id), None
            )
            if not message:
                return

            # Insert/update unpinned entries without changing the pinned highlight.
            existing_idx = next((i for i, entry in enumerate(unpinned) if entry.get("id") == message_id), None)
            if existing_idx is not None:
                entry = unpinned.pop(existing_idx)
                entry.update(message)
            else:
                entry = dict(message)

            entry["highlighted"] = False
            entry["pinned"] = False

            if move_to_front or existing_idx is None:
                unpinned.insert(0, entry)
            else:
                unpinned.insert(existing_idx, entry)

            _rebuild_queue(pinned, unpinned)
            _ensure_single_highlight(pinned_id)
            await broadcast_queue()
            return
        if pinned_id == message_id:
            pinned["highlighted"] = True
            _rebuild_queue(pinned, unpinned)
            await broadcast_queue()
            return
        source = event.get("source") or "queue"
        move_to_front = source == "chat"
        # Fallback to the existing queue entry if it survived history eviction.
        message = message_lookup.get(message_id) or next(
            (entry for entry in highlight_queue if entry.get("id") == message_id), None
        )
        if not message:
            return
        push_highlight_queue(message, move_to_front=move_to_front)
        await broadcast_queue()
    elif event_type == "clearHighlight":
        message_id = event.get("id")
        remove_highlight(message_id)
        await broadcast_queue()
    elif event_type == "pin":
        message_id = event.get("id")
        if not isinstance(message_id, str):
            return
        pin_message(message_id)
        await broadcast_queue()
    elif event_type == "unpin":
        message_id = event.get("id")
        if not isinstance(message_id, str):
            return
        unpin_message(message_id)
        await broadcast_queue()
    elif event_type == "rumble":
        message_id = event.get("id")
        if not isinstance(message_id, str):
            return
        await manager.broadcast({"type": "rumble", "id": message_id})


async def twitch_chat_loop(username: str, token: str, channel: str) -> None:
    _ensure_twitch_queue()
    channel = channel.lstrip("#").lower()
    token = token if token.startswith("oauth:") else f"oauth:{token}"

    while True:
        reader: Optional[asyncio.StreamReader] = None
        writer: Optional[asyncio.StreamWriter] = None
        send_task: Optional[asyncio.Task] = None
        try:
            logging.info("Connecting to Twitch IRC as %s", username)
            ssl_context = ssl.create_default_context()
            reader, writer = await asyncio.open_connection(IRC_HOST, IRC_PORT, ssl=ssl_context)

            capability_commands = ["CAP REQ :twitch.tv/tags twitch.tv/commands\r\n"]
            auth_commands = [
                f"PASS {token}\r\n",
                f"NICK {username}\r\n",
                f"JOIN #{channel}\r\n",
            ]
            for line in capability_commands + auth_commands:
                writer.write(line.encode("utf-8"))
            await writer.drain()

            send_task = asyncio.create_task(_drain_twitch_queue(writer, channel))

            while True:
                raw_bytes = await reader.readline()
                if not raw_bytes:
                    raise ConnectionError("Lost connection to Twitch IRC")
                raw = raw_bytes.decode("utf-8", errors="ignore").strip()

                if raw.startswith("PING"):
                    writer.write(raw.replace("PING", "PONG").encode("utf-8") + b"\r\n")
                    await writer.drain()
                    continue

                if "PRIVMSG" not in raw:
                    if "NOTICE" in raw and "Login authentication failed" in raw:
                        raise RuntimeError("Twitch authentication failed. Check your token.")
                    continue

                payload = parse_privmsg(raw)
                if payload:
                    await broadcast_message(payload)
        except asyncio.CancelledError:
            if send_task:
                send_task.cancel()
                with suppress(asyncio.CancelledError):
                    await send_task
            if writer:
                writer.close()
                await writer.wait_closed()
            raise
        except Exception as err:
            logging.error("Twitch loop error: %s", err)
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        finally:
            if send_task:
                send_task.cancel()
                with suppress(asyncio.CancelledError):
                    await send_task
            if writer:
                writer.close()
                await writer.wait_closed()
    

def _unescape_tag_value(value: str) -> str:
    # Twitch IRC tag escaping rules
    return (
        value.replace("\\s", " ")
        .replace("\\:", ";")
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )


def _parse_tags(raw_tags: str) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    if not raw_tags:
        return tags
    cleaned = raw_tags.lstrip("@")
    for part in cleaned.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", maxsplit=1)
        tags[key] = _unescape_tag_value(value)
    return tags


def _parse_emote_tag(emote_spec: str, message: str) -> List[Dict[str, Any]]:
    if not emote_spec:
        return []

    emotes: List[Dict[str, Any]] = []
    for entry in emote_spec.split("/"):
        if not entry:
            continue
        emote_id, _, positions_raw = entry.partition(":")
        if not emote_id or not positions_raw:
            continue

        positions: List[Tuple[int, int]] = []
        for chunk in positions_raw.split(","):
            start_str, dash, end_str = chunk.partition("-")
            if not dash:
                continue
            try:
                start = int(start_str)
                end = int(end_str)
            except ValueError:
                continue
            if start < 0 or end < start or end >= len(message):
                continue
            positions.append((start, end))

        if positions:
            sample_start, sample_end = positions[0]
            code = message[sample_start : sample_end + 1]
            emotes.append({"id": emote_id, "positions": positions, "code": code})

    return emotes


def parse_privmsg(raw: str) -> Optional[Dict[str, Any]]:
    try:
        tags_raw = ""
        remainder = raw
        if raw.startswith("@"):
            tags_raw, _, remainder = raw.partition(" ")

        prefix, _, trailing = remainder.partition(" :")
        if not trailing:
            return None

        user_section = prefix.split("!", maxsplit=1)[0]
        user = user_section[1:] if user_section.startswith(":") else user_section

        tags = _parse_tags(tags_raw)
        display_name = tags.get("display-name") or user
        text = _unescape_tag_value(trailing)
        emotes = _parse_emote_tag(tags.get("emotes", ""), text)

        return build_message_payload(user=display_name, text=text, emotes=emotes)
    except Exception as err:
        logging.debug("Failed to parse message '%s': %s", raw, err)
        return None


async def fake_chat_loop() -> None:
    import random

    nicknames = ["Orbit", "Nova", "Pixel", "Echo", "Glyph", "Vivid"]
    snippets = [
        "That strat was clean!",
        "Camera 2, we need a close-up!",
        "Reminder: hydrate everyone.",
        "Drop the sponsor tag next.",
        "Mods, can we clip that moment?",
        "Crowd volume is unreal tonight!",
    ]

    while True:
        payload = build_message_payload(
            user=random.choice(nicknames),
            text=random.choice(snippets),
        )
        await broadcast_message(payload)
        await asyncio.sleep(random.uniform(1.5, 3.5))


@app.on_event("startup")
async def startup_event() -> None:
    global CHANNEL_NAME
    load_admin_password()
    if USE_FAKE_STREAM:
        app.state.chat_task = asyncio.create_task(fake_chat_loop())
        mode = "fake"
        CHANNEL_NAME = FAKE_CHANNEL_NAME
    else:
        username = required_file(USERNAME_FILE, "Twitch username")
        token = required_file(TOKEN_FILE, "Twitch OAuth token")
        channel = required_file(CHANNEL_FILE, "Twitch channel")
        CHANNEL_NAME = channel.lstrip("#")
        _ensure_twitch_queue()
        app.state.chat_task = asyncio.create_task(twitch_chat_loop(username, token, channel))
        mode = "twitch"
    app.state.channel_name = CHANNEL_NAME
    logging.info("Chat stream started in %s mode", mode)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    task: asyncio.Task = app.state.chat_task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@app.get("/api/channel")
async def channel_info() -> Dict[str, str]:
    channel = getattr(app.state, "channel_name", None)
    if not channel:
        channel = CHANNEL_NAME
    if not channel:
        try:
            channel = required_file(CHANNEL_FILE, "Twitch channel")
        except RuntimeError:
            channel = "unknown"

    clean_channel = channel.lstrip("#")
    app.state.channel_name = clean_channel

    try:
        bot_username = required_file(USERNAME_FILE, "Twitch username").strip() or "unknown"
    except RuntimeError:
        bot_username = "unknown"

    return {"channel": clean_channel, "bot_username": bot_username}


@app.post("/api/session")
async def create_session(response: Response, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    password = str(body.get("password") or "") if isinstance(body, dict) else ""
    expected = load_admin_password()
    if not hmac.compare_digest(password, expected):
        raise HTTPException(status_code=403, detail="Invalid password")

    token, expires_at = _issue_session()
    expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    max_age = int(timedelta(seconds=SESSION_TTL_SECONDS).total_seconds())
    secure_cookie = SECURE_COOKIES
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path=PREFIX or "/",
    )
    return {"token": token, "expires": expires_dt.isoformat()}


@app.post("/api/custom-message")
async def custom_message(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    token = _extract_session_token(request)
    if not _validate_session(token):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    raw_user = (body.get("user") or "Guest").strip()
    raw_text = (body.get("text") or "").strip()

    if not raw_text:
        raise HTTPException(status_code=400, detail="text is required")

    user = raw_user[:48] if raw_user else "Guest"
    text = raw_text[:500]

    spotlight_raw = body.get("spotlight", True)
    spotlight = bool(spotlight_raw)
    twitch_raw = body.get("twitch", False)
    twitch = bool(twitch_raw)

    payload = build_message_payload(user=user, text=text)
    payload["custom"] = True

    await broadcast_message(payload)
    if spotlight:
        push_highlight_queue(payload, move_to_front=True)
        await broadcast_queue()
    if twitch:
        await enqueue_twitch_message(text)
    return {"status": "ok", "id": payload["id"]}


if PREFIX:
    # Alternate path for deployments served from a subdirectory.
    app.add_api_route(f"{PREFIX}/api/channel", channel_info, methods=["GET"])
    app.add_api_route(f"{PREFIX}/api/session", create_session, methods=["POST"])
    app.add_api_route(f"{PREFIX}/api/custom-message", custom_message, methods=["POST"])
