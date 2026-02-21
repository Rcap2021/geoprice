// GeoPrice Travel — Background Service Worker v1.4.3
// Port-based geo proxy: each country has a dedicated port on hotels.chatleg.ai.
// No tokens, no auth, no 407 challenges — just a PAC script pointing at the right port.

const PROXY_HOST = 'hotels.chatleg.ai';

// Flag emoji for each supported geo (used in toolbar badge)
const GEO_FLAGS = {
  AE: '🇦🇪', AT: '🇦🇹', AU: '🇦🇺', BD: '🇧🇩', CA: '🇨🇦',
  CH: '🇨🇭', CL: '🇨🇱', DE: '🇩🇪', ES: '🇪🇸', FR: '🇫🇷',
  GB: '🇬🇧', HK: '🇭🇰', HU: '🇭🇺', IE: '🇮🇪', IN: '🇮🇳',
  IT: '🇮🇹', JP: '🇯🇵', KE: '🇰🇪', KR: '🇰🇷', KZ: '🇰🇿',
  MX: '🇲🇽', MY: '🇲🇾', NL: '🇳🇱', NZ: '🇳🇿', PK: '🇵🇰',
  PL: '🇵🇱', SG: '🇸🇬', US: '🇺🇸', VN: '🇻🇳', ZA: '🇿🇦',
};

// Geo → proxy port mapping (must match geo-proxy/main.go PORT_MAP)
const GEO_PORTS = {
  IN: { port: 9101, name: 'India' },
  GB: { port: 9102, name: 'United Kingdom' },
  VN: { port: 9103, name: 'Vietnam' },
  MY: { port: 9104, name: 'Malaysia' },
  SG: { port: 9105, name: 'Singapore' },
  JP: { port: 9106, name: 'Japan' },
  HK: { port: 9107, name: 'Hong Kong' },
  CA: { port: 9108, name: 'Canada' },
  FR: { port: 9109, name: 'France' },
  PL: { port: 9110, name: 'Poland' },
  MX: { port: 9111, name: 'Mexico' },
  ZA: { port: 9112, name: 'South Africa' },
  BD: { port: 9113, name: 'Bangladesh' },
  PK: { port: 9114, name: 'Pakistan' },
  HU: { port: 9115, name: 'Hungary' },
  KZ: { port: 9116, name: 'Kazakhstan' },
  CL: { port: 9117, name: 'Chile' },
  KR: { port: 9118, name: 'South Korea' },
  US: { port: 9121, name: 'United States' },
  AE: { port: 9122, name: 'UAE' },
  AT: { port: 9123, name: 'Austria' },
  AU: { port: 9124, name: 'Australia' },
  CH: { port: 9125, name: 'Switzerland' },
  DE: { port: 9126, name: 'Germany' },
  ES: { port: 9127, name: 'Spain' },
  IE: { port: 9128, name: 'Ireland' },
  IT: { port: 9129, name: 'Italy' },
  KE: { port: 9130, name: 'Kenya' },
  NL: { port: 9131, name: 'Netherlands' },
  NZ: { port: 9132, name: 'New Zealand' },
};

function _buildPac(port) {
  return (
    `function FindProxyForURL(url, host) {\n` +
    `  if (shExpMatch(host, "*.booking.com") || host === "booking.com" ||\n` +
    `      shExpMatch(host, "*.ip-api.com") || host === "ip-api.com") {\n` +
    `    return "PROXY ${PROXY_HOST}:${port}";\n` +
    `  }\n` +
    `  return "DIRECT";\n` +
    `}`
  );
}

// Deal sessions: opened via "Book via Extension" on deal cards
let sessions = {};        // { [sessionId]: {tabId, geo, openedAt} }
let activeSessionId = null;

// Manual proxy: set via country picker in popup
let manualGeo = null;
let manualGeoName = null;

// ── Restore state after SW restart ───────────────────────────────────────────
chrome.storage.session.get(['sessions', 'activeSessionId', 'manualGeo', 'manualGeoName']).then(data => {
  if (data.sessions)        sessions        = data.sessions;
  if (data.activeSessionId) activeSessionId = data.activeSessionId;
  if (data.manualGeo)       manualGeo       = data.manualGeo;
  if (data.manualGeoName)   manualGeoName   = data.manualGeoName;
  _updateBadge();
}).catch(() => {});

function _persist() {
  chrome.storage.session.set({ sessions, activeSessionId, manualGeo, manualGeoName }).catch(() => {});
  _updateBadge();
}

