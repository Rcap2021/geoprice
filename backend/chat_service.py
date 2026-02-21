"""
Chat Service - LLM-powered intent extraction
Uses Claude API to understand user travel requests
"""

import os
import json
import re
from typing import List, Tuple, Optional
from datetime import date, datetime
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(Path(__file__).parent / ".env")

# Will be imported from main
from pydantic import BaseModel


class TravelIntent(BaseModel):
    destination_city: Optional[str] = None
    destination_country: Optional[str] = None
    check_in: Optional[date] = None
    check_out: Optional[date] = None
    guests: int = 2
    rooms: int = 1
    hotel_tier: Optional[str] = None

    def is_complete(self) -> bool:
        return all([self.destination_city, self.check_in, self.check_out, self.hotel_tier])


class ChatMessage(BaseModel):
    role: str
    content: str


SYSTEM_PROMPT = """You are a friendly travel booking assistant for GeoPrice Travel. Your job is to help users find the best hotel deals by understanding their travel plans.

You need to extract the following information from the conversation:
1. Destination city (required)
2. Check-in date (required)
3. Check-out date (required)  
4. Number of guests (default: 2)
5. Number of rooms (default: 1)
6. Hotel tier preference (required): budget, mid-range, luxury, or ultra-luxury

Guidelines:
- Be conversational and friendly, not robotic
- Ask for missing information naturally, one or two questions at a time
- If user gives a date range like "March 15-20", parse both dates
- If user says "next weekend", calculate the actual dates based on today's date
- Accept various hotel tier descriptions:
  - "cheap", "affordable", "basic" → budget
  - "nice", "good", "comfortable", "3-star", "4-star" → mid-range  
  - "fancy", "upscale", "5-star", "high-end" → luxury
  - "best", "premium", "top", "presidential" → ultra-luxury
- Once you have ALL required fields, confirm the details and indicate you're ready to search

Today's date is: {today}

After each response, output a JSON block with the extracted intent and whether to trigger search:

```json
{{
  "intent": {{
    "destination_city": "Paris",
    "destination_country": "France",
    "check_in": "2025-03-15",
    "check_out": "2025-03-20",
    "guests": 2,
    "rooms": 1,
    "hotel_tier": "mid-range"
  }},
  "trigger_search": false
}}
```

Set "trigger_search": true ONLY when:
1. ALL required fields are filled (destination_city, check_in, check_out, hotel_tier)
2. User has confirmed they want to search OR you've just confirmed the details with them

Keep your responses concise - 2-3 sentences max unless explaining something specific."""


class ChatService:
    def __init__(self):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        
    async def process_message(
        self,
        messages: List[ChatMessage],
        current_intent: TravelIntent,
        user_message: str
    ) -> Tuple[str, TravelIntent, bool]:
        """
        Process user message – return response, updated intent, and whether to trigger search
        """
        
        # Build message history for Claude
        claude_messages = []
        for msg in messages:
            claude_messages.append({
                "role": msg.role if msg.role in ["user", "assistant"] else "user",
                "content": msg.content
            })
        
        # Add current intent context
        intent_context = f"\n\nCurrent extracted intent:\n{json.dumps(current_intent.model_dump(mode='json'), indent=2, default=str)}"
        
        # Call Claude
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=SYSTEM_PROMPT.format(today=date.today().isoformat()) + intent_context,
                messages=claude_messages
            )
            
            response_text = response.content[0].text
            
            # Parse the JSON from response
            updated_intent, trigger_search = self._parse_response(response_text, current_intent)
            
            # Clean the response (remove JSON block for user display)
            clean_response = self._clean_response(response_text)
            
            return clean_response, updated_intent, trigger_search
            
        except Exception as e:
            # Fallback if API fails
            print(f"Claude API error: {e}")
            return self._fallback_response(current_intent), current_intent, False
    
    def _parse_response(
        self, 
        response: str, 
        current_intent: TravelIntent
    ) -> Tuple[TravelIntent, bool]:
        """Extract JSON intent from Claude's response"""
        
        # Find JSON block
        json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if not json_match:
            # Try without code blocks
            json_match = re.search(r'\{[^{}]*"intent"[^{}]*\{.*?\}[^{}]*\}', response, re.DOTALL)
        
        if json_match:
            try:
                json_str = json_match.group(1) if '```' in response else json_match.group(0)
                data = json.loads(json_str)
                
                intent_data = data.get("intent", {})
                trigger_search = data.get("trigger_search", False)
                
                # Update intent with new values (keep existing if not provided)
                updated = TravelIntent(
                    destination_city=intent_data.get("destination_city") or current_intent.destination_city,
                    destination_country=intent_data.get("destination_country") or current_intent.destination_country,
                    check_in=self._parse_date(intent_data.get("check_in")) or current_intent.check_in,
                    check_out=self._parse_date(intent_data.get("check_out")) or current_intent.check_out,
                    guests=intent_data.get("guests") or current_intent.guests,
                    rooms=intent_data.get("rooms") or current_intent.rooms,
                    hotel_tier=intent_data.get("hotel_tier") or current_intent.hotel_tier
                )
                
                return updated, trigger_search
                
            except json.JSONDecodeError as e:
                print(f"JSON parse error: {e}")
        
        return current_intent, False
    
    def _parse_date(self, date_str: Optional[str]) -> Optional[date]:
        """Parse date string to date object"""
        if not date_str:
            return None
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            return None
    
    def _clean_response(self, response: str) -> str:
        """Remove JSON block from response for user display"""
        # Remove JSON code blocks
        cleaned = re.sub(r'```json\s*.*?\s*```', '', response, flags=re.DOTALL)
        # Remove any trailing whitespace
        cleaned = cleaned.strip()
        return cleaned
    
    def _fallback_response(self, intent: TravelIntent) -> str:
        """Generate fallback response when API fails"""
        missing = []
        if not intent.destination_city:
            missing.append("destination")
        if not intent.check_in:
            missing.append("check-in date")
        if not intent.check_out:
            missing.append("check-out date")
        if not intent.hotel_tier:
            missing.append("hotel preference (budget, mid-range, luxury)")
        
        if missing:
            return f"I'd love to help you find the best hotel deal! Could you tell me your {', '.join(missing)}?"
        else:
            return f"Great! I'll search for {intent.hotel_tier} hotels in {intent.destination_city} from {intent.check_in} to {intent.check_out}. Starting the search now!"


# Simple test
if __name__ == "__main__":
    import asyncio
    
    async def test():
        service = ChatService()
        intent = TravelIntent()
        
        # Simulate conversation
        messages = [
            ChatMessage(role="user", content="I want to go to Paris next month")
        ]
        
        response, intent, trigger = await service.process_message(messages, intent, messages[0].content)
        print(f"Response: {response}")
        print(f"Intent: {intent}")
        print(f"Trigger search: {trigger}")
    
    asyncio.run(test())
