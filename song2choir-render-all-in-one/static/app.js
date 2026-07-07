const form = document.getElementById('renderForm');
const apiUrlInput = document.getElementById('apiUrl');
const audioFile = document.getElementById('audioFile');
const uploadTitle = document.getElementById('uploadTitle');
const uploadMeta = document.getElementById('uploadMeta');
const originalPlayer = document.getElementById('originalPlayer');
const resultPlayer = document.getElementById('resultPlayer');
const statusBadge = document.getElementById('statusBadge');
const healthBadge = document.getElementById('healthBadge');
const statusTitle = document.getElementById('statusTitle');
const statusText = document.getElementById('statusText');
const progressBar = document.getElementById('progressBar');
const renderBtn = document.getElementById('renderBtn');
const resetBtn = document.getElementById('resetBtn');
const downloadLink = document.getElementById('downloadLink');
const metaPanel = document.getElementById('metaPanel');
const metaStyle = document.getElementById('metaStyle');
const metaTempo = document.getElementById('metaTempo');
const metaKey = document.getElementById('metaKey');
const metaDuration = document.getElementById('metaDuration');

const sliders = [
  ['intensity', 'intensityOut'],
  ['room', 'roomOut'],
  ['warmth', 'warmthOut'],
  ['keepOriginal', 'originalOut'],
].map(([input, output]) => [document.getElementById(input), document.getElementById(output)]);

let selectedFile = null;
let originalUrl = null;
let resultUrl = null;
let progressTimer = null;

const sameOriginApi = window.location.origin || 'http://localhost:10000';
const isLocalFile = window.location.protocol === 'file:';
const savedApi = isLocalFile ? (localStorage.getItem('song2choir_api_url') || 'http://localhost:10000') : sameOriginApi;
apiUrlInput.value = savedApi;

function cleanBaseUrl(value) {
  return (value || '').trim().replace(/\/+$/, '');
}

function setStatus(type, title, text, progress) {
  statusBadge.className = 'badge';
  if (type === 'error') statusBadge.classList.add('error');
  if (type === 'work') statusBadge.classList.add('work');
  statusBadge.textContent = type === 'error' ? 'Error' : type === 'work' ? 'Rendering' : 'Ready';
  statusTitle.textContent = title;
  statusText.textContent = text;
  if (typeof progress === 'number') progressBar.style.width = `${Math.max(0, Math.min(100, progress))}%`;
}

function setDownload(blob, filename) {
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  resultUrl = URL.createObjectURL(blob);
  resultPlayer.src = resultUrl;
  downloadLink.href = resultUrl;
  downloadLink.download = filename;
  downloadLink.classList.remove('disabled');
}

function setMeta(response) {
  const style = response.headers.get('X-S2C-Style') || '—';
  const tempo = response.headers.get('X-S2C-Tempo') || '—';
  const key = response.headers.get('X-S2C-Key') || '—';
  const duration = response.headers.get('X-S2C-Duration') || '—';
  metaStyle.textContent = style;
  metaTempo.textContent = tempo && tempo !== '0' ? `${tempo} BPM` : 'Not detected';
  metaKey.textContent = key || '—';
  metaDuration.textContent = duration && duration !== '—' ? `${duration}s` : '—';
  metaPanel.hidden = false;
}

function fakeProgressStart() {
  clearInterval(progressTimer);
  let value = 8;
  progressBar.style.width = '8%';
  progressTimer = setInterval(() => {
    value += Math.max(0.4, (86 - value) * 0.08);
    progressBar.style.width = `${Math.min(86, value)}%`;
  }, 700);
}

function fakeProgressStop(success = true) {
  clearInterval(progressTimer);
  progressTimer = null;
  progressBar.style.width = success ? '100%' : '0%';
}