async function _drawFlagIcon(flag) {
  const sizes = [16, 32, 48, 128];
  const imageData = {};
  for (const size of sizes) {
    const canvas = new OffscreenCanvas(size, size);
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, size, size);
    ctx.font = `${Math.round(size * 0.85)}px serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(flag, size / 2, size / 2 + Math.round(size * 0.05));
    imageData[size] = ctx.getImageData(0, 0, size, size);
  }
  return imageData;
}

async function _updateBadge() {
  const geo = manualGeo || (activeSessionId && sessions[activeSessionId]?.geo) || null;
  if (geo) {
    const flag = GEO_FLAGS[geo.toUpperCase()] || geo;
    try {
      const imageData = await _drawFlagIcon(flag);
      chrome.action.setIcon({ imageData });
    } catch (e) {
      console.warn('[GeoPrice] OffscreenCanvas icon failed:', e);
    }
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setTitle({ title: `GeoPrice — ${GEO_PORTS[geo]?.name || geo} proxy active` });
  } else {
    chrome.action.setIcon({ path: { 16: 'icons/icon16.png', 32: 'icons/icon32.png', 48: 'icons/icon48.png', 128: 'icons/icon128.png' } });
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setTitle({ title: 'GeoPrice Travel' });
  }
}

// ── Message handler ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  if (msg.type === 'GEOPRICE_OPEN') {
    handleGeoOpen(msg)
      .then(r => sendResponse({ ok: true, sessionId: r }))
      .catch(err => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.type === 'GEOPRICE_STATUS') {
    pruneClosedTabs().then(() => {
      sendResponse({ sessions, activeSessionId, manualGeo, manualGeoName });
    });
    return true;
  }

  if (msg.type === 'GEOPRICE_ACTIVATE') {
    const sess = sessions[msg.sessionId];
    if (sess) {
      applyGeoPac(sess.geo, msg.sessionId)
        .then(() => sendResponse({ ok: true }))
        .catch(err => sendResponse({ ok: false, error: err.message }));
    } else {
      sendResponse({ ok: false, error: 'Session not found' });
    }
    return true;
  }

  if (msg.type === 'GEOPRICE_CLOSE_SESSION') {
    closeSession(msg.sessionId).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === 'GEOPRICE_CLEAR') {
    clearAllSessions().then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === 'GEOPRICE_MANUAL_PROXY') {
    handleManualProxy(msg.geo)
      .then(r => sendResponse({ ok: true, geoName: r }))
      .catch(err => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.type === 'GEOPRICE_CLEAR_MANUAL') {
    clearManualProxy().then(() => sendResponse({ ok: true }));
    return true;
  }
});

// ── Manual proxy ──────────────────────────────────────────────────────────────
async function handleManualProxy(geo) {
  const entry = GEO_PORTS[geo.toUpperCase()];
  if (!entry) throw new Error(`Unsupported geo: ${geo}`);

  const pac = _buildPac(entry.port);
  await applyPacData(pac);

  manualGeo     = geo.toUpperCase();
  manualGeoName = entry.name;
  // Deactivate any deal session proxy
  activeSessionId = null;
  _persist();

  // Verify the extension controls the proxy
  const level = await new Promise(resolve => {
    chrome.proxy.settings.get({}, d => resolve(d.levelOfControl));
  });
  console.log('[GeoPrice] Proxy levelOfControl:', level);
  if (level !== 'controlled_by_this_extension') {
    throw new Error(`Proxy blocked by ${level}. Disable other proxy/VPN extensions first.`);
  }

  return entry.name;
}

async function clearManualProxy() {
  manualGeo     = null;
  manualGeoName = null;
  if (activeSessionId && sessions[activeSessionId]) {
    await applyGeoPac(sessions[activeSessionId].geo, activeSessionId).catch(() => {});
  } else {
    await clearProxy();
  }
  _persist();
}

// ── Deal session helpers ──────────────────────────────────────────────────────
async function handleGeoOpen({ booking_url, geo }) {
  const tab = await new Promise(resolve => {
    chrome.tabs.create({ url: booking_url }, resolve);
  });

  const sessionId = `sess_${Date.now()}_${tab.id}`;
  sessions[sessionId] = {
    tabId: tab.id,
    geo: geo || '??',
    openedAt: new Date().toISOString(),
  };

  // Deal session proxy overrides manual proxy
  manualGeo = null; manualGeoName = null;
  await applyGeoPac(geo, sessionId);
  _persist();

  chrome.tabs.onRemoved.addListener(function onTabRemoved(tabId) {
    if (tabId === tab.id) {
      chrome.tabs.onRemoved.removeListener(onTabRemoved);
      closeSession(sessionId);
    }
  });

  return sessionId;
}

async function applyGeoPac(geo, sessionId) {
  const entry = GEO_PORTS[geo?.toUpperCase()];
  if (!entry) throw new Error(`No proxy port for geo: ${geo}`);
  const pac = _buildPac(entry.port);
  await applyPacData(pac);
  activeSessionId = sessionId;
  _persist();
}

async function applyPacData(pac) {
  return new Promise((resolve, reject) => {
    chrome.proxy.settings.set(
      { value: { mode: 'pac_script', pacScript: { data: pac } }, scope: 'regular' },
      () => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve();
      }
    );
  });
}

async function closeSession(sessionId) {
  delete sessions[sessionId];
  if (activeSessionId === sessionId) {
    const remaining = Object.keys(sessions);
    if (remaining.length > 0) {
      const latest = remaining.sort().pop();
      await applyGeoPac(sessions[latest].geo, latest).catch(() => {});
    } else if (manualGeo) {
      await handleManualProxy(manualGeo).catch(() => {});
    } else {
      await clearProxy();
    }
  }
  _persist();
}

async function clearAllSessions() {
  sessions = {};
  activeSessionId = null;
  if (!manualGeo) await clearProxy();
  _persist();
}

async function clearProxy() {
  activeSessionId = null;
  chrome.proxy.settings.clear({ scope: 'regular' }, () => {
    if (chrome.runtime.lastError) console.warn('[GeoPrice] Error clearing proxy:', chrome.runtime.lastError.message);
  });
}

async function pruneClosedTabs() {
  const tabIds = Object.values(sessions).map(s => s.tabId).filter(Boolean);
  if (!tabIds.length) return;
  await new Promise(resolve => {
    let checked = 0;
    tabIds.forEach(tabId => {
      chrome.tabs.get(tabId, tab => {
        if (chrome.runtime.lastError || !tab) {
          for (const [sid, s] of Object.entries(sessions)) {
            if (s.tabId === tabId) closeSession(sid);
          }
        }
        if (++checked === tabIds.length) resolve();
      });
    });
  });
}
