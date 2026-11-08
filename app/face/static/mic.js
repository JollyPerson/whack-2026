// /mic: a phone's browser as a debater's microphone (see app/face/phone_mic.py).
// getUserMedia -> AudioWorklet (mic-worklet.js: 16 kHz mono Int16, 80 ms frames) -> binary WebSocket frames to
// /mic/ws?ch=A|B -> the server plays them into the null sink the ears record. Reconnects with backoff; a refusal
// (wrong token, channel off, another phone already on it) stops and says why. The page sends a random session id
// (?sid=) so that, after a Wi-Fi blip, the server lets this page take its own channel back instead of refusing it as
// "another phone"; a link that stops draining for STALL_MS is closed and reconnected rather than left looking live.
(function () {
  'use strict';
  const Q = new URLSearchParams(location.search);
  const SIDES = {A: 'Proposition', B: 'Opposition'};
  const CH = (Q.get('ch') || '').trim().toUpperCase();
  const AGC = Q.get('agc') !== '0';
  const TOKEN = Q.get('token') || '';
  const FRAME_SAMPLES = 1280;                // 80 ms at 16 kHz
  const MAX_BUFFERED = 64000;                // ~2 s of audio queued on a stalled link: drop rather than lag
  const STALL_MS = 3000;                     // the queue stuck at MAX_BUFFERED this long = a dead link: reconnect
  const BUSY_RETRY_MS = 60000;               // "busy" within this long of losing a live link: retry (our old socket)
  const SID = makeSid();
  const FATAL = {
    4401: 'The server wants a token: open this page with ?token=<MIC_TOKEN> added to the address.',
    4404: 'This microphone is not switched on at the server: start it with PHONE_MICS=A,B.',
    4409: 'Another phone is already this microphone. Stop that one first, then press Start.',
  };
  const INSECURE = 'Not a secure context: the browser only lets a page use the microphone over HTTPS or on ' +
    'localhost. Open it as http://localhost:8000/mic?ch=' + (CH || 'A') + ' via adb reverse (adb reverse tcp:8000 ' +
    'tcp:8000), or over HTTPS on the laptop\'s second port: https://<laptop IP>:8443/mic (SSL_CERTFILE / ' +
    'SSL_KEYFILE on the server).';
  const $ = id => document.getElementById(id);

  let running = false, stream = null, ctx = null, src = null, node = null, ws = null, wake = null;
  let retry = 0, retryTimer = null, shown = 0, sentBytes = 0, dropped = 0, connectedOnce = false;
  let stalledSince = 0, lostAt = 0;          // lostAt: when a live link last dropped

  function makeSid() {
    const b = new Uint8Array(12);
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) crypto.getRandomValues(b);
    else for (let i = 0; i < b.length; i++) b[i] = Math.random() * 256;
    return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  }
  // Always this page's own host: the token and the audio never go anywhere else.
  function wsUrl() {
    const p = new URLSearchParams({ch: CH, sid: SID});
    if (TOKEN) p.set('token', TOKEN);
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/mic/ws?' + p.toString();
  }
  function backoffMs(n) { return Math.min(10000, 500 * Math.pow(2, n)); }

  function setState(cls, text) {
    $('state').className = 'state ' + cls;
    $('state-text').textContent = text;
  }
  function showError(msg) { $('error').textContent = msg; $('error').hidden = !msg; }
  function setButton() {
    $('toggle').textContent = running ? 'Stop' : 'Start';
    $('toggle').classList.toggle('on', running);
  }
  function setLevel(peak) {
    const db = peak > 0 ? 20 * Math.log10(peak) : -Infinity;
    const pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    const bar = $('level');
    bar.style.width = pct.toFixed(0) + '%';
    bar.className = peak >= 0.98 ? 'clip' : db > -6 ? 'hot' : '';
    $('level-text').textContent = !running ? 'mic off' : peak >= 0.98 ? 'CLIP: move the phone back'
      : isFinite(db) ? db.toFixed(0) + ' dBFS' : 'silence';
  }
  function setStats() {
    $('stats').textContent = running ? (sentBytes / 32000).toFixed(1) + ' s sent' + (dropped ? ' · ' + dropped +
      ' frames not sent while offline' : '') : '';
  }

  async function requestWake() {
    if (!('wakeLock' in navigator)) { $('wake-note').textContent = 'This browser cannot keep it on: set a long screen timeout.'; return; }
    try {
      wake = await navigator.wakeLock.request('screen');
      wake.addEventListener('release', () => { wake = null; });
      $('wake-note').textContent = 'The screen stays on while the mic is live.';
    } catch (e) {
      $('wake-note').textContent = 'Could not keep the screen on (' + (e && e.name || 'error') + '): set a long screen timeout.';
    }
  }

  function micError(e) {
    const n = e && e.name;
    if (n === 'NotAllowedError' || n === 'SecurityError' || n === 'PermissionDeniedError')
      return 'Microphone permission denied. Allow the microphone for this site (the icon left of the address, or ' +
        'Site settings > Microphone), then press Start again.';
    if (n === 'NotFoundError' || n === 'DevicesNotFoundError') return 'No microphone found on this phone.';
    if (n === 'NotReadableError' || n === 'TrackStartError')
      return 'The microphone is busy: another app (a call, a recorder, MicYou) is using it. Close it and press Start.';
    return 'Could not start the microphone: ' + ((e && (e.message || e.name)) || String(e));
  }

  function onFrame(e) {
    const peak = e.data.peak || 0;
    const now = Date.now();
    if (now - shown > 80) { shown = now; setLevel(peak); setStats(); }
    if (ws && ws.readyState === 1 && ws.bufferedAmount < MAX_BUFFERED && connectedOnce) {
      ws.send(e.data.pcm);
      sentBytes += e.data.pcm.byteLength;
      stalledSince = 0;
    } else {
      dropped++;
      if (ws && connectedOnce && ws.bufferedAmount >= MAX_BUFFERED) {
        if (!stalledSince) stalledSince = now;
        else if (now - stalledSince > STALL_MS) stalled();
      }
    }
  }

  // The link stopped draining (half-dead Wi-Fi): drop this socket and reconnect instead of looking live.
  function stalled() {
    const s = ws;
    ws = null; connectedOnce = false; stalledSince = 0; lostAt = Date.now();
    try { s.close(4000, 'stalled'); } catch (_) {}
    retryLater('Link stalled (nothing getting through); reconnecting in ');
  }

  function retryLater(prefix, refusal) {
    const ms = backoffMs(retry++);
    setState('retrying', prefix + Math.round(ms / 100) / 10 + ' s');
    if (retry >= 4) showError('Cannot reach the server' + (refusal ? ': ' + refusal : '') + '. Is it running, and is ' +
      (location.hostname === 'localhost' ? '"adb reverse tcp:8000 tcp:8000" still in place (replug = run it again)?'
        : 'the phone on the same network?'));
    retryTimer = setTimeout(connect, ms);
  }

  function connect() {
    clearTimeout(retryTimer); retryTimer = null;
    if (!running) return;
    setState('connecting', retry ? 'Reconnecting…' : 'Connecting to the laptop…');
    connectedOnce = false; stalledSince = 0;
    let sock;
    try {
      sock = new WebSocket(wsUrl());
    } catch (e) {
      return fail('Bad server address: ' + (e && e.message || e));
    }
    sock.binaryType = 'arraybuffer';
    let refusal = '';
    sock.onmessage = ev => {
      if (typeof ev.data !== 'string') return;
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m && m.ok) {
        retry = 0; connectedOnce = true; showError('');
        setState('live', 'Live: microphone ' + CH + ' (' + SIDES[CH] + ') is on the laptop');
      } else if (m && m.error) {
        refusal = String(m.error);
      }
    };
    sock.onclose = ev => {
      if (ws !== sock) return;                 // an old socket we already gave up on (stalled, stopped)
      ws = null;
      if (connectedOnce) lostAt = Date.now();
      connectedOnce = false;
      if (!running) return;
      // Busy right after we were live is most likely our own old socket the server hasn't noticed is dead (it
      // normally hands the channel back to the same page at once): keep retrying for a while before giving up.
      const busyAfterLive = ev.code === 4409 && lostAt && Date.now() - lostAt < BUSY_RETRY_MS;
      if (FATAL[ev.code] && !busyAfterLive) return fail(FATAL[ev.code] + (refusal ? ' (Server: ' + refusal + ')' : ''));
      retryLater(busyAfterLive ? 'The laptop still holds the old connection; retrying in '
        : 'Connection lost; retrying in ', refusal);
    };
    ws = sock;
  }

  function fail(msg) {
    stop();
    showError(msg);
    setState('error', 'Stopped');
  }

  async function start() {
    showError('');
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
      return fail(INSECURE);
    if (typeof AudioWorkletNode === 'undefined') return fail('This browser has no AudioWorklet: use a current Chrome, Firefox or Safari.');
    running = true; setButton(); $('toggle').disabled = true;
    setState('connecting', 'Asking for the microphone…');
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: AGC, channelCount: 1}, video: false});
    } catch (e) {
      $('toggle').disabled = false;
      return fail(micError(e));
    }
    try {
      ctx = new AudioContext();
      await ctx.audioWorklet.addModule('/static/mic-worklet.js');
      src = ctx.createMediaStreamSource(stream);
      node = new AudioWorkletNode(ctx, 'mic-downsampler', {processorOptions: {outRate: 16000, frameSamples: FRAME_SAMPLES}});
      node.port.onmessage = onFrame;
      src.connect(node);
      node.connect(ctx.destination);           // outputs silence; keeps the node pulled on every browser
      await ctx.resume();
    } catch (e) {
      $('toggle').disabled = false;
      return fail('Could not start audio processing: ' + ((e && (e.message || e.name)) || e));
    }
    $('toggle').disabled = false;
    if (!running) return stop();                // Stop pressed while the permission prompt was up
    stream.getAudioTracks().forEach(t => { t.onended = () => running && fail('The microphone stopped (another app took it?). Press Start again.'); });
    sentBytes = 0; dropped = 0; retry = 0; lostAt = 0;
    requestWake();
    connect();
  }

  function stop() {
    running = false;
    clearTimeout(retryTimer); retryTimer = null;
    if (ws) { const s = ws; ws = null; try { s.close(1000, 'stopped'); } catch (_) {} }
    try { if (src) src.disconnect(); if (node) { node.port.onmessage = null; node.disconnect(); } } catch (_) {}
    if (stream) stream.getTracks().forEach(t => { t.onended = null; t.stop(); });
    if (ctx) ctx.close().catch(() => {});
    if (wake) wake.release().catch(() => {});
    src = node = stream = ctx = wake = null;
    setButton(); setLevel(0); setStats();
    setState('', 'Stopped');
  }

  function init() {
    if (!SIDES[CH]) {
      $('picker').hidden = false;
      for (const c of ['A', 'B']) {
        const p = new URLSearchParams(Q); p.set('ch', c);
        $('pick-' + c).href = '?' + p.toString();
      }
      setState('', 'Pick a side');
      return;
    }
    document.body.classList.add(CH);
    document.title = 'Mic ' + CH + ' · ' + SIDES[CH];
    $('letter').textContent = CH;
    $('side-name').textContent = SIDES[CH];
    $('side-sub').textContent = 'Microphone ' + CH + (AGC ? '' : ' · auto gain off');
    for (const id of ['mic', 'toggle', 'meter', 'tips']) $(id).hidden = false;
    $('toggle').addEventListener('click', () => (running ? stop() : start()));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && running && !wake) requestWake();
    });
    if (!window.isSecureContext) { showError(INSECURE); setState('error', 'Cannot use the microphone here'); }
  }

  // For tests/js/mic_page.js: the pure parts.
  if (typeof window !== 'undefined') window.__mic = {wsUrl, backoffMs, micError, FATAL, INSECURE, SID};
  init();
})();
