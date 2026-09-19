// popup.js - kept in a separate file because Manifest V3 blocks inline scripts.
const SERVER = 'http://localhost:8787';

fetch(SERVER + '/api/stats')
  .then((r) => r.json())
  .then((d) => {
    document.getElementById('status').className = 'status ok';
    document.getElementById('status').textContent = '✅ Server online';
    document.getElementById('stats').innerHTML = `
      <div class="stat-row"><span>Total errors logged</span><strong>${d.total}</strong></div>
      <div class="stat-row"><span>Due for review</span><strong>${d.pending_review}</strong></div>
      <div class="stat-row"><span>Mastered</span><strong>${d.mastered}</strong></div>
    `;
  })
  .catch(() => {
    document.getElementById('status').className = 'status err';
    document.getElementById('status').textContent = '❌ Server offline. Run: python server/app.py';
  });

document.getElementById('openDash').onclick = () =>
  chrome.tabs.create({ url: SERVER + '/' });