async function checkBackendHealth() {
  const base = cleanBaseUrl(apiUrlInput.value);
  if (!base) return;
  try {
    const res = await fetch(`${base}/api/health`, { method: 'GET' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    healthBadge.textContent = data.ok ? `Backend online · max ${data.max_upload_mb || '?'}MB` : 'Backend reachable';
    healthBadge.className = 'badge';
  } catch (error) {
    healthBadge.textContent = 'Backend not reachable';
    healthBadge.className = 'badge error';
  }
}

apiUrlInput.addEventListener('change', () => {
  const value = cleanBaseUrl(apiUrlInput.value);
  apiUrlInput.value = value;
  localStorage.setItem('song2choir_api_url', value);
  checkBackendHealth();
});
apiUrlInput.addEventListener('blur', checkBackendHealth);

sliders.forEach(([input, output]) => {
  input.addEventListener('input', () => {
    output.textContent = `${input.value}%`;
  });
});

audioFile.addEventListener('change', () => {
  selectedFile = audioFile.files?.[0] || null;
  if (!selectedFile) return;
  if (originalUrl) URL.revokeObjectURL(originalUrl);
  originalUrl = URL.createObjectURL(selectedFile);
  originalPlayer.src = originalUrl;
  uploadTitle.textContent = selectedFile.name;
  uploadMeta.textContent = `${(selectedFile.size / 1024 / 1024).toFixed(2)} MB · ${selectedFile.type || 'audio file'}`;
  setStatus('ready', 'Song loaded', 'Choose your choir settings, then render through Render.', 0);
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const base = cleanBaseUrl(apiUrlInput.value);
  if (!base) {
    setStatus('error', 'Render URL required', 'The app should auto-use this Render URL. Use http://localhost:10000 only for local testing.', 0);
    return;
  }
  if (!selectedFile) {
    setStatus('error', 'No audio selected', 'Upload an audio file first.', 0);
    return;
  }

  localStorage.setItem('song2choir_api_url', base);
  renderBtn.disabled = true;
  renderBtn.textContent = 'Rendering...';
  downloadLink.classList.add('disabled');
  metaPanel.hidden = true;
  setStatus('work', 'Rendering choir version', 'Render is analyzing and building the choir stack. Larger songs take longer.', 8);
  fakeProgressStart();

  const body = new FormData();
  body.append('file', selectedFile);
  body.append('style', document.getElementById('style').value);
  body.append('harmony', document.getElementById('harmony').value);
  body.append('intensity', String(Number(document.getElementById('intensity').value) / 100));
  body.append('room', String(Number(document.getElementById('room').value) / 100));
  body.append('warmth', String(Number(document.getElementById('warmth').value) / 100));
  body.append('keep_original', String(Number(document.getElementById('keepOriginal').value) / 100));

  try {
    const response = await fetch(`${base}/api/render`, { method: 'POST', body });
    if (!response.ok) {
      let detail = `Render failed with HTTP ${response.status}.`;
      try {
        const err = await response.json();
        detail = err.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const blob = await response.blob();
    const safeName = selectedFile.name.replace(/\.[^.]+$/, '').slice(0, 70) || 'song';
    setDownload(blob, `${safeName}-song2choir-pro.wav`);
    setMeta(response);
    fakeProgressStop(true);
    setStatus('ready', 'Choir render complete', 'Preview the result or download the WAV file.', 100);
  } catch (error) {
    fakeProgressStop(false);
    setStatus('error', 'Render failed', error.message || 'Something went wrong while rendering.', 0);
  } finally {
    renderBtn.disabled = false;
    renderBtn.textContent = 'Render Choir Version';
  }
});

resetBtn.addEventListener('click', () => {
  selectedFile = null;
  audioFile.value = '';
  if (originalUrl) URL.revokeObjectURL(originalUrl);
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  originalUrl = null;
  resultUrl = null;
  originalPlayer.removeAttribute('src');
  resultPlayer.removeAttribute('src');
  originalPlayer.load();
  resultPlayer.load();
  uploadTitle.textContent = 'Click to upload audio';
  uploadMeta.textContent = 'MP3, WAV, M4A, OGG, FLAC or WEBM. Max depends on backend settings.';
  downloadLink.classList.add('disabled');
  metaPanel.hidden = true;
  setStatus('ready', 'Upload a song to start', 'After rendering, your result will appear here with a download button.', 0);
});

checkBackendHealth();
