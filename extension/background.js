// GeoPrice Travel — Background Service Worker
// Routes booking.com through the server relay proxy (hotels.chatleg.ai:8766)
// using a PAC script + token-based proxy auth via onAuthRequired.

let activeProxyTabId = null;
let currentToken = null;   // token for the active proxy session

// ── Proxy auth: supply token when the relay proxy sends a 407 challenge ──
chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {
    if (details.isProxy && currentToken) {
      callback({ authCredentials: { username: currentToken, password: 'x' } });
    } else {
      callback({});
    }
  },
  { urls: ['<all_urls>'] },
  ['asyncBlocking']
);

// ── Message handler ──
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'GEOPRICE_OPEN') {
    handleGeoOpen(msg).then(() => sendResponse({ ok: true })).catch(err => {
      console.error('[GeoPrice] Error handling GEOPRICE_OPEN:', err);
      sendResponse({ ok: false, error: err.message });
    });
    return true; // keep channel open for async response
  }

  if (msg.type === 'GEOPRICE_STATUS') {
    sendResponse({ activeTabId: activeProxyTabId });
    return true;
  }

  if (msg.type === 'GEOPRICE_CLEAR') {
    clearProxy();
    sendResponse({ ok: true });
    return true;
  }
});

async function handleGeoOpen({ token, booking_url, geo }) {
  // 1. Store token so onAuthRequired can supply it
  currentToken = token;

  // 2. Fetch PAC script from server (now points to relay: hotels.chatleg.ai:8766)
  const res = await fetch(`https://hotels.chatleg.ai/api/pac/${token}`);
  if (!res.ok) {
    currentToken = null;
    throw new Error(`PAC fetch failed: ${res.status}`);
  }
  const { pac } = await res.json();

  // 3. Set proxy to inline PAC — routes only *.booking.com through relay
  await new Promise((resolve, reject) => {
    chrome.proxy.settings.set(
      { value: { mode: 'pac_script', pacScript: { data: pac } }, scope: 'regular' },
      () => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve();
        }
      }
    );
  });

  // 4. Open booking URL in new tab
  const tab = await new Promise((resolve) => {
    chrome.tabs.create({ url: booking_url }, resolve);
  });

  activeProxyTabId = tab.id;

  // 5. Clear proxy when the booking tab is closed
  chrome.tabs.onRemoved.addListener(function cleanup(tabId) {
    if (tabId === tab.id) {
      chrome.tabs.onRemoved.removeListener(cleanup);
      clearProxy();
    }
  });
}

function clearProxy() {
  currentToken = null;
  chrome.proxy.settings.clear({ scope: 'regular' }, () => {
    if (chrome.runtime.lastError) {
      console.warn('[GeoPrice] Error clearing proxy:', chrome.runtime.lastError.message);
    }
  });
  activeProxyTabId = null;
}
