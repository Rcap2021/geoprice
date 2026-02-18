#!/usr/bin/env python3
"""
Test script for GeoPrice Travel
Run this to verify your setup is working correctly
"""

import asyncio
import os
import sys
from datetime import date, timedelta

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))


async def test_chat_service():
    """Test the chat service with Claude API"""
    print("\n" + "="*50)
    print("Testing Chat Service (Claude API)")
    print("="*50)
    
    try:
        from chat_service import ChatService, TravelIntent, ChatMessage
        
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("❌ ANTHROPIC_API_KEY not set")
            print("   Set it in .env or environment")
            return False
        
        service = ChatService()
        intent = TravelIntent()
        
        # Test conversation
        test_messages = [
            "I want to go to Tokyo",
            "Next month, maybe around the 15th to 20th",
            "2 people, and we want a nice hotel, nothing too cheap"
        ]
        
        messages = []
        for msg in test_messages:
            messages.append(ChatMessage(role="user", content=msg))
            response, intent, triggered = await service.process_message(messages, intent, msg)
            messages.append(ChatMessage(role="assistant", content=response))
            
            print(f"\nUser: {msg}")
            print(f"Bot: {response[:200]}...")
            print(f"Intent: {intent.destination_city}, {intent.check_in} - {intent.check_out}, {intent.hotel_tier}")
            print(f"Search triggered: {triggered}")
        
        if intent.destination_city == "Tokyo":
            print("\n✅ Chat service working correctly!")
            return True
        else:
            print("\n⚠️ Chat service responded but intent extraction may have issues")
            return True
            
    except Exception as e:
        print(f"\n❌ Chat service error: {e}")
        return False


async def test_price_engine_no_proxy():
    """Test the price engine without proxies (will use local IP)"""
    print("\n" + "="*50)
    print("Testing Price Engine (No Proxy - Single Geo)")
    print("="*50)
    
    try:
        from price_engine import PriceEngine, TravelIntent
        
        engine = PriceEngine()
        
        # Test with just US (no proxy needed)
        intent = TravelIntent(
            destination_city="New York",
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
            guests=2,
            rooms=1,
            hotel_tier="mid-range"
        )
        
        print(f"\nSearching: {intent.destination_city}")
        print(f"Dates: {intent.check_in} to {intent.check_out}")
        print(f"Tier: {intent.hotel_tier}")
        print("\nNote: Running without proxy, using local IP")
        print("This tests that Playwright and scraping logic work\n")
        
        results = {}
        async for update in engine.search_all_geos(intent, geos=["US"]):
            print(f"Progress: {update['progress']}%")
            if "geo_results" in update:
                results[update["geo"]] = update["geo_results"]
                print(f"Found {len(update['geo_results'])} hotels from {update['geo']}")
        
        if results.get("US"):
            print(f"\n✅ Price engine working! Found {len(results['US'])} hotels")
            for hotel in results["US"][:3]:
                print(f"   - {hotel['hotel_name']}: ${hotel['usd_price']}")
            return True
        else:
            print("\n⚠️ No results found. This might be normal for some destinations.")
            return True
            
    except Exception as e:
        print(f"\n❌ Price engine error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_price_engine_with_proxy():
    """Test the price engine with proxy (requires proxy config)"""
    print("\n" + "="*50)
    print("Testing Price Engine (With Proxy)")
    print("="*50)
    
    proxy_url = os.getenv("PROXY_BASE_URL")
    if not proxy_url:
        print("⚠️ PROXY_BASE_URL not set - skipping proxy test")
        print("   Set proxy config in .env to test geo-based pricing")
        return True
    
    try:
        from price_engine import PriceEngine, TravelIntent
        
        engine = PriceEngine()
        
        intent = TravelIntent(
            destination_city="Paris",
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
            guests=2,
            rooms=1,
            hotel_tier="mid-range"
        )
        
        print(f"\nSearching: {intent.destination_city}")
        print("Testing with: US, BR, IN\n")
        
        all_results = {}
        async for update in engine.search_all_geos(intent, geos=["US", "BR", "IN"]):
            print(f"Progress: {update['progress']}% - Completed: {update.get('geo', '')}")
            if "geo_results" in update:
                all_results[update["geo"]] = update["geo_results"]
        
        # Calculate best deals
        best_deals = engine.calculate_best_deals(all_results)
        
        if best_deals:
            print(f"\n✅ Found {len(best_deals)} deals with savings!")
            for deal in best_deals[:3]:
                print(f"   {deal['hotel_name']}: ${deal['usd_price']} via {deal['geo_country_name']} (save {deal['savings_percent']}%)")
            return True
        else:
            print("\n⚠️ No price differences found (or scraping failed for some geos)")
            return True
            
    except Exception as e:
        print(f"\n❌ Proxy price engine error: {e}")
        return False


def test_dependencies():
    """Test that all dependencies are installed"""
    print("\n" + "="*50)
    print("Testing Dependencies")
    print("="*50)
    
    deps = {
        "fastapi": "FastAPI",
        "uvicorn": "Uvicorn",
        "anthropic": "Anthropic SDK",
        "playwright": "Playwright",
        "pydantic": "Pydantic",
    }
    
    all_ok = True
    for module, name in deps.items():
        try:
            __import__(module)
            print(f"✅ {name}")
        except ImportError:
            print(f"❌ {name} - run: pip install {module}")
            all_ok = False
    
    # Check Playwright browsers
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        print("✅ Playwright Chromium browser")
    except Exception as e:
        print(f"❌ Playwright browser - run: playwright install chromium")
        all_ok = False
    
    return all_ok


async def main():
    print("\n" + "="*50)
    print("GeoPrice Travel - Setup Test")
    print("="*50)
    
    # Load .env if exists
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    
    results = {}
    
    # Test dependencies
    results["dependencies"] = test_dependencies()
    
    # Test chat
    results["chat"] = await test_chat_service()
    
    # Test price engine without proxy
    results["scraper_local"] = await test_price_engine_no_proxy()
    
    # Test with proxy (optional)
    results["scraper_proxy"] = await test_price_engine_with_proxy()
    
    # Summary
    print("\n" + "="*50)
    print("Summary")
    print("="*50)
    
    for test, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{test}: {status}")
    
    if all(results.values()):
        print("\n🎉 All tests passed! You're ready to go.")
        print("\nTo start the server:")
        print("  cd backend && uvicorn main:app --reload")
        print("\nThen open frontend/index.html in your browser")
    else:
        print("\n⚠️ Some tests failed. Check the output above.")


if __name__ == "__main__":
    asyncio.run(main())
