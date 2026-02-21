# Chrome Web Store Submission Guide — GeoPrice Travel

## Submission URL
https://chrome.google.com/webstore/devconsole

---

## Extension Details

| Field | Value |
|---|---|
| Name | GeoPrice — Hotel Price Arbitrage |
| Short description (132 chars max) | Find hotels up to 70% cheaper. Compares Booking.com prices across 32 countries via geo proxy. One click to book the cheapest rate. |
| Category | Productivity |
| Language | English |
| Website | https://hotels.chatleg.ai |

---

## Detailed Description (store listing)

```
🌍 GeoPrice Travel — Book Hotels at Global Best Prices

Booking.com shows different prices depending on which country you're browsing from.
GeoPrice automatically finds the cheapest version of any hotel listing across 32
countries — then routes your booking through the right geo proxy so you get that price.

HOW IT WORKS
1. Visit hotels.chatleg.ai and search for any city
2. Click "Book Cheapest" on a deal card
3. The extension opens Booking.com through a geo-targeted proxy — no VPN needed
4. Complete your booking at the discounted geo price

TYPICAL SAVINGS
• Budget hotels: 20–40% cheaper from India, Brazil, or Argentina
• Luxury hotels: 30–70% cheaper from Southeast Asia or Eastern Europe
• Real examples: $578 hotel → $468 from Malaysia (save $110 per stay)

PRIVACY & SECURITY
• Only routes *.booking.com traffic — your other browsing is never touched
• Proxy is active only while you have a booking tab open
• "Clear Proxy" button instantly removes all routing
• No personal data collected or stored

PERMISSIONS EXPLAINED
• proxy: Required to route booking.com requests through geo proxy
• tabs: Detects when your booking tab closes (to auto-clear the proxy)
• storage: Saves your session token so the proxy survives browser restarts
• webRequest + webRequestAuthProvider: Handles proxy authentication silently

WORKS WITH
• All Booking.com hotel listings
• Chrome 102+
• Compatible with hotels.chatleg.ai deal cards
```

---

## Privacy Policy URL
https://hotels.chatleg.ai/privacy

*(Create a simple privacy policy page at this URL before submitting)*

---

## Screenshots Required (1-5, min 1280×800 or 640×400)

Take these screenshots before submitting:

1. **Main page** — hotels.chatleg.ai showing deal cards with savings badges
2. **Popup active** — Extension popup showing "Proxy active" with green dot
3. **Booking flow** — booking.com open with geo-targeted pricing visible
4. **Deal card close-up** — showing price comparison and savings amount

Recommended size: **1280×800** PNG

---

## Pre-submission Checklist

- [ ] Privacy policy page live at https://hotels.chatleg.ai/privacy
- [ ] Extension tested on Chrome stable (102+)
- [ ] All 4 icons present: 16, 32, 48, 128 px (in `/icons/`)
- [ ] `geoprice-extension.zip` is current (run `zip -r geoprice-extension.zip manifest.json background.js content.js popup.html popup.js icons/`)
- [ ] At least 1 screenshot uploaded (1280×800 PNG recommended)
- [ ] Single-purpose description matches manifest `description` field
- [ ] Reviewed Chrome Web Store Developer Program Policies

---

## Justification for Permissions (needed for review)

Chrome Web Store review may ask you to justify each permission:

- **proxy**: Routes only `*.booking.com` traffic through the geo proxy. The PAC script explicitly returns `DIRECT` for all other hosts.
- **webRequestAuthProvider**: Handles the proxy 407 challenge silently so no credential dialogs appear. Only supplies credentials for the server's own relay proxy (`hotels.chatleg.ai`).
- **tabs**: Used solely to detect when the booking tab is closed and auto-clear the proxy.
- **storage**: Persists the session token (not personal data) so proxy auth survives service worker restarts.
