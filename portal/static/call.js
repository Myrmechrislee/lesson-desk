(() => {
  'use strict';
  const root = document.querySelector('[data-call]');
  if (!root) return;
  const $ = selector => root.querySelector(selector), $$ = selector => [...root.querySelectorAll(selector)];
  const dialog = $('[data-call-dialog]'), videoSource = $('[data-call-video-source]'), audioSource = $('[data-call-audio-source]');
  const enabled = {audio: true, video: true}, peers = new Map(), receivedSignals = new Set();
  let stream = window.MediaStream ? new MediaStream() : null, sessionId = null, polling = false, joining = false, ready = false, closed = false;
  let audioContext, selfMeter, meterTimer, networkTimer, pollTimer, lastPoll = Date.now(), playbackUrl, recorder;
  let configurationVersion = 0, configurationPromise, stopped = false, silentSince = performance.now();

  function message(text) {
    $('[data-call-status]').textContent = text;
    const summary = $('[data-call-summary]'); summary.textContent = text; summary.hidden = !sessionId;
  }
  function warning(text) { const node = $('[data-call-network]'); node.textContent = text; node.hidden = !text; }
  async function api(url, payload) {
    const response = await fetch(url, {method: payload ? 'POST' : 'GET', cache: 'no-store', headers: payload ? {'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.token} : {}, body: payload ? JSON.stringify(payload) : undefined});
    if (!response.ok) {
      const html = new DOMParser().parseFromString(await response.text(), 'text/html');
      throw new Error(html.querySelector('.error-message, main p')?.textContent.trim() || `Call request failed (${response.status}).`);
    }
    return response.json();
  }
  function context() {
    if (!audioContext) audioContext = new (window.AudioContext || window.webkitAudioContext)();
    audioContext.resume().catch(() => {}); return audioContext;
  }
  async function devices() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const items = await navigator.mediaDevices.enumerateDevices();
    for (const [select, kind, label] of [[videoSource, 'videoinput', 'Camera'], [audioSource, 'audioinput', 'Microphone']]) {
      const selected = select.value; select.replaceChildren(new Option(`Default ${label.toLowerCase()}`, ''));
      for (const device of items.filter(item => item.kind === kind)) select.add(new Option(device.label || `${label} ${select.options.length}`, device.deviceId));
      if ([...select.options].some(option => option.value === selected)) select.value = selected;
    }
  }
  function audioMeter(media) {
    try {
      if (!media.getAudioTracks().length) return null;
      const ac = context(), analyser = ac.createAnalyser(); analyser.fftSize = 512;
      const node = ac.createMediaStreamSource(media); node.connect(analyser);
      return {node, analyser, data: new Float32Array(analyser.fftSize), lastSpoke: 0};
    } catch { return null; }
  }
  function level(meter) {
    if (!meter) return 0;
    meter.analyser.getFloatTimeDomainData(meter.data);
    return Math.sqrt(meter.data.reduce((sum, value) => sum + value * value, 0) / meter.data.length);
  }
  function tileState(tile, cameraOn, micOn, speaking) {
    tile.classList.toggle('is-audio-only', !cameraOn); tile.classList.toggle('is-muted', !micOn); tile.classList.toggle('is-speaking', micOn && speaking);
  }
  function syncControls() {
    for (const button of $$('[data-call-toggle]')) {
      const kind = button.dataset.callToggle, label = kind === 'audio' ? 'microphone' : 'camera';
      button.setAttribute('aria-pressed', String(enabled[kind])); button.setAttribute('aria-label', `Turn ${label} ${enabled[kind] ? 'off' : 'on'}`); button.title = button.getAttribute('aria-label');
    }
    tileState($('[data-call-self-tile]'), enabled.video && !!stream?.getVideoTracks().length, enabled.audio, false);
    $('[data-call-preview-video]').style.visibility = enabled.video ? '' : 'hidden';
    $('[data-call-record]').disabled = !enabled.audio || !stream?.getAudioTracks().length || !window.MediaRecorder || recorder?.state === 'recording';
  }
  function startMeter() {
    selfMeter?.node.disconnect(); selfMeter = audioMeter(stream); silentSince = performance.now();
    if (meterTimer) return;
    meterTimer = setInterval(() => {
      const time = performance.now(), ownLevel = enabled.audio ? level(selfMeter) : 0;
      if (ownLevel > .008) { silentSince = time; if (selfMeter) selfMeter.lastSpoke = time; }
      $('[data-call-meter]').value = Math.min(1, ownLevel * 5);
      $('[data-call-silence]').hidden = !enabled.audio || !ready || time - silentSince < 7000;
      tileState($('[data-call-self-tile]'), enabled.video && !!stream?.getVideoTracks().length, enabled.audio, !!selfMeter && time - selfMeter.lastSpoke < 350);
      for (const peer of peers.values()) {
        if (peer.person.microphone_on && level(peer.meter) > .008 && peer.meter) peer.meter.lastSpoke = time;
        const videoTrack = peer.stream.getVideoTracks()[0];
        tileState(peer.tile, peer.person.camera_on && !!videoTrack && !videoTrack.muted && videoTrack.readyState === 'live', peer.person.microphone_on, !!peer.meter && time - peer.meter.lastSpoke < 350);
      }
    }, 120);
  }
  function mediaState() { return {camera_on: enabled.video && !!stream?.getVideoTracks().length, microphone_on: enabled.audio && !!stream?.getAudioTracks().length}; }
  async function acquire(desired) {
    if (!navigator.mediaDevices?.getUserMedia && (desired.audio || desired.video)) throw new Error('Camera and microphone need HTTPS and a supported browser. Turn both devices off to join.');
    const constraints = {
      audio: desired.audio ? {deviceId: audioSource.value ? {exact: audioSource.value} : undefined, echoCancellation: true, noiseSuppression: true, autoGainControl: true} : false,
      video: desired.video ? {deviceId: videoSource.value ? {exact: videoSource.value} : undefined, width: {ideal: 640, max: 960}, height: {ideal: 360, max: 540}, frameRate: {ideal: 20, max: 24}} : false
    };
    return desired.audio || desired.video ? navigator.mediaDevices.getUserMedia(constraints) : new MediaStream();
  }
  function configure() {
    configurationVersion++; ready = false;
    if (configurationPromise) return configurationPromise;
    configurationPromise = (async () => {
      while (!stopped) {
        const version = configurationVersion, desired = {...enabled};
        message('Applying devices and testing your microphone…');
        try {
          const next = await acquire(desired);
          if (stopped || version !== configurationVersion) { next.getTracks().forEach(track => track.stop()); continue; }
          const previous = stream; stream = next;
          for (const video of $$('[data-call-self], [data-call-preview-video]')) { video.srcObject = stream; video.play().catch(() => {}); }
          try {
            for (const peer of peers.values()) for (const kind of ['audio', 'video']) await peer.senders[kind].replaceTrack(stream.getTracks().find(track => track.kind === kind) || null);
          } finally { previous?.getTracks().forEach(track => track.stop()); }
          if (stopped || version !== configurationVersion) continue;
          for (const track of stream.getTracks()) track.onended = () => {
            enabled[track.kind] = false; syncControls();
            message(`${track.kind === 'audio' ? 'Microphone' : 'Camera'} disconnected. Select a source or turn it on to reconnect.`); poll();
          };
          ready = true; syncControls(); startMeter();
          await devices().catch(() => {});
          if (sessionId) poll();
          message(sessionId ? 'You are in the lesson call.' : 'Preview ready. Check your microphone level, then join the call.');
          if (version === configurationVersion) return true;
        } catch (error) {
          if (version !== configurationVersion) continue;
          message(error.name === 'NotAllowedError' ? 'Device permission was denied. Allow access, or turn camera and microphone off to join.' : error.message);
          return false;
        }
      }
      return false;
    })().finally(() => { configurationPromise = null; });
    return configurationPromise;
  }
  for (const button of $$('[data-call-toggle]')) button.addEventListener('click', () => {
    const kind = button.dataset.callToggle; enabled[kind] = !enabled[kind];
    // Disable the outgoing track immediately, including while a replacement is pending.
    for (const track of stream?.getTracks().filter(track => track.kind === kind) || []) { track.enabled = enabled[kind]; if (!enabled[kind]) track.stop(); }
    syncControls(); stopped = false; configure();
  });
  for (const control of [videoSource, audioSource]) control.addEventListener('change', () => { stopped = false; configure(); });
  for (const button of $$('[data-call-open]')) button.addEventListener('click', () => {
    dialog.showModal(); try { context(); } catch { /* Devices still work without metering. */ }
    if (!ready) { stopped = false; configure(); }
  });
  for (const button of $$('[data-call-close]')) button.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (!sessionId && !joining) stopPreview(); });
  $('[data-call-test-audio]').addEventListener('click', () => {
    try { const ac = context(), osc = ac.createOscillator(), gain = ac.createGain(); gain.gain.value = .12; osc.frequency.value = 440; osc.connect(gain).connect(ac.destination); osc.start(); osc.stop(ac.currentTime + .6); }
    catch { message('Speaker test unavailable in this browser.'); }
  });
  $('[data-call-record]').addEventListener('click', () => {
    const track = stream.getAudioTracks()[0]; if (!track || !enabled.audio || recorder?.state === 'recording') return;
    const button = $('[data-call-record]'), chunks = []; recorder = new MediaRecorder(new MediaStream([track])); button.disabled = true; button.textContent = 'Recording 3 seconds…';
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = () => {
      if (playbackUrl) URL.revokeObjectURL(playbackUrl);
      playbackUrl = URL.createObjectURL(new Blob(chunks, {type: recorder.mimeType}));
      const audio = $('[data-call-playback]'); audio.src = playbackUrl; audio.hidden = false; button.textContent = 'Record microphone test'; syncControls();
    };
    recorder.start(); setTimeout(() => { if (recorder.state !== 'inactive') recorder.stop(); }, 3000);
  });
  async function signal(target, kind, data) { if (sessionId) await api(root.dataset.signals, {id: sessionId, target, kind, data}); }
  async function tune(sender, kind, poor = false) {
    try {
      const parameters = sender.getParameters(); if (!parameters.encodings?.length) return;
      parameters.encodings[0].maxBitrate = kind === 'audio' ? 48000 : poor ? 100000 : 450000;
      if (kind === 'video') { parameters.encodings[0].maxFramerate = poor ? 10 : 20; parameters.encodings[0].scaleResolutionDownBy = poor ? 2 : 1; parameters.degradationPreference = 'maintain-framerate'; }
      await sender.setParameters(parameters);
    } catch { /* Browser-specific encoding controls are optional. */ }
  }
  function removePeer(id, peer) { peer.pc.close(); peer.meter?.node.disconnect(); peer.tile.remove(); peers.delete(id); }
  function peerFor(person) {
    const previous = peers.get(person.id); if (previous) { previous.person = person; return previous; }
    const pc = new RTCPeerConnection({iceServers: JSON.parse(root.dataset.ice), bundlePolicy: 'max-bundle'}), remoteStream = new MediaStream();
    const tile = document.createElement('figure'); tile.className = 'call-tile is-audio-only';
    const video = document.createElement('video'); video.autoplay = true; video.playsInline = true; video.srcObject = remoteStream;
    const avatar = $('[data-call-self-tile] .call-audio-avatar').cloneNode(true), caption = document.createElement('figcaption');
    caption.textContent = `${person.name} · ${person.role}`; tile.append(video, avatar, caption); $('[data-call-videos]').append(tile);
    const peer = {pc, tile, video, stream: remoteStream, meter: null, senders: {}, makingOffer: false, ignoreOffer: false, settingAnswer: false, candidates: [], polite: sessionId > person.id, person}; peers.set(person.id, peer);
    for (const kind of ['audio', 'video']) {
      const track = stream.getTracks().find(item => item.kind === kind), transceiver = pc.addTransceiver(track || kind, {direction: 'sendrecv', sendEncodings: [{maxBitrate: kind === 'audio' ? 48000 : 450000, priority: kind === 'audio' ? 'high' : 'low'}]});
      peer.senders[kind] = transceiver.sender;
      if (transceiver.setCodecPreferences) {
        const codecs = RTCRtpReceiver.getCapabilities(kind)?.codecs || [], preferred = kind === 'audio' ? 'audio/opus' : 'video/VP8';
        try { transceiver.setCodecPreferences([...codecs.filter(c => c.mimeType.toLowerCase() === preferred.toLowerCase()), ...codecs.filter(c => c.mimeType.toLowerCase() !== preferred.toLowerCase())]); } catch { /* Codec preferences are optional. */ }
      }
    }
    pc.ontrack = event => {
      remoteStream.addTrack(event.track);
      if (event.track.kind === 'audio') { peer.meter?.node.disconnect(); peer.meter = audioMeter(remoteStream); }
      video.play().catch(() => message('Tap a participant tile to enable their audio.'));
    };
    tile.addEventListener('click', () => { try { context(); } catch {} video.play().catch(() => {}); });
    pc.onicecandidate = event => { if (event.candidate) signal(person.id, 'candidate', event.candidate.toJSON()).catch(() => warning('Connectivity interrupted. Reconnecting…')); };
    pc.onnegotiationneeded = async () => {
      try { peer.makingOffer = true; await pc.setLocalDescription(); await signal(person.id, 'description', pc.localDescription.toJSON()); }
      catch { warning('Call connection interrupted. Reconnecting…'); } finally { peer.makingOffer = false; }
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected') { for (const kind of ['audio', 'video']) tune(peer.senders[kind], kind); message('Connected to the lesson call.'); }
      if (['disconnected', 'failed'].includes(pc.connectionState)) { warning('A participant connection is interrupted. Reconnecting…'); if (pc.connectionState === 'failed') pc.restartIce(); }
    };
    return peer;
  }
  async function receive(item, roster) {
    const person = roster.find(row => row.id === item.sender); if (!person) return;
    const peer = peerFor(person), pc = peer.pc;
    if (item.kind === 'description') {
      const description = item.data, readyForOffer = !peer.makingOffer && (pc.signalingState === 'stable' || peer.settingAnswer), collision = description.type === 'offer' && !readyForOffer;
      peer.ignoreOffer = !peer.polite && collision; if (peer.ignoreOffer) return;
      peer.settingAnswer = description.type === 'answer'; await pc.setRemoteDescription(description); peer.settingAnswer = false;
      for (const candidate of peer.candidates.splice(0)) await pc.addIceCandidate(candidate);
      if (description.type === 'offer') { await pc.setLocalDescription(); await signal(item.sender, 'description', pc.localDescription.toJSON()); }
    } else if (!peer.ignoreOffer) { if (pc.remoteDescription) await pc.addIceCandidate(item.data); else peer.candidates.push(item.data); }
  }
  async function poll() {
    if (!sessionId || polling || closed) return;
    polling = true; const currentSession = sessionId;
    try {
      const heartbeat = await api(root.dataset.session, {action: 'heartbeat', id: currentSession, ...mediaState()});
      if (closed || sessionId !== currentSession) return;
      const roster = heartbeat.participants;
      for (const person of roster) if (person.id !== sessionId) peerFor(person);
      for (const [id, peer] of peers) if (!roster.some(person => person.id === id)) removePeer(id, peer);
      const data = await api(`${root.dataset.signals}?id=${currentSession}`); if (closed || sessionId !== currentSession) return;
      for (const item of data.signals) {
        if (receivedSignals.has(item.id)) continue;
        try { await receive(item, roster); } catch { warning('A participant is reconnecting. If audio does not return, leave and rejoin.'); }
        receivedSignals.add(item.id);
      }
      if (data.signals.length) await api(`${root.dataset.signals}/ack`, {id: currentSession, messages: data.signals.map(item => item.id)});
      if (receivedSignals.size > 1000) { const retained = [...receivedSignals].slice(-200); receivedSignals.clear(); for (const id of retained) receivedSignals.add(id); }
      lastPoll = Date.now(); if (peers.size === 0) message('You are in the call. Waiting for others to join…');
    } catch (error) { if (!closed && sessionId === currentSession) warning(`${error.message} If the connection does not return, leave and rejoin.`); }
    finally { polling = false; }
  }
  async function quality() {
    if (!sessionId) return;
    if (!navigator.onLine) { warning('You are offline. Call connectivity is interrupted.'); return; }
    let poor = Date.now() - lastPoll > 6000;
    const connection = navigator.connection;
    if (connection && (connection.saveData || connection.downlink < .5 || ['slow-2g', '2g'].includes(connection.effectiveType))) poor = true;
    for (const peer of peers.values()) {
      try {
        const stats = await peer.pc.getStats();
        for (const report of stats.values()) {
          if (report.type === 'candidate-pair' && report.state === 'succeeded' && (report.currentRoundTripTime > .8 || report.availableOutgoingBitrate > 0 && report.availableOutgoingBitrate < 180000)) poor = true;
          if (report.type === 'inbound-rtp' && !report.isRemote) {
            const previous = peer.lastStats?.get(report.id);
            if (previous) { const lost = Math.max(0, (report.packetsLost || 0) - previous.lost), received = Math.max(0, (report.packetsReceived || 0) - previous.received); if (lost + received > 0 && lost / (lost + received) > .08) poor = true; }
            if (!peer.lastStats) peer.lastStats = new Map(); peer.lastStats.set(report.id, {lost: report.packetsLost || 0, received: report.packetsReceived || 0});
          }
        }
      } catch { /* Stats may be unavailable while reconnecting. */ }
      if (['disconnected', 'failed'].includes(peer.pc.connectionState)) poor = true;
    }
    warning(poor ? 'Connection is slow or interrupted. Video quality is reduced to preserve audio.' : '');
    for (const peer of peers.values()) if (peer.poor !== poor) { peer.poor = poor; await tune(peer.senders.video, 'video', poor); }
  }
  function joinedUI(joined) {
    $('[data-call-videos]').hidden = !joined; $('[data-call-active-controls]').hidden = !joined;
    $('[data-call-open]').hidden = joined; $('[data-call-join]').hidden = joined;
    $('[data-call-summary]').hidden = !joined;
  }
  $('[data-call-join]').addEventListener('click', async () => {
    if (sessionId || joining) return;
    joining = true; $('[data-call-join]').disabled = true;
    try {
      try { context(); } catch {} stopped = false;
      if (!ready && !await (configurationPromise || configure())) return;
      const data = await api(root.dataset.session, {action: 'join', ...mediaState()});
      sessionId = data.id; closed = false; receivedSignals.clear(); joinedUI(true); dialog.close();
      await poll(); pollTimer = setInterval(poll, 1200); networkTimer = setInterval(quality, 4000);
    } catch (error) { message(error.message); } finally { joining = false; $('[data-call-join]').disabled = false; }
  });
  function stopPreview() {
    stopped = true; configurationVersion++; ready = false;
    if (recorder?.state === 'recording') recorder.stop();
    stream?.getTracks().forEach(track => track.stop()); stream = window.MediaStream ? new MediaStream() : null;
    selfMeter?.node.disconnect(); selfMeter = null; clearInterval(meterTimer); meterTimer = null;
    $('[data-call-meter]').value = 0; $('[data-call-silence]').hidden = true; syncControls();
  }
  function leave(unloading = false) {
    if (sessionId) {
      const payload = {action: 'leave', id: sessionId};
      if (unloading) fetch(root.dataset.session, {method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.token}, body: JSON.stringify(payload)}).catch(() => {});
      else api(root.dataset.session, payload).catch(() => {});
    }
    sessionId = null; closed = true; clearInterval(pollTimer); clearInterval(networkTimer);
    for (const [id, peer] of peers) removePeer(id, peer);
    stopPreview(); joinedUI(false); warning(''); message('You left the call. Open call settings to join again.');
  }
  $('[data-call-leave]').addEventListener('click', () => leave()); window.addEventListener('pagehide', () => { leave(true); if (playbackUrl) URL.revokeObjectURL(playbackUrl); audioContext?.close().catch(() => {}); });
  window.addEventListener('offline', () => { if (sessionId) warning('You are offline. Call connectivity is interrupted.'); });
  window.addEventListener('online', () => { if (sessionId) { warning('Connection restored. Reconnecting…'); poll(); } });
  navigator.mediaDevices?.addEventListener('devicechange', () => devices().then(() => { if (!stopped) configure(); }).catch(() => {}));
  syncControls();
  if (!window.RTCPeerConnection || !window.MediaStream) {
    $('[data-call-join]').disabled = true; message('This browser does not support video calling. Use a current browser.');
  } else {
    configure();
    if (new URLSearchParams(location.search).get('call') === '1') dialog.showModal();
  }
})();
