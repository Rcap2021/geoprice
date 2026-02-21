// GeoPrice Travel — Popup Script v1.3.0

const manifest = chrome.runtime.getManifest();
const versionStr = `v${manifest.version}`;
document.getElementById('ext-version').textContent = versionStr;
document.getElementById('ext-version-footer').textContent = versionStr;

// ── DOM refs ──────────────────────────────────────────────────────────────────
const idleState      = document.getElementById('idle-state');
const activeState    = document.getElementById('active-state');
const sessionsList   = document.getElementById('sessions-list');
const clearAllBtn    = document.getElementById('clear-all-btn');
const multiNotice    = document.getElementById('multi-proxy-notice');

const countrySelect  = document.getElementById('country-select');
const btnConnect     = document.getElementById('btn-connect');
const btnDisconnect  = document.getElementById('btn-disconnect');
const connectedRow   = document.getElementById('connected-row');
const connLabelText  = document.getElementById('connected-label-text');
const pickerCard     = document.getElementById('picker-card');
const ipResult       = document.getElementById('ip-result');

// ── Country picker ────────────────────────────────────────────────────────────
countrySelect.addEventListener('change', () => {
  btnConnect.disabled = !countrySelect.value;
});

btnConnect.addEventListener('click', async () => {
  const geo = countrySelect.value;
  if (!geo) return;
  btnConnect.disabled = true;
  btnConnect.textContent = 'Connecting…';

  chrome.runtime.sendMessage({ type: 'GEOPRICE_MANUAL_PROXY', geo }, (resp) => {
    btnConnect.textContent = 'Connect';
    if (chrome.runtime.lastError || !resp?.ok) {
      alert(`Failed to connect: ${resp?.error || chrome.runtime.lastError?.message || 'Unknown error'}`);
      btnConnect.disabled = false;
      return;
    }
    showConnected(geo, resp.geoName);
  });
});

btnDisconnect.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'GEOPRICE_CLEAR_MANUAL' }, () => {
    showDisconnected();
  });
});

function flagEmoji(geo) {
  if (!geo || geo.length !== 2) return '🌐';
  const offset = 127397;
  return String.fromCodePoint(...[...geo.toUpperCase()].map(c => c.charCodeAt(0) + offset));
}

function showConnected(geo, geoName) {
  const label = `${flagEmoji(geo)} ${geoName || geo} proxy active`;
  connLabelText.textContent = label;
  connectedRow.style.display = '';
  pickerCard.classList.add('connected');
  verifyIp();
}

function verifyIp() {
  ipResult.className = 'ip-badge checking';
  ipResult.textContent = 'Verifying IP…';
  ipResult.style.display = '';
  fetch('http://ip-api.com/json')
    .then(r => r.json())
    .then(d => {
      if (d.status === 'success') {
        const flag = flagEmoji(d.countryCode);
        ipResult.className = 'ip-badge';
        ipResult.textContent = `✓ ${flag} ${d.query} — ${d.country}`;
      } else {
        ipResult.className = 'ip-badge checking';
        ipResult.textContent = 'IP check failed';
      }
    })
    .catch(() => {
      ipResult.className = 'ip-badge checking';
      ipResult.textContent = 'IP check unavailable';
    });
}

function showDisconnected() {
  connectedRow.style.display = 'none';
  ipResult.style.display = 'none';
  pickerCard.classList.remove('connected');
  countrySelect.value = '';
  btnConnect.disabled = true;
}

// Restore manual proxy state on open
chrome.runtime.sendMessage({ type: 'GEOPRICE_STATUS' }, (response) => {
  if (chrome.runtime.lastError || !response) return;
  if (response.manualGeo) {
    countrySelect.value = response.manualGeo;
    btnConnect.disabled = false;
    showConnected(response.manualGeo, response.manualGeoName);
  }
  renderSessions(response.sessions || {}, response.activeSessionId);
});

// ── Deal sessions ─────────────────────────────────────────────────────────────
function renderSessions(sessions, activeSessionId) {
  const ids = Object.keys(sessions);

  if (ids.length === 0) {
    idleState.style.display  = '';
    activeState.style.display = 'none';
    return;
  }

  idleState.style.display   = 'none';
  activeState.style.display = '';
  multiNotice.style.display = ids.length > 1 ? '' : 'none';

  sessionsList.innerHTML = '';

  ids.sort((a, b) => (sessions[b].openedAt || '').localeCompare(sessions[a].openedAt || ''));

  ids.forEach(sid => {
    const sess     = sessions[sid];
    const isActive = sid === activeSessionId;
    const flag     = flagEmoji(sess.geo);
    const geoLabel = sess.geo ? `${flag} ${sess.geo}` : '🌐 Unknown';

    const card = document.createElement('div');
    card.className = `session-card${isActive ? ' active' : ''}`;
    card.innerHTML = `
      <div class="dot ${isActive ? 'dot-active' : 'dot-idle'}"></div>
      <div class="session-info">
        <div class="session-geo">${geoLabel}</div>
        <div class="session-status ${isActive ? 'is-active' : ''}">
          ${isActive ? '● Proxy active' : '○ Proxy inactive'}
        </div>
      </div>
      <div class="session-actions">
        ${!isActive ? `<button class="btn-activate" data-sid="${sid}">Make Active</button>` : ''}
        <button class="btn-close" title="Close session" data-close="${sid}">✕</button>
      </div>
    `;
    sessionsList.appendChild(card);
  });

  sessionsList.querySelectorAll('.btn-activate').forEach(btn => {
    btn.addEventListener('click', () => {
      chrome.runtime.sendMessage({ type: 'GEOPRICE_ACTIVATE', sessionId: btn.dataset.sid }, () => {
        refreshStatus();
      });
    });
  });

  sessionsList.querySelectorAll('.btn-close').forEach(btn => {
    btn.addEventListener('click', () => {
      chrome.runtime.sendMessage({ type: 'GEOPRICE_CLOSE_SESSION', sessionId: btn.dataset.close }, () => {
        refreshStatus();
      });
    });
  });
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: 'GEOPRICE_STATUS' }, (response) => {
    if (chrome.runtime.lastError || !response) return;
    renderSessions(response.sessions || {}, response.activeSessionId);
  });
}

clearAllBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'GEOPRICE_CLEAR' }, () => {
    refreshStatus();
  });
});
