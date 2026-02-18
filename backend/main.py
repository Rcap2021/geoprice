"""
GeoPrice Travel - Backend API
Chat interface + Price search engine
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from uuid import uuid4, UUID
import asyncio
import json
import os
import secrets
import subprocess
from pathlib import Path

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

# Browser session pool — ports 16080-16099 (20 concurrent sessions)
BROWSER_PORTS = list(range(16080, 16100))
browser_sessions: Dict[str, dict] = {}
browser_port_lock = asyncio.Lock()

# Services
chat_service = ChatService()
price_engine = PriceEngine()


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
    proxy_format = os.getenv("PROXY_FORMAT", "viprox")
    proxy_base_url = os.getenv("PROXY_BASE_URL", "")
    proxy_username = os.getenv("PROXY_USERNAME", "")
    proxy_password = os.getenv("PROXY_PASSWORD", "")

    if not proxy_base_url:
        return ""

    geo_lower = geo_code.lower()

    if proxy_format == "viprox":
        # e.g. http://chatleg-rc_my:password@gw-magic.viprox.net:7383
        host_part = proxy_base_url.replace("http://", "").replace("https://", "")
        scheme = "https" if proxy_base_url.startswith("https://") else "http"
        geo_username = f"{proxy_username}-rc_{geo_lower}"
        return f"{scheme}://{geo_username}:{proxy_password}@{host_part}"

    return proxy_base_url


@app.post("/api/browse")
async def browse(request: BrowseRequest):
    """
    Spawn a geo-targeted remote browser (Docker + noVNC) for a given URL and country.
    Returns a session URL pointing to the noVNC web UI through nginx.
    """
    async with browser_port_lock:
        # Also check ports actually bound by Docker (survives backend restarts)
        try:
            ps = subprocess.run(
                ["docker", "ps", "--format", "{{.Ports}}"],
                capture_output=True, text=True, timeout=5
            )
            docker_ports = set()
            for line in ps.stdout.splitlines():
                for segment in line.split(","):
                    segment = segment.strip()
                    if "127.0.0.1:" in segment:
                        try:
                            docker_ports.add(int(segment.split("127.0.0.1:")[1].split("->")[0]))
                        except (IndexError, ValueError):
                            pass
        except Exception:
            docker_ports = set()

        used_ports = {s["port"] for s in browser_sessions.values()} | docker_ports
        free_ports = [p for p in BROWSER_PORTS if p not in used_ports]
        if not free_ports:
            raise HTTPException(
                status_code=503,
                detail="All browser sessions are in use. Please try again in a few minutes."
            )
        port = free_ports[0]
        slot_index = BROWSER_PORTS.index(port)

        session_id = secrets.token_hex(8)
        container_name = f"browse_{session_id}"

        proxy_url = _build_proxy_url_for_geo(request.country)

        docker_cmd = [
            "docker", "run", "-d", "--rm",
            "--name", container_name,
            "-p", f"127.0.0.1:{port}:6080",
            "-e", f"PROXY_URL={proxy_url}",
            "-e", f"START_URL={request.url}",
            "--memory=512m",
            "--cpus=0.5",
            "geoprice-browser"
        ]

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=15
            )
            if result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to start browser: {result.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=500, detail="Docker launch timed out")

        browser_sessions[session_id] = {
            "port": port,
            "slot": slot_index,
            "container_name": container_name,
            "country": request.country,
            "url": request.url,
            "started_at": datetime.utcnow().isoformat(),
        }

    # Wait until noVNC/websockify is actually accepting connections (max 15s)
    import socket
    for _ in range(30):
        await asyncio.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except (ConnectionRefusedError, OSError):
            continue

    # Schedule cleanup after 30 minutes (outside the lock)
    asyncio.create_task(_cleanup_browser_session(session_id, container_name, delay=1800))

    session_url = f"/browse/s{slot_index}/vnc.html?autoconnect=true&resize=scale"
    return {
        "session_url": session_url,
        "session_id": session_id,
        "expires_in": 1800
    }


async def _cleanup_browser_session(session_id: str, container_name: str, delay: int = 1800):
    """Stop a browser container and release its port slot after `delay` seconds."""
    await asyncio.sleep(delay)
    try:
        subprocess.run(["docker", "stop", container_name], timeout=10, capture_output=True)
    except Exception:
        pass
    browser_sessions.pop(session_id, None)


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
