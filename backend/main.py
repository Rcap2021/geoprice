"""
GeoPrice Travel - Backend API
Chat interface + Price search engine
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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

# Browser session pool — ports 16080-16099 (20 slots, POOL_SIZE pre-warmed)
BROWSER_PORTS = list(range(16080, 16100))
POOL_SIZE = 5  # containers kept warm and ready at all times
browser_sessions: Dict[str, dict] = {}
slot_containers: Dict[int, str] = {}   # slot_index -> container_name
slot_status: Dict[int, str] = {}       # "warming" | "warm" | "active" | "free"
warm_queue: asyncio.Queue = asyncio.Queue()  # slot indices ready to hand out

# Services
chat_service = ChatService()
price_engine = PriceEngine()


# ============== Browser Pool ==============

async def _start_warm_slot(slot_index: int):
    """Start a warm (idle, about:blank) browser container at the given slot."""
    import httpx
    port = BROWSER_PORTS[slot_index]
    container_name = f"browse_warm_{slot_index}"
    slot_status[slot_index] = "warming"
    slot_containers[slot_index] = container_name

    # Remove any existing container at this slot
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10)

    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", container_name,
        "-p", f"127.0.0.1:{port}:6080",
        "-e", "START_URL=about:blank",
        "--memory=512m", "--cpus=0.5",
        "geoprice-browser"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            slot_status[slot_index] = "free"
            return
    except Exception:
        slot_status[slot_index] = "free"
        return

    # Wait for websockify to serve HTTP (up to 30s)
    async with httpx.AsyncClient() as client:
        for _ in range(60):
            await asyncio.sleep(0.5)
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/vnc.html", timeout=2.0)
                if resp.status_code == 200:
                    break
            except Exception:
                continue

    slot_status[slot_index] = "warm"
    await warm_queue.put(slot_index)


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
    """Handle one browser connection to the CONNECT relay proxy on port 8766."""
    import base64

    geo_writer = None
    try:
        method, target, headers = await _proxy_read_request(reader)

        if method != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        proxy_auth = headers.get("proxy-authorization", "")

        if not proxy_auth:
            # Challenge the browser — Chrome will fire onAuthRequired in the extension
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=\"GeoPrice\"\r\n"
                b"Content-Length: 0\r\n"
                b"Proxy-Connection: keep-alive\r\n"
                b"\r\n"
            )
            await writer.drain()
            # Browser retries on the same connection with credentials
            try:
                method2, target2, headers2 = await asyncio.wait_for(
                    _proxy_read_request(reader), timeout=15
                )
                if method2 == "CONNECT":
                    target, headers = target2, headers2
                    proxy_auth = headers2.get("proxy-authorization", "")
            except asyncio.TimeoutError:
                return

        if not proxy_auth or not proxy_auth.lower().startswith("basic "):
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=\"GeoPrice\"\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            await writer.drain()
            return

        # Decode Basic auth → token:x
        try:
            creds = base64.b64decode(proxy_auth[6:]).decode("utf-8", errors="replace")
            token = creds.split(":")[0]
        except Exception:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"GeoPrice\"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        # Validate token
        entry = proxy_tokens.get(token)
        if not entry or datetime.utcnow() > entry["expires_at"]:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"GeoPrice\"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        proxy_addr = entry["proxy_addr"]   # "host:port"
        proxy_host, proxy_port_str = proxy_addr.rsplit(":", 1)
        proxy_port = int(proxy_port_str)

        # Connect to the geo proxy
        try:
            geo_reader, geo_writer = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port), timeout=15
            )
        except Exception as e:
            print(f"[Relay] Cannot reach geo proxy {proxy_addr}: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        # Forward CONNECT to geo proxy (it's an HTTP proxy that supports CONNECT)
        geo_writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await geo_writer.drain()

        # Read geo proxy's response to our CONNECT
        try:
            geo_status = await asyncio.wait_for(geo_reader.readline(), timeout=15)
            # drain remaining headers
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

        # Tell the browser the tunnel is open
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

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
async def startup_browser_pool():
    """Stop leftover containers from previous runs, then pre-warm the pool."""
    try:
        ps = subprocess.run(
            ["docker", "ps", "-q", "--filter", "ancestor=geoprice-browser"],
            capture_output=True, text=True, timeout=10
        )
        for cid in ps.stdout.split():
            subprocess.run(["docker", "stop", cid], capture_output=True, timeout=10)
    except Exception:
        pass
    for i in range(POOL_SIZE):
        asyncio.create_task(_start_warm_slot(i))
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
    return FileResponse(zip_path, media_type="application/zip", filename="geoprice-extension.zip")


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


@app.post("/api/proxy-token")
async def create_proxy_token(request: ProxyTokenRequest):
    """Issue a short-lived token that the Chrome extension exchanges for a PAC script."""
    geo = request.geo_country.upper()
    proxy_addr = PriceEngine.GEO_PROXIES.get(geo)
    if not geo or not proxy_addr:
        raise HTTPException(status_code=400, detail="Unsupported geo or no proxy available")
    token = secrets.token_urlsafe(32)
    proxy_tokens[token] = {
        "geo_code": geo,
        "proxy_addr": proxy_addr,
        "expires_at": datetime.utcnow() + timedelta(minutes=30),
    }
    return {"token": token, "ttl": 1800}


@app.get("/api/pac/{token}")
async def get_pac_script(token: str):
    """Return a PAC script routing booking.com through the server relay proxy (token-gated)."""
    entry = proxy_tokens.get(token)
    if not entry or datetime.utcnow() > entry["expires_at"]:
        raise HTTPException(status_code=404, detail="Invalid or expired token")
    relay_host = os.getenv("PROXY_RELAY_HOST", "hotels.chatleg.ai")
    relay_port = int(os.getenv("PROXY_RELAY_PORT", "8766"))
    pac = (
        f'function FindProxyForURL(url, host) {{\n'
        f'  if (shExpMatch(host, "*.booking.com") || host === "booking.com") {{\n'
        f'    return "PROXY {relay_host}:{relay_port}";\n'
        f'  }}\n'
        f'  return "DIRECT";\n'
        f'}}'
    )
    return {"pac": pac, "geo": entry["geo_code"]}


@app.post("/api/browse")
async def browse(request: BrowseRequest):
    """
    Route user to a pre-warmed geo-targeted remote browser (Docker + noVNC).
    Returns a session URL pointing to the noVNC web UI through nginx.
    """
    try:
        slot_index = warm_queue.get_nowait()
    except asyncio.QueueEmpty:
        raise HTTPException(
            status_code=503,
            detail="No browser sessions available right now. Please try again in a moment."
        )

    container_name = slot_containers[slot_index]
    port = BROWSER_PORTS[slot_index]
    proxy_url = _build_proxy_url_for_geo(request.country)

    # Build proxy args for Chromium.
    # Direct proxies (no auth): pass --proxy-server directly.
    # Auth proxies: start a local relay inside the container (Chromium ignores
    # credentials embedded in the proxy URL).
    relay_cmd = ""
    proxy_arg = ""
    if proxy_url:
        m = re.match(r'https?://([^:]+):([^@]+)@(.+)', proxy_url)
        if m:
            # Auth proxy — use relay
            user, password, hostport = m.groups()
            relay_cmd = (
                f"pkill -f proxy-relay.py 2>/dev/null || true; "
                f"python3 /usr/local/bin/proxy-relay.py 18080 {hostport} {user}:{password} & "
                "sleep 0.5; "
            )
            proxy_arg = "--proxy-server=http://127.0.0.1:18080"
        else:
            # No-auth direct proxy — pass URL straight to Chromium
            proxy_arg = f"--proxy-server={proxy_url}"

    # Use the geo's local currency so Booking.com applies the correct pricing tier.
    # Forcing USD here causes Booking.com to show US-tier prices, wiping out the discount.
    from price_engine import PriceEngine
    url = request.url
    if 'booking.com' in url:
        sep = '&' if '?' in url else '?'
        if 'selected_currency=' not in url:
            geo_currency = PriceEngine.GEO_COUNTRIES.get(request.country, {}).get("currency", "USD")
            url += f'{sep}selected_currency={geo_currency}'
            sep = '&'
        if 'lang=' not in url:
            url += f'{sep}lang=en-us'

    safe_url = url.replace("'", "%27")

    # Kill all chromium processes inside the container.
    # pkill is not available in this image, so we use Python (which is present).
    # Match only the binary path (first null-delimited arg) to avoid killing
    # our own bash/python process that mentions "chromium" in its arguments.
    kill_chromium = (
        "python3 -c \""
        "import os,glob\n"
        "for f in glob.glob('/proc/*/cmdline'):\n"
        " try:\n"
        "  args=open(f,'rb').read().split(b'\\x00')\n"
        "  if args and b'chromium' in args[0]:\n"
        "   os.kill(int(f.split('/')[2]),9)\n"
        " except: pass\n"
        "\" 2>/dev/null; sleep 1; "
    )

    nav_script = (
        relay_cmd +
        kill_chromium +
        f"DISPLAY=:1 /usr/bin/chromium "
        "--no-sandbox --disable-dev-shm-usage --disable-gpu "
        "--disable-blink-features=AutomationControlled "
        "--window-size=1280,900 --start-maximized "
        "--no-first-run --disable-infobars "
        "--user-data-dir=/tmp/chrome-geo-session "
        f"--ignore-certificate-errors {proxy_arg} '{safe_url}' &"
    )
    result = subprocess.run(
        ["docker", "exec", "-d", container_name, "bash", "-c", nav_script],
        capture_output=True, timeout=10, text=True
    )
    if result.returncode != 0:
        print(f"[browse] docker exec failed (rc={result.returncode}): {result.stderr[:200]}")

    session_id = secrets.token_hex(8)
    slot_status[slot_index] = "active"
    browser_sessions[session_id] = {
        "port": port,
        "slot": slot_index,
        "container_name": container_name,
        "country": request.country,
        "url": request.url,
        "started_at": datetime.utcnow().isoformat(),
    }

    # Schedule cleanup and pool replenishment after 30 minutes
    asyncio.create_task(_cleanup_and_replenish(session_id, slot_index, delay=1800))

    # Fix: pass path so noVNC WebSocket connects through the right nginx location
    session_url = (
        f"/browse/s{slot_index}/vnc.html"
        f"?autoconnect=true&resize=scale&path=browse/s{slot_index}/"
    )
    return {
        "session_url": session_url,
        "session_id": session_id,
        "expires_in": 1800
    }


async def _cleanup_and_replenish(session_id: str, slot_index: int, delay: int = 1800):
    """Stop the used browser container after delay, then warm a fresh one at that slot."""
    await asyncio.sleep(delay)
    browser_sessions.pop(session_id, None)
    container_name = slot_containers.get(slot_index, "")
    try:
        subprocess.run(["docker", "stop", container_name], timeout=10, capture_output=True)
    except Exception:
        pass
    slot_status[slot_index] = "free"
    asyncio.create_task(_start_warm_slot(slot_index))


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
