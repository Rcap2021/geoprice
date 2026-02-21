"""
GeoPrice Travel - Backend API
Chat interface + Price search engine
"""

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import date, datetime, timedelta
from uuid import uuid4, UUID
import asyncio
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from chat_service import ChatService, TravelIntent as ChatTravelIntent
from price_engine import PriceEngine

app = FastAPI(title="GeoPrice Travel API", version="1.0.0")

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage (replace with PostgreSQL in production)
conversations: Dict[str, dict] = {}
searches: Dict[str, dict] = {}
proxy_tokens: Dict[str, dict] = {}  # token → { geo_code, proxy_addr, expires_at }

# Neko WebRTC remote browser sessions (on-demand, no pool)
# HTTP signaling ports: 17080-17099  (20 concurrent sessions max)
# WebRTC EPR per slot: 59000+slot*100 … 59099+slot*100  (100 ports each, firewall open 59000-59100)
NEKO_HTTP_PORT_MIN = 17080
NEKO_HTTP_PORT_MAX = 17099
NEKO_EPR_BASE = 59000
NEKO_EPR_PER_SLOT = 100
neko_sessions: Dict[str, dict] = {}  # session_id -> {http_port, container_name, ...}

# Services
chat_service = ChatService()
price_engine = PriceEngine()


# ============== Neko Session Helpers ==============

