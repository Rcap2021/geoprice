// GeoPrice Travel — Popup Script

const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const clearBtn = document.getElementById('clear-btn');

// Query background service worker for current state
chrome.runtime.sendMessage({ type: 'GEOPRICE_STATUS' }, (response) => {
  if (chrome.runtime.lastError || !response) return;

  if (response.activeTabId !== null && response.activeTabId !== undefined) {
    statusDot.classList.remove('dot-idle');
    statusDot.classList.add('dot-active');
    statusText.textContent = 'Proxy active';
    clearBtn.disabled = false;
  } else {
    statusText.textContent = 'Idle — no active proxy';
    clearBtn.disabled = true;
  }
});

clearBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'GEOPRICE_CLEAR' }, () => {
    statusDot.classList.remove('dot-active');
    statusDot.classList.add('dot-idle');
    statusText.textContent = 'Idle — proxy cleared';
    clearBtn.disabled = true;
  });
});
