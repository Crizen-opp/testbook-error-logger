// background.js - Service worker. Content scripts running on HTTPS pages can't
// reliably fetch HTTP localhost due to Chrome's mixed-content policy. The
// service worker runs in the extension's own security context and can,
// so we proxy all server calls through here.

const SERVER_URL = 'http://localhost:8787';

// MV3 service workers are killed after ~30s idle. Keep-alive alarm prevents
// "Extension context invalidated" errors when the user clicks after a pause.
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(() => {});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'fetch') {
    const opts = msg.options || {};
    fetch(SERVER_URL + msg.path, opts)
      .then(async (r) => {
        const body = await r.text();
        sendResponse({ ok: r.ok, status: r.status, body });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: err.message });
      });
    return true; // keep channel open for async response
  }
});
