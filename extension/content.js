// GeoPrice Travel — Content Script
// Injected into hotels.chatleg.ai to:
//   1. Signal that the extension is installed (detectable by the page)
//   2. Relay GEOPRICE_OPEN messages from the page to the background service worker

// Mark the page so the frontend can detect the extension
document.documentElement.setAttribute('data-geoprice-ext', '1.0');

// Relay window.postMessage → chrome.runtime.sendMessage
window.addEventListener('message', (event) => {
  // Only accept messages from the same window (the page)
  if (event.source !== window) return;
  if (!event.data || event.data.type !== 'GEOPRICE_OPEN') return;

  chrome.runtime.sendMessage(event.data, (response) => {
    if (chrome.runtime.lastError) {
      console.error('[GeoPrice] Relay error:', chrome.runtime.lastError.message);
    }
  });
});