def _pick_neko_port() -> int:
    """Pick a free HTTP port in the neko pool."""
    used = {s["http_port"] for s in neko_sessions.values()}
    for port in range(NEKO_HTTP_PORT_MIN, NEKO_HTTP_PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("All neko session ports are in use")


# ============== CONNECT Relay Proxy ==============

async def _proxy_read_request(reader: asyncio.StreamReader):
    """Read one HTTP request line + headers. Returns (method, target, headers_dict)."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
    except asyncio.TimeoutError:
        return None, None, {}
    request_line = request_line.decode("utf-8", errors="replace").strip()
    if not request_line:
        return None, None, {}
    parts = request_line.split(" ", 2)
    if len(parts) < 2:
        return None, None, {}
    method, target = parts[0].upper(), parts[1]
    headers: dict = {}
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError:
            break
        decoded = line.decode("utf-8", errors="replace").strip()
        if not decoded:
            break
        if ":" in decoded:
            k, _, v = decoded.partition(":")
            headers[k.strip().lower()] = v.strip()
    return method, target, headers


async def _proxy_pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
    """Pipe bytes from src to dst until EOF or error."""
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except Exception:
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def _proxy_handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """
    Handle one browser connection to the geo proxy on port 8766.
    Supports both CONNECT (HTTPS tunneling) and plain HTTP GET/POST.
    The extension's onAuthRequired handler provides the token as Basic auth username.
    """
    import base64

    _407 = (
        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
        b"Proxy-Authenticate: Basic realm=\"GeoPrice\"\r\n"
        b"Content-Length: 0\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"\r\n"
    )

    geo_writer = None
    try:
        method, target, headers = await _proxy_read_request(reader)
        if not method:
            return

        proxy_auth = headers.get("proxy-authorization", "")

        if not proxy_auth:
            # Challenge — Chrome fires onAuthRequired in the extension background.js
            writer.write(_407)
            await writer.drain()
            # Read the retry (Chrome resends the same request with credentials)
            try:
                method, target, headers = await asyncio.wait_for(
                    _proxy_read_request(reader), timeout=15
                )
                proxy_auth = headers.get("proxy-authorization", "")
            except asyncio.TimeoutError:
                return

        if not proxy_auth or not proxy_auth.lower().startswith("basic "):
            writer.write(_407)
            await writer.drain()
            return

        # Decode Basic auth → token:x
        try:
            creds = base64.b64decode(proxy_auth[6:]).decode("utf-8", errors="replace")
            token = creds.split(":")[0]
        except Exception:
            writer.write(_407)
            await writer.drain()
            return

        # Validate token
        entry = proxy_tokens.get(token)
        if not entry or datetime.utcnow() > entry["expires_at"]:
            writer.write(_407)
            await writer.drain()
            return

        proxy_addr = entry["proxy_addr"]   # "host:port"
        proxy_host, proxy_port_str = proxy_addr.rsplit(":", 1)
        proxy_port = int(proxy_port_str)

        # Connect to the upstream geo proxy
        try:
            geo_reader, geo_writer = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port), timeout=15
            )
        except Exception as e:
            print(f"[Relay] Cannot reach geo proxy {proxy_addr}: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        if method == "CONNECT":
            # HTTPS tunnel: forward CONNECT to geo proxy
            geo_writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            await geo_writer.drain()

            # Read geo proxy's CONNECT response
            try:
                geo_status = await asyncio.wait_for(geo_reader.readline(), timeout=15)
                while True:
                    line = await asyncio.wait_for(geo_reader.readline(), timeout=5)
                    if line in (b"\r\n", b"\n", b""):
                        break
            except asyncio.TimeoutError:
                writer.write(b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return

            if b"200" not in geo_status:
                print(f"[Relay] Geo proxy refused CONNECT to {target}: {geo_status.strip()}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return

            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

        else:
            # Plain HTTP (GET/POST/etc): forward full request to geo proxy
            req = f"{method} {target} HTTP/1.1\r\n"
            # Strip proxy-specific headers before forwarding
            fwd_headers = {k: v for k, v in headers.items()
                           if k.lower() not in ("proxy-authorization", "proxy-connection")}
            fwd_headers.setdefault("connection", "close")
            req += "".join(f"{k}: {v}\r\n" for k, v in fwd_headers.items())
            req += "\r\n"
            geo_writer.write(req.encode())
            await geo_writer.drain()

        # Bidirectional pipe until either side closes
        await asyncio.gather(
            _proxy_pipe(reader, geo_writer),
            _proxy_pipe(geo_reader, writer),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"[Relay] Unexpected error: {e}")
        try:
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
        if geo_writer:
            try:
                geo_writer.close()
            except Exception:
                pass


async def _start_connect_proxy():
    RELAY_PORT = int(os.getenv("PROXY_RELAY_PORT", "8766"))
    server = await asyncio.start_server(_proxy_handle_client, "0.0.0.0", RELAY_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"[Relay] CONNECT proxy listening on {addrs}")
    asyncio.create_task(server.serve_forever())


@app.on_event("startup")
async def on_startup():
    """Clean up leftover neko containers from previous runs and start relay proxy."""
    try:
        ps = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=geoprice-neko-"],
            capture_output=True, text=True, timeout=10
        )
        for cid in ps.stdout.split():
            subprocess.run(["docker", "stop", cid], capture_output=True, timeout=10)
    except Exception:
        pass
    asyncio.create_task(_start_connect_proxy())


# ============== Models ==============

TravelIntent = ChatTravelIntent


class ChatMessage(BaseModel):
    role: str  # user or assistant
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Conversation(BaseModel):
    id: str
    messages: List[ChatMessage] = []
    intent: TravelIntent = Field(default_factory=TravelIntent)
    status: str = "active"  # active, searching, completed
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    message: str
    baseline_geo: str = "US"


class SearchTriggerRequest(BaseModel):
    intent: TravelIntent
    baseline_geo: str = "US"


class BrowseRequest(BaseModel):
    url: str
    country: str  # e.g. "MY", "IN", "BR"


class ProxyTokenRequest(BaseModel):
    geo_country: str


class ChatResponse(BaseModel):
    conversation_id: str
    response: str
    intent: TravelIntent
    intent_complete: bool
    search_triggered: bool
    search_id: Optional[str] = None


class HotelDeal(BaseModel):
    hotel_name: str
    hotel_id: str
    stars: Optional[int] = None
    location: str
    geo_country: str
    geo_country_name: Optional[str] = None
    geo_price: float
    geo_currency: str
    usd_price: float
    baseline_usd_price: Optional[float] = None
    baseline_geo: Optional[str] = None
    baseline_geo_name: Optional[str] = None
    savings_percent: Optional[float] = None
    savings_usd: Optional[float] = None
    room_type: Optional[str] = None
    includes_breakfast: bool = False
    free_cancellation: bool = False
    review_score: Optional[float] = None
    booking_url: str
    baseline_url: Optional[str] = None


class SearchResult(BaseModel):
    search_id: str
    status: str  # pending, in_progress, completed, failed
    intent: TravelIntent
    progress: int = 0  # 0-100
    geos_completed: List[str] = []
    geos_total: int = 0
    best_deals: List[HotelDeal] = []
    all_results: Dict[str, List[HotelDeal]] = {}
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


# ============== Endpoints ==============

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the frontend"""
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content="<h1>GeoPrice API running. Frontend not found.</h1>")


