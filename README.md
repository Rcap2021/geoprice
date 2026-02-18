# GeoPrice Travel

A chat-based travel booking platform that finds the best hotel prices globally by checking rates from different geographic locations via residential proxy network.

**Users save 20-30%** on hotel bookings by finding geo-pricing arbitrage.

## How It Works

```
1. User chats: "I want to go to Paris in March, mid-range hotel"
2. AI extracts: destination, dates, hotel tier
3. System searches Booking.com from 12 countries simultaneously via proxy
4. User sees: "Hotel XYZ - $180 via Brazil (Save 28% vs US price of $250)"
5. User clicks to book at the discounted price
```

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Frontend      │────▶│   Backend API   │────▶│  Price Engine   │
│   (React)       │     │   (FastAPI)     │     │  (Playwright)   │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
                                                ┌─────────────────┐
                                                │  Proxy Network  │
                                                │  (Exalive.ai)   │
                                                └─────────────────┘
```

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Anthropic API key
- Residential proxy credentials (Exalive.ai or similar)

### Setup

1. **Clone and configure**
```bash
cd geoprice
cp .env.example .env
# Edit .env with your credentials
```

2. **Run with Docker**
```bash
docker-compose up --build
```

3. **Access**
- Frontend: http://localhost:3000
- API: http://localhost:8000
- API docs: http://localhost:8000/docs

### Local Development (without Docker)

```bash
# Backend
cd backend
pip install -r requirements.txt
playwright install chromium

# Set environment variables
export ANTHROPIC_API_KEY=sk-ant-xxx
export PROXY_BASE_URL=http://proxy.exalive.ai:PORT
export PROXY_USERNAME=your_user
export PROXY_PASSWORD=your_pass

# Run
uvicorn main:app --reload --port 8000

# Frontend (in another terminal)
cd frontend
python -m http.server 3000
# Or just open index.html in browser
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key for chat |
| `PROXY_BASE_URL` | Yes | Proxy server URL |
| `PROXY_USERNAME` | Yes | Proxy username |
| `PROXY_PASSWORD` | Yes | Proxy password |
| `PROXY_FORMAT` | No | `username` (default), `url`, or `env` |
| `MAX_CONCURRENT_SCRAPERS` | No | Default: 4 |
| `HEADLESS` | No | `true` (default) or `false` for debugging |

### Proxy Configuration Formats

**Option 1: Username-based geo targeting** (most common for residential proxies)
```env
PROXY_BASE_URL=http://proxy.exalive.ai:PORT
PROXY_USERNAME=customer123
PROXY_PASSWORD=secret
PROXY_FORMAT=username
# Results in: customer123-country-br:secret@proxy.exalive.ai:PORT
```

**Option 2: Per-country environment variables**
```env
PROXY_BR=http://user:pass@br.proxy.exalive.ai:10001
PROXY_IN=http://user:pass@in.proxy.exalive.ai:10002
PROXY_TR=http://user:pass@tr.proxy.exalive.ai:10003
```

**Option 3: URL-based geo targeting**
```env
PROXY_BASE_URL=http://user:pass@proxy.exalive.ai
PROXY_FORMAT=url
# Results in: http://user:pass@proxy.exalive.ai-country-br
```

## API Endpoints

### Chat
```
POST /api/chat
{
  "conversation_id": "optional-uuid",
  "message": "I want to go to Paris next month"
}

Response:
{
  "conversation_id": "uuid",
  "response": "Great! What type of hotel...",
  "intent": {
    "destination_city": "Paris",
    "check_in": "2025-04-01",
    ...
  },
  "intent_complete": false,
  "search_triggered": false
}
```

### Search
```
POST /api/search/trigger
{
  "destination_city": "Paris",
  "check_in": "2025-04-01",
  "check_out": "2025-04-05",
  "guests": 2,
  "rooms": 1,
  "hotel_tier": "mid-range"
}

Response:
{
  "search_id": "uuid",
  "status": "pending"
}
```

### Get Results
```
GET /api/search/{search_id}

Response:
{
  "status": "completed",
  "best_deals": [
    {
      "hotel_name": "Hotel Le Marais",
      "usd_price": 145.00,
      "baseline_usd_price": 198.00,
      "savings_percent": 26.8,
      "geo_country": "BR",
      "geo_country_name": "Brazil",
      "booking_url": "https://booking.com/..."
    }
  ]
}
```

### WebSocket Stream
```javascript
const ws = new WebSocket('ws://localhost:8000/api/search/{search_id}/stream');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(`Progress: ${data.progress}%`);
  console.log(`Countries checked: ${data.geos_completed}`);
};
```

## Countries Checked

| Code | Country | Currency | Typical Savings |
|------|---------|----------|-----------------|
| BR | Brazil | BRL | 20-35% |
| IN | India | INR | 15-30% |
| AR | Argentina | ARS | 25-40% |
| TR | Turkey | TRY | 15-25% |
| ID | Indonesia | IDR | 10-25% |
| TH | Thailand | THB | 10-20% |
| PL | Poland | PLN | 10-20% |
| MX | Mexico | MXN | 10-20% |
| ZA | South Africa | ZAR | 10-20% |
| PT | Portugal | EUR | 5-15% |
| US | United States | USD | Baseline |
| GB | United Kingdom | GBP | Baseline |

## Project Structure

```
geoprice/
├── backend/
│   ├── main.py           # FastAPI app, endpoints
│   ├── chat_service.py   # Claude-powered intent extraction
│   ├── price_engine.py   # Multi-geo Booking.com scraper
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── index.html        # React chat UI (single file)
├── docker-compose.yml
├── nginx.conf
├── .env.example
└── README.md
```

## Debugging

### View browser during scraping
```bash
export HEADLESS=false
uvicorn main:app --reload
```

### Test single geo
```python
from price_engine import PriceEngine, TravelIntent
from datetime import date
import asyncio

async def test():
    engine = PriceEngine()
    intent = TravelIntent(
        destination_city="Paris",
        check_in=date(2025, 4, 15),
        check_out=date(2025, 4, 20),
        guests=2,
        rooms=1,
        hotel_tier="mid-range"
    )
    
    async for update in engine.search_all_geos(intent, geos=["US", "BR"]):
        print(update)

asyncio.run(test())
```

### Common Issues

**No hotels found:**
- Booking.com may have changed their HTML selectors
- Check browser console for errors with `HEADLESS=false`
- Verify proxy is working correctly

**Proxy connection errors:**
- Verify proxy credentials
- Check if geo-targeting format matches your proxy provider
- Test with a single country first

**Claude API errors:**
- Verify `ANTHROPIC_API_KEY` is set correctly
- Check API rate limits

## Next Steps

See the full project plan for:
- [ ] Remote browser for booking (Phase 2)
- [ ] Affiliate link injection
- [ ] User authentication
- [ ] Database persistence
- [ ] Email campaigns & retargeting
- [ ] Expand to flights, car rentals

## License

Proprietary - Internal Use Only