@app.get("/extension/geoprice-extension.zip")
async def download_extension():
    """Serve the Chrome extension as a downloadable zip."""
    zip_path = Path(__file__).parent.parent / "extension" / "geoprice-extension.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Extension zip not found")
    return FileResponse(zip_path, media_type="application/zip", filename="geoprice-extension.zip",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/health")
async def api_root():
    return {"status": "ok", "service": "GeoPrice Travel API"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Main chat endpoint. Handles conversation and intent extraction.
    """
    baseline_geo = request.baseline_geo

    # Get or create conversation
    if request.conversation_id and request.conversation_id in conversations:
        conv_data = conversations[request.conversation_id]
        conv = Conversation(**conv_data)
    else:
        conv = Conversation(id=str(uuid4()))

    # Add user message
    conv.messages.append(ChatMessage(role="user", content=request.message))

    # Process with LLM
    response_text, updated_intent, search_triggered = await chat_service.process_message(
        messages=conv.messages,
        current_intent=conv.intent,
        user_message=request.message
    )

    # Update conversation
    conv.intent = updated_intent
    conv.messages.append(ChatMessage(role="assistant", content=response_text))

    # Save conversation
    conversations[conv.id] = conv.model_dump(mode='json')

    # If search triggered, start the price search
    search_id = None
    if search_triggered and conv.intent.is_complete():
        conv.status = "searching"
        search_id = str(uuid4())

        # Initialize search record
        searches[search_id] = SearchResult(
            search_id=search_id,
            status="pending",
            intent=conv.intent,
            geos_total=len(price_engine.GEO_COUNTRIES),
            started_at=datetime.utcnow()
        ).model_dump(mode='json')
        searches[search_id]["baseline_geo"] = baseline_geo

        # Start search in background
        asyncio.create_task(run_price_search(search_id, conv.intent, baseline_geo))

    return ChatResponse(
        conversation_id=conv.id,
        response=response_text,
        intent=conv.intent,
        intent_complete=conv.intent.is_complete(),
        search_triggered=search_triggered,
        search_id=search_id
    )


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Get conversation history"""
    if conversation_id not in conversations:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversations[conversation_id]


@app.post("/api/search/trigger")
async def trigger_search(request: SearchTriggerRequest):
    """Manually trigger a search with complete intent"""
    intent = request.intent
    baseline_geo = request.baseline_geo

    if not intent.is_complete():
        raise HTTPException(status_code=400, detail="Intent is incomplete")

    search_id = str(uuid4())
    searches[search_id] = SearchResult(
        search_id=search_id,
        status="pending",
        intent=intent,
        geos_total=len(price_engine.GEO_COUNTRIES),
        started_at=datetime.utcnow()
    ).model_dump(mode='json')
    searches[search_id]["baseline_geo"] = baseline_geo

    asyncio.create_task(run_price_search(search_id, intent, baseline_geo))

    return {"search_id": search_id, "status": "pending"}


@app.get("/api/search/{search_id}", response_model=SearchResult)
async def get_search_results(search_id: str):
    """Get search status and results"""
    if search_id not in searches:
        raise HTTPException(status_code=404, detail="Search not found")
    return SearchResult(**searches[search_id])


@app.websocket("/api/search/{search_id}/stream")
async def stream_search_results(websocket: WebSocket, search_id: str):
    """
    WebSocket endpoint for streaming search results in real-time
    """
    await websocket.accept()
    
    if search_id not in searches:
        await websocket.send_json({"error": "Search not found"})
        await websocket.close()
        return
    
    try:
        last_progress = -1
        last_geos = 0
        
        while True:
            search = searches.get(search_id)
            if not search:
                break
                
            # Send update if progress changed
            current_progress = search.get("progress", 0)
            current_geos = len(search.get("geos_completed", []))
            
            if current_progress != last_progress or current_geos != last_geos:
                await websocket.send_json({
                    "status": search["status"],
                    "progress": current_progress,
                    "geos_completed": search.get("geos_completed", []),
                    "geos_total": search.get("geos_total", 0),
                    "best_deals": search.get("best_deals", []),
                    "latest_geo": search.get("geos_completed", [])[-1] if search.get("geos_completed") else None
                })
                last_progress = current_progress
                last_geos = current_geos
            
            # Check if complete
            if search["status"] in ["completed", "failed"]:
                await websocket.send_json({
                    "status": search["status"],
                    "progress": 100,
                    "best_deals": search.get("best_deals", []),
                    "completed": True
                })
                break
            
            await asyncio.sleep(0.5)
            
    except WebSocketDisconnect:
        pass


async def run_price_search(search_id: str, intent: TravelIntent, baseline_geo: str = "US"):
    """
    Background task to run price search across all geos
    """
    try:
        searches[search_id]["status"] = "in_progress"

        # Run the search
        async for update in price_engine.search_all_geos(intent):
            # Update search record with progress
            searches[search_id]["progress"] = update["progress"]
            searches[search_id]["geos_completed"] = update["geos_completed"]

            if "geo_results" in update:
                geo = update["geo"]
                searches[search_id]["all_results"][geo] = update["geo_results"]

                # Recalculate best deals vs user's baseline country
                searches[search_id]["best_deals"] = price_engine.calculate_best_deals(
                    searches[search_id]["all_results"],
                    baseline_geo=baseline_geo
                )

        searches[search_id]["status"] = "completed"
        searches[search_id]["completed_at"] = datetime.utcnow().isoformat()

    except Exception as e:
        searches[search_id]["status"] = "failed"
        searches[search_id]["error"] = str(e)


def _build_proxy_url_for_geo(geo_code: str) -> str:
    """Build a full proxy URL for a given country code."""
    from price_engine import PriceEngine
    proxy_format = os.getenv("PROXY_FORMAT", "direct")

    if proxy_format == "direct":
        server = PriceEngine.GEO_PROXIES.get(geo_code)
        return f"http://{server}" if server else ""

    proxy_base_url = os.getenv("PROXY_BASE_URL", "")
    proxy_username = os.getenv("PROXY_USERNAME", "")
    proxy_password = os.getenv("PROXY_PASSWORD", "")

    if not proxy_base_url:
        return ""

    geo_lower = geo_code.lower()

    if proxy_format == "viprox":
        host_part = proxy_base_url.replace("http://", "").replace("https://", "")
        scheme = "https" if proxy_base_url.startswith("https://") else "http"
        geo_username = f"{proxy_username}-rc_{geo_lower}"
        return f"{scheme}://{geo_username}:{proxy_password}@{host_part}"

    return proxy_base_url


# Port pool for per-token no-auth relay servers (8770–8869 = 100 slots)
_TOKEN_PORT_MIN = 8770
_TOKEN_PORT_MAX = 8869
_active_relay_servers: Dict[str, asyncio.AbstractServer] = {}  # token → server


_BOOKING_HOST_RE = re.compile(r'^(www\.)?booking\.com$', re.IGNORECASE)


async def _relay_noauth_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    proxy_host: str,
    proxy_port: int,
    allowed_ip: str,
    restrict_hosts: bool = True,
):
    """
    No-auth CONNECT relay: forwards to a fixed upstream geo proxy.
    Security guards:
      1. Only accepts connections from the IP that created the token.
      2. (optional) Only tunnels to *.booking.com — for the extension relay.
         Neko relay sets restrict_hosts=False so the full browser can load
         all page assets (CDN, fonts, images on bstatic.com, etc.)
    """
    client_ip = writer.get_extra_info("peername", ("?", 0))[0]
    geo_writer = None
    try:
        # Guard 1: IP allowlist — only enforced for neko relay (allowed_ip="127.0.0.1").
        # Extension relay skips this: port-per-token + host allowlist (Guard 2) is sufficient.
        if allowed_ip == "127.0.0.1" and client_ip != allowed_ip:
            print(f"[Relay/noauth] Rejected connection from {client_ip} (expected {allowed_ip})")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        method, target, headers = await _proxy_read_request(reader)
        if method != "CONNECT":
            # Plain HTTP proxy request (e.g. GET http://ip-api.com/json HTTP/1.1)
            # Forward to the geo proxy as-is.
            try:
                geo_reader, geo_writer = await asyncio.wait_for(
                    asyncio.open_connection(proxy_host, proxy_port), timeout=15
                )
            except Exception as e:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            # Reconstruct and forward the request
            req  = f"{method} {target} HTTP/1.1\r\n"
            req += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            req += "\r\n"
            geo_writer.write(req.encode())
            await geo_writer.drain()
            await asyncio.gather(
                _proxy_pipe(reader, geo_writer),
                _proxy_pipe(geo_reader, writer),
                return_exceptions=True,
            )
            return

        # Guard 2 (extension relay only): target must be *.booking.com or ip-api.com
        if restrict_hosts:
            # For CONNECT the target is host:port; for plain HTTP it's http://host/path
            target_host = target.split(":")[0]
            if target_host.startswith("http://") or target_host.startswith("https://"):
                from urllib.parse import urlparse as _urlparse
                target_host = _urlparse(target).hostname or ""
            allowed = (
                _BOOKING_HOST_RE.match(target_host)
                or target_host.endswith(".booking.com")
                or target_host in ("ip-api.com", "www.ip-api.com")
            )
            if not allowed:
                print(f"[Relay/noauth] Blocked request to non-allowed host: {target}")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return

        # Connect to the geo proxy
        try:
            geo_reader, geo_writer = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port), timeout=15
            )
        except Exception as e:
            print(f"[Relay/noauth] Cannot reach geo proxy {proxy_host}:{proxy_port}: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        # Forward CONNECT to the geo proxy
        geo_writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await geo_writer.drain()

        # Read geo proxy's response
        try:
            geo_status = await asyncio.wait_for(geo_reader.readline(), timeout=15)
            while True:
                line = await asyncio.wait_for(geo_reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
        except asyncio.TimeoutError:
            writer.write(b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        if b"200" not in geo_status:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        await asyncio.gather(
            _proxy_pipe(reader, geo_writer),
            _proxy_pipe(geo_reader, writer),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"[Relay/noauth] Error: {e}")
    finally:
        try:
            writer.close()
        except Exception:
            pass
        if geo_writer:
            try:
                geo_writer.close()
            except Exception:
                pass


def _pick_relay_port() -> int:
    """Pick a free port in the per-token pool (8770–8869).

    Checks both our in-memory tracking dicts AND does an actual OS-level
    socket bind test so stale relay servers from expired sessions don't
    block the port from being reused.
    """
    import socket as _socket
    used = {v.get("relay_port") for v in proxy_tokens.values() if v.get("relay_port")}
    used |= {s.get("relay_port") for s in neko_sessions.values() if s.get("relay_port")}
    for port in range(_TOKEN_PORT_MIN, _TOKEN_PORT_MAX + 1):
        if port in used:
            continue
        # Verify at OS level — stale server processes may still hold the socket
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("All relay ports in use")


@app.post("/api/proxy-token")
async def create_proxy_token(request: ProxyTokenRequest, http_request: Request):
    """
    Issue a short-lived token that the Chrome extension exchanges for a PAC script.
    Starts a dedicated no-auth relay on a per-token port — Chrome never sees a 407
    challenge, so no native credential dialog appears.
    The relay only accepts connections from the same IP that called this endpoint.
    """
    geo = request.geo_country.upper()
    proxy_addr = PriceEngine.GEO_PROXIES.get(geo)
    if not geo or not proxy_addr:
        raise HTTPException(status_code=400, detail="Unsupported geo or no proxy available")

    # Capture the real client IP (X-Forwarded-For set by nginx, fallback to direct)
    forwarded = http_request.headers.get("X-Forwarded-For", "")
    allowed_ip = forwarded.split(",")[0].strip() if forwarded else (http_request.client.host if http_request.client else "")
    if not allowed_ip:
        raise HTTPException(status_code=400, detail="Cannot determine client IP")

    token = secrets.token_urlsafe(32)
    proxy_tokens[token] = {
        "geo_code": geo,
        "proxy_addr": proxy_addr,
        "allowed_ip": allowed_ip,
        "expires_at": datetime.utcnow() + timedelta(minutes=30),
    }
    print(f"[Relay] Token issued for {geo} → {proxy_addr} (client={allowed_ip})")

    # Auto-expire token
    async def _expire():
        await asyncio.sleep(1800)
        proxy_tokens.pop(token, None)

    asyncio.create_task(_expire())

    return {"token": token, "ttl": 1800}


@app.get("/api/pac/{token}")
async def get_pac_script(token: str):
    """Return a PAC script routing booking.com through the server relay proxy (token-gated)."""
    entry = proxy_tokens.get(token)
    if not entry or datetime.utcnow() > entry["expires_at"]:
        raise HTTPException(status_code=404, detail="Invalid or expired token")
    relay_host = os.getenv("PROXY_RELAY_HOST", "hotels.chatleg.ai")
    # All extension proxy traffic goes through port 8766 (authenticated CONNECT+HTTP proxy).
    # Chrome's onAuthRequired handler in background.js supplies the token as Basic auth.
    pac = (
        f'function FindProxyForURL(url, host) {{\n'
        f'  if (shExpMatch(host, "*.booking.com") || host === "booking.com"\n'
        f'      || host === "ip-api.com" || host === "www.ip-api.com") {{\n'
        f'    return "PROXY {relay_host}:8766";\n'
        f'  }}\n'
        f'  return "DIRECT";\n'
        f'}}'
    )
    geo_code = entry["geo_code"]
    geo_info = PriceEngine.GEO_COUNTRIES.get(geo_code, {})
    geo_name = geo_info.get("name", geo_code)
    return {"pac": pac, "geo": geo_code, "geo_name": geo_name}


from fastapi.responses import Response as _Response

@app.get("/api/pac-js/{token}")
async def get_pac_js(token: str):
    """Return raw PAC JavaScript (not JSON) — used by pacScript.url in the extension."""
    entry = proxy_tokens.get(token)
    if not entry or datetime.utcnow() > entry["expires_at"]:
        raise HTTPException(status_code=404, detail="Invalid or expired token")
    relay_host = os.getenv("PROXY_RELAY_HOST", "hotels.chatleg.ai")
    pac = (
        f'function FindProxyForURL(url, host) {{\n'
        f'  if (shExpMatch(host, "*.booking.com") || host === "booking.com" ||\n'
        f'      host === "ip-api.com" || host === "www.ip-api.com") {{\n'
        f'    return "PROXY {relay_host}:8766";\n'
        f'  }}\n'
        f'  return "DIRECT";\n'
        f'}}\n'
    )
    return _Response(content=pac, media_type="application/x-ns-proxy-autoconfig")


@app.post("/api/browse")
async def browse(request: BrowseRequest):
    """
    Spin up a Neko WebRTC remote browser session geo-targeted to the requested country.
    Uses our existing per-token relay proxy so the neko container (--network host) can
    reach the geo proxy without auth dialogs.
    Returns the session URL, a one-time password, and session_id.
    """
    from price_engine import PriceEngine

    geo = request.country.upper()
    geo_info = PriceEngine.GEO_COUNTRIES.get(geo, {})

    # ── 1. Pick neko HTTP port ────────────────────────────────────────────────
    try:
        neko_port = _pick_neko_port()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="All browser sessions are busy. Try again shortly.")

    slot_idx = neko_port - NEKO_HTTP_PORT_MIN
    epr_min = NEKO_EPR_BASE + slot_idx * NEKO_EPR_PER_SLOT
    epr_max = epr_min + NEKO_EPR_PER_SLOT - 1

    # ── 2. Start an internal relay for the geo proxy (--network host means
    #      containers access host via 127.0.0.1) ──────────────────────────────
    relay_port = None
    relay_srv = None
    proxy_addr = PriceEngine.GEO_PROXIES.get(geo)

    if proxy_addr:
        try:
            relay_port = _pick_relay_port()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="No relay ports available, try again shortly")

        proxy_host, proxy_port_str = proxy_addr.rsplit(":", 1)
        proxy_port_int = int(proxy_port_str)

        relay_srv = await asyncio.start_server(
            lambda r, w: _relay_noauth_client(r, w, proxy_host, proxy_port_int, "127.0.0.1", restrict_hosts=False),
            "0.0.0.0", relay_port,
        )
        asyncio.create_task(relay_srv.serve_forever())
        print(f"[Neko] Internal relay port {relay_port} → {proxy_addr} (geo={geo})")

    # ── 3. Append geo currency + language to Booking.com URL ─────────────────
    url = request.url
    if "booking.com" in url:
        sep = "&" if "?" in url else "?"
        if "selected_currency=" not in url:
            geo_currency = geo_info.get("currency", "USD")
            url += f"{sep}selected_currency={geo_currency}"
            sep = "&"
        if "lang=" not in url:
            url += f"{sep}lang=en-us"

    # ── 4. Build env vars for our chromium-wrapper.sh ────────────────────────
    # NEKO_PROXY_FLAG: single --proxy-server flag (empty string = no proxy)
    # NEKO_START_URL:  URL to open (passed as last arg to chromium)
    proxy_flag = f"--proxy-server=http://127.0.0.1:{relay_port}" if relay_port else ""

    # ── 5. Launch neko container ──────────────────────────────────────────────
    session_id = secrets.token_hex(8)
    container_name = f"geoprice-neko-{session_id}"
    neko_password = secrets.token_urlsafe(6)
    public_host = os.getenv("SERVER_PUBLIC_IP", "hotels.chatleg.ai")
    # NEKO_NAT1TO1 must be a raw IP — resolve hostname if needed
    import socket as _sock
    try:
        public_ip = _sock.gethostbyname(public_host)
    except Exception:
        public_ip = public_host

    cmd = [
        "docker", "run", "-d", "--rm",
        "--network", "host",
        "--name", container_name,
        "--memory=1g", "--cpus=1.0",
        "--shm-size=2g",              # Chromium needs shared memory
        "-e", f"NEKO_BIND=:{neko_port}",
        "-e", f"NEKO_EPR={epr_min}:{epr_max}",
        "-e", f"NEKO_NAT1TO1={public_ip}",  # must be IP, not hostname
        "-e", "NEKO_ICELITE=1",
        "-e", f"NEKO_PASSWORD={neko_password}",
        "-e", "NEKO_PASSWORD_ADMIN=admin",
        "-e", "NEKO_SCREEN=1280x720@30",
        "-e", f"NEKO_PROXY_FLAG={proxy_flag}",
        "-e", f"NEKO_START_URL={url}",
        os.getenv("NEKO_IMAGE", "geoprice-neko"),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Clean up relay if container failed to start
            if relay_srv:
                relay_srv.close()
            raise HTTPException(status_code=500,
                detail=f"Failed to start browser session: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        if relay_srv:
            relay_srv.close()
        raise HTTPException(status_code=504, detail="Timeout launching browser session")

    neko_sessions[session_id] = {
        "http_port": neko_port,
        "container_name": container_name,
        "country": geo,
        "password": neko_password,
        "relay_port": relay_port,
        "relay_srv": relay_srv,
        "started_at": datetime.utcnow().isoformat(),
    }

    # Schedule cleanup after 30 minutes
    asyncio.create_task(_cleanup_neko_session(session_id, delay=1800))

    # ── Wait for neko HTTP port to be listening ───────────────────────────────
    # docker run returns immediately but neko takes ~3-8 s to bind its port.
    # If we return the URL before neko is ready the browser gets ERR_CONNECTION_REFUSED.
    neko_ready = False
    for _ in range(30):  # up to 15 seconds (30 × 0.5 s)
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", neko_port), timeout=1
            )
            w.close()
            await w.wait_closed()
            neko_ready = True
            break
        except Exception:
            await asyncio.sleep(0.5)

    if not neko_ready:
        print(f"[Neko] Warning: port {neko_port} not ready after 15 s — returning URL anyway")

    # Give neko's HTTP server an extra moment to finish initialising
    await asyncio.sleep(0.5)

    # neko v2 auto-connects when BOTH ?pwd= AND ?usr= are in the URL.
    # No login form shown — goes directly to the remote browser.
    session_url = f"http://{public_host}:{neko_port}/?pwd={neko_password}&usr=Viewer"
    ws_url = f"ws://{public_host}:{neko_port}/ws"
    return {
        "session_url": session_url,
        "ws_url": ws_url,
        "session_id": session_id,
        "password": neko_password,
        "expires_in": 1800,
        "type": "neko_webrtc",
    }


async def _cleanup_neko_session(session_id: str, delay: int = 1800):
    """Stop the neko container and relay after delay seconds."""
    await asyncio.sleep(delay)
    session = neko_sessions.pop(session_id, {})
    container = session.get("container_name")
    if container:
        try:
            subprocess.run(["docker", "stop", container], capture_output=True, timeout=15)
        except Exception:
            pass
    relay_srv = session.get("relay_srv")
    if relay_srv:
        try:
            relay_srv.close()
            await relay_srv.wait_closed()
        except Exception:
            pass
    relay_port = session.get("relay_port")
    if relay_port:
        print(f"[Neko] Cleaned up session {session_id}, relay port {relay_port} freed")


# ============== Featured Deals (from deal_scanner) ==============

@app.get("/api/featured-deals")
async def get_featured_deals(limit: int = 20, city: Optional[str] = None):
    """
    Return pre-scanned best deals (all cities or filtered by ?city=Paris).
    Populated by deal_scanner.py running in the background (local or remote).
    Returns empty list if scanner hasn't run yet.
    """
    db_path = os.getenv("DEAL_SCANNER_DB", "/tmp/geoprice_deals.db")
    if not Path(db_path).exists():
        return {"deals": [], "cities": [], "message": "Scanner warming up"}
    try:
        from deal_scanner import get_featured_deals as _get_deals, get_scanner_stats as _get_stats
        deals = _get_deals(db_path, limit=limit, city=city)
        stats = _get_stats(db_path)
        return {"deals": deals, "cities": stats.get("cities", []), "stats": stats}
    except Exception as e:
        return {"deals": [], "cities": [], "error": str(e)}


@app.post("/api/scanner/ingest")
async def scanner_ingest(request: Request):
    """
    Receive deals from a remote scanner instance and store them in the local DB.
    Protected by bearer token (SCANNER_INGEST_TOKEN in .env).
    Payload: { city, check_in, check_out, tier, deals: [...] }
    """
    ingest_token = os.getenv("SCANNER_INGEST_TOKEN", "")
    if not ingest_token:
        raise HTTPException(status_code=503, detail="Ingest not configured on this server")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != ingest_token:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    city = body.get("city", "")
    check_in_str = body.get("check_in", "")
    check_out_str = body.get("check_out", "")
    tier = body.get("tier", "")
    deals = body.get("deals", [])

    if not all([city, check_in_str, check_out_str, tier]):
        raise HTTPException(status_code=400, detail="Missing required fields: city, check_in, check_out, tier")

    from datetime import date as _date
    try:
        check_in = _date.fromisoformat(check_in_str)
        check_out = _date.fromisoformat(check_out_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")

    db_path = os.getenv("DEAL_SCANNER_DB", "/tmp/geoprice_deals.db")
    from deal_scanner import init_db as _init_db, save_deals as _save_deals
    _init_db(db_path)
    _save_deals(db_path, city, check_in, check_out, tier, deals, 0)

    return {"ok": True, "stored": len(deals), "city": city}


@app.get("/api/neko-sessions")
async def neko_sessions_status():
    """Return active neko session count and available slots."""
    active = len(neko_sessions)
    total = NEKO_HTTP_PORT_MAX - NEKO_HTTP_PORT_MIN + 1
    return {"active": active, "available": total - active, "total": total}


# ============== Health Check ==============

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "conversations_count": len(conversations),
        "active_searches": len([s for s in searches.values() if s.get("status") == "in_progress"])
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
