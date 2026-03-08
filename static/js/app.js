/* ============================================================
   Nurse Call System — Frontend Application
   WebSocket-driven real-time dashboard
   ============================================================ */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  calls: {},        // call_id -> call object (active + acknowledged)
  devices: [],
  users: [],
  apartments: [],
  areas: [],
  relayConfigs: [],
  inputConfigs: [],
  ws: null,
  wsRetryTimer: null,
  currentPage: 'dashboard',
  currentSettingsTab: 'smtp',
  soundEnabled: true,
  // Monitor
  monLines: [],           // all received raw lines
  monSeenDevices: {},     // device_id -> { count, isCall, isKnown }
  monStats: { total: 0, calls: 0, devices: 0, unknown: 0 },
  monSelectedIdx: null,
  // Learn mode
  learnActive: false,
  learnTarget: null,   // 'device' | 'repeater'
  learnAbortCtrl: null,
  learnTimer: null,
  // Auth
  token: localStorage.getItem('cc_token') || null,
  currentUser: null,
  authRequired: false,
};

const API = '';          // same-origin API calls
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

function connectWS() {
  clearTimeout(state.wsRetryTimer);
  const ws = new WebSocket(WS_URL);
  state.ws = ws;

  ws.onopen = () => {
    setWsStatus('connected');
    // Start ping loop
    ws._pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 20000);
  };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    handleWsMessage(msg);
  };

  ws.onclose = () => {
    clearInterval(ws._pingInterval);
    setWsStatus('disconnected');
    state.wsRetryTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    setWsStatus('error');
    ws.close();
  };
}

function handleWsMessage(msg) {
  const { event, data } = msg;
  switch (event) {
    case 'init':
      // Bootstrap active/acknowledged calls
      (data.calls || []).forEach(c => state.calls[c.id] = c);
      renderActiveCalls();
      updateCounters();
      break;
    case 'call.new':
      state.calls[data.id] = data;
      addOrUpdateCallCard(data);
      updateCounters();
      playAlert(data.priority);
      _startAlarmRepeat(data.priority);
      toast(`New call: ${data.device_name} — ${data.location}`, 'warn');
      _showOsNotification(data);
      if (state.currentPage !== 'dashboard') showPage('dashboard');
      break;
    case 'call.updated':
      state.calls[data.id] = data;
      addOrUpdateCallCard(data);
      updateCounters();
      break;
    case 'call.cleared':
      delete state.calls[data.id];
      removeCallCard(data.id);
      updateCounters();
      toast(`Call cleared: ${data.device_name}`, 'success');
      // Dismiss emergency banner when the corresponding staff emergency call clears
      if (data.device_id && data.device_id.startsWith('staff_emergency:')) {
        dismissEmergencyBanner();
      }
      // Stop the repeat for this priority if no active calls remain at that level
      if (!Object.values(state.calls).some(c => c.priority === data.priority)) {
        _stopAlarmRepeat(data.priority);
      }
      break;
    case 'coordinator.status':
      updateCoordinatorStatus(data.status);
      break;
    case 'coordinator.raw':
      handleMonitorLine(data);
      break;
    case 'coordinator.device_seen':
      handleDeviceSeen(data.device_id, data.raw);
      break;
    case 'coordinator.repeater_seen':
      handleRepeaterSeen(data.serial_number);
      break;
    case 'roam_alert.event':
      handleRaWsEvent(data);
      break;
    case 'aeroscout.status':
      updateAleStatus(data.status);
      break;
    case 'aeroscout.raw':
      appendAleMonitorRow(data);
      break;
    case 'aeroscout.device':
      upsertAleControllerCard(data);
      break;
    case 'aeroscout.tag':
      upsertAleTagRow(data);
      break;
    case 'staff.message':
      _appendChatMessage(data);
      break;
    case 'staff.emergency':
      _appendChatMessage(data, true);
      _showEmergencyBanner(data);
      _playBeeps(3);
      break;
    case 'pong':
      break;
  }
}

function setWsStatus(status) {
  const dot   = document.getElementById('wsDot');
  const label = document.getElementById('wsLabel');
  dot.className = `ws-dot ${status}`;
  label.textContent = { connected: 'Connected', disconnected: 'Reconnecting…', error: 'Error' }[status] || status;
}

// ---------------------------------------------------------------------------
// Page navigation
// ---------------------------------------------------------------------------

// Pages each role may access.  If auth is off / user unknown, all pages are allowed.
const ROLE_PAGES = {
  admin:  ['dashboard', 'history', 'devices', 'apartments', 'areas', 'roam-alert', 'messages', 'settings'],
  staff:  ['dashboard', 'history', 'devices', 'apartments', 'areas', 'roam-alert', 'messages'],
  viewer: ['dashboard', 'history', 'messages'],
};

function _applyRoleUI(role) {
  const allowed = new Set(ROLE_PAGES[role] || ROLE_PAGES.staff);

  // Show/hide sidebar nav links by role
  document.querySelectorAll('.nav-link[data-page]').forEach(link => {
    link.style.display = allowed.has(link.dataset.page) ? '' : 'none';
  });

  // Staff Emergency button — visible to all roles (viewers are front-line staff)
  const emergBtn = document.getElementById('staffEmergencyBtn');
  if (emergBtn) emergBtn.classList.remove('hidden');
}

function showPage(page) {
  // Role guard — redirect to dashboard if this role can't access the page
  if (state.currentUser) {
    const allowed = ROLE_PAGES[state.currentUser.role] || ROLE_PAGES.staff;
    if (!allowed.includes(page)) page = 'dashboard';
  }

  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));

  document.getElementById(`page-${page}`)?.classList.remove('hidden');
  document.querySelector(`.nav-link[data-page="${page}"]`)?.classList.add('active');
  document.getElementById('pageTitle').textContent =
    { dashboard: 'Dashboard', history: 'Call History', devices: 'Devices',
      apartments: 'Apartments', areas: 'Areas', monitor: 'Innovonics Coordinator',
      settings: 'Settings', 'roam-alert': 'Wander Management',
      messages: 'Messages' }[page] || page;

  state.currentPage = page;

  if (page === 'history')    loadHistory();
  if (page === 'devices')    loadDevices();
  if (page === 'settings')   loadSettingsPage();
  if (page === 'apartments') loadApartments();
  if (page === 'areas')      loadAreas();
  if (page === 'roam-alert') loadRoamAlertPage();
  if (page === 'messages')   _scrollChatToBottom();
}

// ---------------------------------------------------------------------------
// Active calls rendering
// ---------------------------------------------------------------------------

function renderActiveCalls() {
  const grid = document.getElementById('activeCallsGrid');
  grid.innerHTML = '';
  const calls = Object.values(state.calls).sort((a, b) => {
    const p = { emergency: 0, urgent: 1, normal: 2 };
    return (p[a.priority] ?? 3) - (p[b.priority] ?? 3) || new Date(a.timestamp) - new Date(b.timestamp);
  });
  if (calls.length === 0) {
    grid.innerHTML = `<div class="empty-state" id="noActiveCalls">
      <div class="empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.61 3.37a2 2 0 0 1 1.95-2.18h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg></div>
      <p>No active calls</p><span>System monitoring — all clear</span>
    </div>`;
    return;
  }
  calls.forEach(c => grid.appendChild(buildCallCard(c)));
}

function buildCallCard(call) {
  const el = document.createElement('div');
  el.id = `call-${call.id}`;
  el.className = `call-card priority-${call.priority} status-${call.status}`;

  const isAcked = call.status === 'acknowledged';
  const elapsed = elapsedStr(call.timestamp);
  const ackedBy = call.acknowledged_by ? ` · Acked by ${call.acknowledged_by}` : '';

  el.innerHTML = `
    <div class="call-header">
      <div class="call-room">${esc(call.location || 'Unknown Room')}</div>
      <span class="priority-badge">${esc(call.priority)}</span>
    </div>
    <div class="call-body">
      <div class="call-device">${esc(call.device_name || call.device_id)}</div>
      <div class="call-meta">ID: ${esc(call.device_id)}</div>
      <div class="call-meta">Called: ${fmtTime(call.timestamp)}${ackedBy}</div>
      <span class="call-timer" id="timer-${call.id}">${elapsed}</span>
    </div>
    <div class="call-footer">
      ${isAcked
        ? `<button class="btn-clear" onclick="openClearModal(${call.id})">Clear Call</button>`
        : `<button class="btn-warn" onclick="openAckModal(${call.id})">Acknowledge</button>
           <button class="btn-clear" onclick="openClearModal(${call.id})">Clear</button>`
      }
    </div>`;
  return el;
}

function addOrUpdateCallCard(call) {
  const existing = document.getElementById(`call-${call.id}`);
  const grid = document.getElementById('activeCallsGrid');
  const emptyState = document.getElementById('noActiveCalls');
  if (emptyState) emptyState.remove();

  const card = buildCallCard(call);
  if (existing) {
    existing.replaceWith(card);
  } else {
    grid.insertBefore(card, grid.firstChild);
  }
}

function removeCallCard(callId) {
  document.getElementById(`call-${callId}`)?.remove();
  if (!document.querySelector('.call-card')) {
    renderActiveCalls();  // shows empty state
  }
}

// Live elapsed timers
setInterval(() => {
  Object.values(state.calls).forEach(c => {
    const el = document.getElementById(`timer-${c.id}`);
    if (el) el.textContent = elapsedStr(c.timestamp);
  });
}, 5000);

// ---------------------------------------------------------------------------
// Counters
// ---------------------------------------------------------------------------

function updateCounters() {
  const active = Object.values(state.calls).filter(c => c.status === 'active').length;
  const acked  = Object.values(state.calls).filter(c => c.status === 'acknowledged').length;
  document.getElementById('activeCount').textContent = active;
  document.getElementById('ackCount').textContent = acked;
  document.getElementById('activeCallsBadge').textContent = active + acked;
  document.title = active > 0 ? `(${active}) Nurse Call System` : 'Nurse Call System';
}

// ---------------------------------------------------------------------------
// Call history
// ---------------------------------------------------------------------------

async function loadHistory() {
  const filter = document.getElementById('historyFilter')?.value || 'all';
  const rows = await api(`/api/calls/?status=${filter}&limit=200`);
  const tbody = document.getElementById('historyBody');
  tbody.innerHTML = '';
  (rows || []).forEach(c => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">#${c.id}</td>
      <td>${esc(c.device_name || c.device_id)}</td>
      <td>${esc(c.location || '')}</td>
      <td><span class="tag tag-${c.priority}">${esc(c.priority)}</span></td>
      <td>${fmtDateTime(c.timestamp)}</td>
      <td>${c.acknowledged_at ? fmtDateTime(c.acknowledged_at) + '<br><small>' + esc(c.acknowledged_by || '') + '</small>' : '—'}</td>
      <td>${c.cleared_at ? fmtDateTime(c.cleared_at) : '—'}</td>
      <td><span class="tag tag-${c.status}">${c.status}</span></td>`;
    tbody.appendChild(tr);
  });
}

// ---------------------------------------------------------------------------
// Devices
// ---------------------------------------------------------------------------

const VENDOR_LABELS = { innovonics: 'Innovonics', arial_legacy: 'Arial Legacy', arial_900: 'Arial 900', universal_tx: 'Universal TX / Reed Switch' };

async function loadDevices() {
  [state.devices, state.relayConfigs, state.apartments, state.areas] = await Promise.all([
    api('/api/devices/').then(r => r || []),
    api('/api/settings/relays').then(r => r || []),
    api('/api/apartments/').then(r => r || []),
    api('/api/areas/').then(r => r || []),
  ]);
  const tbody = document.getElementById('devicesBody');
  tbody.innerHTML = '';
  state.devices.forEach(d => {
    const vendor = VENDOR_LABELS[d.vendor_type] || d.vendor_type || 'Innovonics';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">${esc(d.device_id)}</td>
      <td>${esc(d.name)}</td>
      <td>${esc((d.apartment_id ? (state.apartments.find(a => a.id === d.apartment_id)?.name || d.location) : d.location) || '')}</td>
      <td>${esc(d.device_type)}</td>
      <td>${esc(vendor)}</td>
      <td><span class="tag tag-${d.priority}">${esc(d.priority)}</span></td>
      <td>${d.last_seen ? fmtTime(d.last_seen) : 'Never'}</td>
      <td>${d.active ? '<span class="tag tag-cleared">Yes</span>' : '<span class="tag tag-acknowledged">No</span>'}</td>
      <td style="white-space:nowrap">
        <button class="btn-secondary btn-sm" onclick="openEditDeviceModal('${esc(d.device_id)}')">Edit</button>
        <button class="btn-secondary btn-sm" onclick="deleteDevice('${esc(d.device_id)}')">Remove</button>
      </td>`;
    tbody.appendChild(tr);
  });
}

function _populateDeviceModalDropdowns(selectedAptId, selectedRelayId, selectedAreaId) {
  // Apartment datalist
  const dl = document.getElementById('devApartmentList');
  if (dl) {
    dl.innerHTML = state.apartments.map(a => `<option value="${esc(a.name)}"></option>`).join('');
  }
  const aptInput = document.getElementById('devApartmentName');
  if (aptInput) {
    const apt = state.apartments.find(a => a.id === selectedAptId);
    aptInput.value = apt ? apt.name : '';
  }

  // Area datalist
  const adl = document.getElementById('devAreaList');
  if (adl) {
    adl.innerHTML = state.areas.map(a => `<option value="${esc(a.name)}"></option>`).join('');
  }
  const areaInput = document.getElementById('devAreaName');
  if (areaInput) {
    const area = state.areas.find(a => a.id === selectedAreaId);
    areaInput.value = area ? area.name : '';
  }

  const relaySel = document.getElementById('devRelayConfigId');
  relaySel.innerHTML = '<option value="">— None —</option>' +
    state.relayConfigs.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
  relaySel.value = selectedRelayId || '';
}

function openDeviceModal() {
  document.getElementById('deviceModalTitle').textContent = 'Register Device';
  document.getElementById('devSubmitBtn').textContent = 'Register';
  document.getElementById('devEditId').value = '';
  document.getElementById('devId').value = '';
  document.getElementById('devId').readOnly = false;
  document.getElementById('btnDetect').style.display = '';
  document.getElementById('learnStatus').classList.add('hidden');
  document.getElementById('devName').value = '';
  document.getElementById('devType').value = 'pendant';
  document.getElementById('devVendorType').value = 'innovonics';
  document.getElementById('devPriority').value = 'normal';
  document.getElementById('devAuxLabel').value = '';
  document.getElementById('auxLabelRow').style.display = 'none';
  _populateDeviceModalDropdowns(null, null, null);
  openModal('deviceModal');
}

function openEditDeviceModal(deviceId) {
  const d = state.devices.find(x => x.device_id === deviceId);
  if (!d) return;
  document.getElementById('deviceModalTitle').textContent = 'Edit Device';
  document.getElementById('devSubmitBtn').textContent = 'Save Changes';
  document.getElementById('devEditId').value = d.device_id;
  document.getElementById('devId').value = d.device_id;
  document.getElementById('devId').readOnly = true;
  document.getElementById('btnDetect').style.display = 'none';
  document.getElementById('learnStatus').classList.add('hidden');
  document.getElementById('devName').value = d.name || '';
  document.getElementById('devType').value = d.device_type || 'pendant';
  document.getElementById('devVendorType').value = d.vendor_type || 'innovonics';
  document.getElementById('devPriority').value = d.priority || 'normal';
  document.getElementById('devAuxLabel').value = d.aux_label || '';
  document.getElementById('auxLabelRow').style.display = d.vendor_type === 'arial_900' ? '' : 'none';
  _populateDeviceModalDropdowns(d.apartment_id, d.relay_config_id, d.area_id);
  // Fall back to stored location if no apartment is matched
  const aptInput = document.getElementById('devApartmentName');
  if (aptInput && !aptInput.value && d.location) aptInput.value = d.location;
  openModal('deviceModal');
}

async function _resolveApartmentId(aptName) {
  if (!aptName) return null;
  const match = state.apartments.find(a => a.name.toLowerCase() === aptName.toLowerCase());
  if (match) return match.id;
  const created = await api('/api/apartments/', 'POST', { name: aptName, relay_config_id: null });
  if (created?.id) {
    state.apartments.push(created);
    toast(`Apartment "${aptName}" created`, 'success');
    return created.id;
  }
  return null;
}

async function _resolveAreaId(areaName) {
  if (!areaName) return null;
  const match = state.areas.find(a => a.name.toLowerCase() === areaName.toLowerCase());
  if (match) return match.id;
  const created = await api('/api/areas/', 'POST', { name: areaName, relay_config_id: null });
  if (created?.id) {
    state.areas.push(created);
    toast(`Area "${areaName}" created`, 'success');
    return created.id;
  }
  return null;
}

async function submitDevice() {
  const editId = document.getElementById('devEditId').value;
  const isEdit = !!editId;
  const aptName  = (document.getElementById('devApartmentName')?.value || '').trim();
  const areaName = (document.getElementById('devAreaName')?.value || '').trim();
  const relayId  = document.getElementById('devRelayConfigId').value;

  const [aptId, areaId] = await Promise.all([
    _resolveApartmentId(aptName),
    _resolveAreaId(areaName),
  ]);

  const vendorType = document.getElementById('devVendorType').value;
  const body = {
    name:            document.getElementById('devName').value.trim(),
    location:        aptName,
    device_type:     document.getElementById('devType').value,
    vendor_type:     vendorType,
    priority:        document.getElementById('devPriority').value,
    apartment_id:    aptId   || null,
    area_id:         areaId  || null,
    relay_config_id: relayId ? +relayId : null,
    aux_label:       vendorType === 'arial_900' ? (document.getElementById('devAuxLabel').value.trim() || null) : null,
  };
  if (!body.name) { toast('Name is required', 'error'); return; }
  if (isEdit) {
    const res = await api(`/api/devices/${encodeURIComponent(editId)}`, 'PATCH', body);
    if (res?.device_id) { closeModal('deviceModal'); toast('Device updated', 'success'); loadDevices(); }
  } else {
    const deviceId = document.getElementById('devId').value.trim();
    if (!deviceId) { toast('Device ID is required', 'error'); return; }
    const res = await api('/api/devices/', 'POST', { device_id: deviceId, ...body });
    if (res?.id) { closeModal('deviceModal'); toast('Device registered', 'success'); loadDevices(); }
  }
}

async function deleteDevice(deviceId) {
  if (!confirm(`Remove device ${deviceId}?`)) return;
  await api(`/api/devices/${encodeURIComponent(deviceId)}`, 'DELETE');
  toast('Device removed', 'success');
  loadDevices();
}

// ---------------------------------------------------------------------------
// Apartments
// ---------------------------------------------------------------------------

async function loadApartments() {
  [state.apartments, state.relayConfigs, state.devices, state.areas] = await Promise.all([
    api('/api/apartments/').then(r => r || []),
    api('/api/settings/relays').then(r => r || []),
    api('/api/devices/').then(r => r || []),
    api('/api/areas/').then(r => r || []),
  ]);
  renderApartments(state.apartments);
}

function renderApartments(apts) {
  const el = document.getElementById('apartmentsList');
  if (!el) return;
  if (!apts.length) {
    el.innerHTML = '<p class="hint">No apartments configured yet.</p>';
    return;
  }
  el.innerHTML = '';
  const relayName = id => (state.relayConfigs.find(r => r.id === id) || {}).name || '—';
  const areaName  = id => (state.areas.find(a => a.id === id) || {}).name || '—';
  apts.forEach(a => {
    const aptDevices = state.devices.filter(d => d.apartment_id === a.id);
    const devHtml = aptDevices.length
      ? aptDevices.map(d => `<span class="seen-device-chip is-known" title="${esc(d.device_id)}">${esc(d.name)}</span>`).join('')
      : '<span class="hint" style="font-size:0.82rem">No devices assigned</span>';

    const areaSub = a.area_id ? ` · Area: ${esc(areaName(a.area_id))}` : '';
    const card = document.createElement('div');
    card.className = 'config-card';
    card.style.flexDirection = 'column';
    card.style.alignItems = 'stretch';
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div class="config-card-info">
          <div class="config-card-name">${esc(a.name)}</div>
          <div class="config-card-sub">Dome relay: ${esc(relayName(a.relay_config_id))}${areaSub}</div>
        </div>
        <div class="config-card-actions">
          <button type="button" class="btn-secondary btn-sm" onclick="openApartmentModal(${a.id})">Edit</button>
          <button type="button" class="btn-secondary btn-sm" onclick="deleteApartment(${a.id})">Del</button>
        </div>
      </div>
      <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">${devHtml}</div>`;
    el.appendChild(card);
  });
}

function _populateAptRelaySelect(selectedId) {
  const sel = document.getElementById('aptRelayConfigId');
  sel.innerHTML = '<option value="">— None —</option>' +
    state.relayConfigs.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
  sel.value = selectedId || '';
}

async function openApartmentModal(editId) {
  if (!state.relayConfigs.length) {
    state.relayConfigs = await api('/api/settings/relays').then(r => r || []);
  }
  if (!state.areas.length) {
    state.areas = await api('/api/areas/').then(r => r || []);
  }
  const isEdit = !!editId;
  document.getElementById('apartmentModalTitle').textContent = isEdit ? 'Edit Apartment' : 'Add Apartment';
  document.getElementById('aptEditId').value = editId || '';

  // Populate area datalist
  const adl = document.getElementById('aptAreaList');
  if (adl) adl.innerHTML = state.areas.map(a => `<option value="${esc(a.name)}"></option>`).join('');

  if (isEdit) {
    const a = state.apartments.find(x => x.id === editId);
    if (a) {
      document.getElementById('aptName').value = a.name || '';
      const area = state.areas.find(x => x.id === a.area_id);
      document.getElementById('aptAreaName').value = area ? area.name : '';
      _populateAptRelaySelect(a.relay_config_id);
    }
  } else {
    document.getElementById('aptName').value = '';
    document.getElementById('aptAreaName').value = '';
    _populateAptRelaySelect(null);
  }
  openModal('apartmentModal');
}

async function submitApartment() {
  const editId   = document.getElementById('aptEditId').value;
  const relayId  = document.getElementById('aptRelayConfigId').value;
  const areaName = (document.getElementById('aptAreaName')?.value || '').trim();
  const areaId   = await _resolveAreaId(areaName);
  const body = {
    name:            document.getElementById('aptName').value.trim(),
    relay_config_id: relayId ? +relayId : null,
    area_id:         areaId || null,
  };
  if (!body.name) { toast('Name is required', 'error'); return; }
  if (editId) {
    await api(`/api/apartments/${editId}`, 'PUT', body);
  } else {
    await api('/api/apartments/', 'POST', body);
  }
  closeModal('apartmentModal');
  toast('Apartment saved', 'success');
  loadApartments();
}

async function deleteApartment(id) {
  if (!confirm('Delete this apartment? Devices will be un-assigned.')) return;
  await api(`/api/apartments/${id}`, 'DELETE');
  toast('Apartment deleted', 'success');
  loadApartments();
}

// ---------------------------------------------------------------------------
// Areas
// ---------------------------------------------------------------------------

async function loadAreas() {
  [state.areas, state.relayConfigs, state.apartments, state.devices] = await Promise.all([
    api('/api/areas/').then(r => r || []),
    api('/api/settings/relays').then(r => r || []),
    api('/api/apartments/').then(r => r || []),
    api('/api/devices/').then(r => r || []),
  ]);
  renderAreas(state.areas);
}

function renderAreas(areas) {
  const el = document.getElementById('areasList');
  if (!el) return;
  if (!areas.length) {
    el.innerHTML = '<p class="hint">No areas configured yet.</p>';
    return;
  }
  el.innerHTML = '';
  const relayName = id => (state.relayConfigs.find(r => r.id === id) || {}).name || '—';
  areas.forEach(a => {
    const areaApts  = state.apartments.filter(ap => ap.area_id === a.id);
    const areaDev   = state.devices.filter(d => d.area_id === a.id);
    const aptHtml = areaApts.length
      ? areaApts.map(ap => `<span class="seen-device-chip is-known" title="Apartment">${esc(ap.name)}</span>`).join('')
      : '';
    const devHtml = areaDev.length
      ? areaDev.map(d => `<span class="seen-device-chip" title="${esc(d.device_id)}">${esc(d.name)}</span>`).join('')
      : '';
    const membersHtml = (aptHtml || devHtml)
      ? aptHtml + devHtml
      : '<span class="hint" style="font-size:0.82rem">No apartments or devices assigned</span>';

    const card = document.createElement('div');
    card.className = 'config-card';
    card.style.flexDirection = 'column';
    card.style.alignItems = 'stretch';
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div class="config-card-info">
          <div class="config-card-name">${esc(a.name)}</div>
          <div class="config-card-sub">Area relay: ${esc(relayName(a.relay_config_id))}</div>
        </div>
        <div class="config-card-actions">
          <button type="button" class="btn-secondary btn-sm" onclick="openAreaModal(${a.id})">Edit</button>
          <button type="button" class="btn-secondary btn-sm" onclick="deleteArea(${a.id})">Del</button>
        </div>
      </div>
      <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">${membersHtml}</div>`;
    el.appendChild(card);
  });
}

async function openAreaModal(editId) {
  if (!state.relayConfigs.length) {
    state.relayConfigs = await api('/api/settings/relays').then(r => r || []);
  }
  const isEdit = !!editId;
  document.getElementById('areaModalTitle').textContent = isEdit ? 'Edit Area' : 'Add Area';
  document.getElementById('areaEditId').value = editId || '';

  const relaySel = document.getElementById('areaRelayConfigId');
  relaySel.innerHTML = '<option value="">— None —</option>' +
    state.relayConfigs.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');

  if (isEdit) {
    const a = state.areas.find(x => x.id === editId);
    if (a) {
      document.getElementById('areaName').value = a.name || '';
      relaySel.value = a.relay_config_id || '';
    }
  } else {
    document.getElementById('areaName').value = '';
    relaySel.value = '';
  }
  openModal('areaModal');
}

async function submitArea() {
  const editId  = document.getElementById('areaEditId').value;
  const relayId = document.getElementById('areaRelayConfigId').value;
  const body = {
    name:            document.getElementById('areaName').value.trim(),
    relay_config_id: relayId ? +relayId : null,
  };
  if (!body.name) { toast('Name is required', 'error'); return; }
  if (editId) {
    await api(`/api/areas/${editId}`, 'PUT', body);
  } else {
    await api('/api/areas/', 'POST', body);
  }
  closeModal('areaModal');
  toast('Area saved', 'success');
  loadAreas();
}

async function deleteArea(id) {
  if (!confirm('Delete this area? Apartments and devices will be un-assigned from it.')) return;
  await api(`/api/areas/${id}`, 'DELETE');
  toast('Area deleted', 'success');
  loadAreas();
}

// ---------------------------------------------------------------------------
// Acknowledge / Clear modals
// ---------------------------------------------------------------------------

async function openAckModal(callId) {
  document.getElementById('ackCallId').value = callId;
  document.getElementById('ackNotes').value = '';
  await populateUserSelect('ackActor');
  openModal('ackModal');
}

async function openClearModal(callId) {
  document.getElementById('clearCallId').value = callId;
  document.getElementById('clearNotes').value = '';
  await populateUserSelect('clearActor');
  openModal('clearModal');
}

async function submitAck() {
  const callId = document.getElementById('ackCallId').value;
  const actor  = document.getElementById('ackActor').value;
  const notes  = document.getElementById('ackNotes').value;
  await api(`/api/calls/${callId}/acknowledge`, 'POST', { actor, notes });
  closeModal('ackModal');
}

async function submitClear() {
  const callId = document.getElementById('clearCallId').value;
  const actor  = document.getElementById('clearActor').value;
  const notes  = document.getElementById('clearNotes').value;
  await api(`/api/calls/${callId}/clear`, 'POST', { actor, notes });
  closeModal('clearModal');
}

// ---------------------------------------------------------------------------
// Test call injection
// ---------------------------------------------------------------------------

async function openInjectModal() {
  if (!state.devices.length) state.devices = await api('/api/devices/') || [];
  const sel = document.getElementById('injectDeviceId');
  sel.innerHTML = state.devices.map(d =>
    `<option value="${esc(d.device_id)}">${esc(d.name)} (${esc(d.device_id)})</option>`
  ).join('') || '<option value="">No devices registered</option>';
  document.getElementById('injectRaw').value = '';
  openModal('injectModal');
}

async function submitInject() {
  const device_id = document.getElementById('injectDeviceId').value;
  const raw_data  = document.getElementById('injectRaw').value || 'manual-test';
  if (!device_id) { toast('Select a device first', 'error'); return; }
  await api('/api/calls/inject', 'POST', { device_id, raw_data });
  closeModal('injectModal');
  toast('Test call injected', 'success');
}

// ---------------------------------------------------------------------------
// Settings page
// ---------------------------------------------------------------------------

function showSettingsTab(tab) {
  const panel = document.getElementById('page-settings');
  panel.querySelectorAll(':scope > .settings-panel').forEach(p => p.classList.add('hidden'));
  panel.querySelectorAll(':scope > .settings-tabs .stab').forEach(b => b.classList.remove('active'));
  document.getElementById(`stab-${tab}`)?.classList.remove('hidden');
  panel.querySelector(`.settings-tabs .stab[onclick="showSettingsTab('${tab}')"]`)?.classList.add('active');
  state.currentSettingsTab = tab;
  if (tab === 'innovonics') loadCoordPage();
  if (tab === 'roamalert')  { loadRaSettings(); loadAeroscoutSettings(); }
}

function showCoordTab(tab) {
  // Inner tabs live inside stab-innovonics
  const panel = document.getElementById('stab-innovonics');
  if (!panel) return;
  panel.querySelectorAll('[id^="ctab-"]').forEach(p => p.classList.add('hidden'));
  panel.querySelectorAll('.stab').forEach(b => b.classList.remove('active'));
  document.getElementById(`ctab-${tab}`)?.classList.remove('hidden');
  panel.querySelector(`.stab[onclick="showCoordTab('${tab}')"]`)?.classList.add('active');
  // When opening the monitor tab, render any lines buffered while it was hidden
  if (tab === 'monitor') _flushMonitorBuffer();
}

async function loadCoordPage() {
  const [cfg, status] = await Promise.all([
    api('/api/settings/innovonics'),
    api('/api/settings/innovonics/status'),
  ]);
  if (cfg) loadInnoForm(cfg);
  if (status) updateCoordinatorStatus(status.status);
  loadRepeaters();
}

async function loadSettingsPage() {
  const [smtp, telegram, twilio, pagers, relays, inputs, rules, users, areas, devices] = await Promise.all([
    api('/api/settings/smtp'),
    api('/api/settings/telegram'),
    api('/api/settings/twilio'),
    api('/api/settings/pagers'),
    api('/api/settings/relays'),
    api('/api/settings/inputs'),
    api('/api/settings/rules'),
    api('/api/users/'),
    api('/api/areas/'),
    api('/api/devices/'),
  ]);

  loadSmtpForm(smtp || {});
  loadTelegramForm(telegram || {});
  loadTwilioForm(twilio || {});
  renderPagers(pagers || []);
  state.relayConfigs = relays || [];
  renderRelays(state.relayConfigs);
  state.inputConfigs = inputs || [];
  renderInputs(state.inputConfigs);
  state.areas = areas || [];
  renderRules(rules || []);
  state.users = users || [];
  renderUsers(users || []);
  state.devices = devices || [];
  renderSeenDevices();
  // Sync auth toggle with current server state
  const toggle = document.getElementById('authRequiredToggle');
  if (toggle) toggle.checked = state.authRequired;
}

// ── SMTP ──────────────────────────────────────────────────────

function loadSmtpForm(cfg) {
  document.getElementById('smtpServer').value     = cfg.server     || '';
  document.getElementById('smtpPort').value       = cfg.port       || 587;
  document.getElementById('smtpEmail').value      = cfg.email      || '';
  document.getElementById('smtpEncryption').value = cfg.encryption || 'STARTTLS';
  document.getElementById('smtpEnabled').checked  = !!cfg.enabled;
  // Never pre-fill password from server
}

async function saveSMTP(e) {
  e.preventDefault();
  const body = {
    server:     document.getElementById('smtpServer').value,
    port:       +document.getElementById('smtpPort').value,
    email:      document.getElementById('smtpEmail').value,
    password:   document.getElementById('smtpPassword').value,
    encryption: document.getElementById('smtpEncryption').value,
    enabled:    document.getElementById('smtpEnabled').checked ? 1 : 0,
  };
  const r = await api('/api/settings/smtp', 'PUT', body);
  if (r?.ok) toast('SMTP settings saved', 'success');
}

async function testSMTP() {
  const to = prompt('Send test email to:');
  if (!to) return;
  const r = await api('/api/settings/smtp/test', 'POST', { to });
  if (r?.ok) toast('Test email sent!', 'success');
}

// ── Telegram ──────────────────────────────────────────────────

function loadTelegramForm(cfg) {
  // bot_token is never returned by GET — leave the password field blank
  document.getElementById('telegramChatId').value  = cfg.chat_id || '';
  document.getElementById('telegramEnabled').checked = !!cfg.enabled;
}

async function saveTelegram(e) {
  e.preventDefault();
  const token = document.getElementById('telegramBotToken').value;
  const body = {
    chat_id: document.getElementById('telegramChatId').value,
    enabled: document.getElementById('telegramEnabled').checked ? 1 : 0,
  };
  // Only send bot_token if the user typed something (preserve existing otherwise)
  if (token) body.bot_token = token;
  const r = await api('/api/settings/telegram', 'PUT', body);
  if (r?.ok) {
    toast('Telegram settings saved', 'success');
    document.getElementById('telegramBotToken').value = '';
  }
}

async function testTelegram() {
  const chatId = document.getElementById('telegramChatId').value.trim() || null;
  const r = await api('/api/settings/telegram/test', 'POST', { chat_id: chatId });
  if (r?.ok) toast('Telegram test message sent!', 'success');
}

// ── Twilio ────────────────────────────────────────────────────

function loadTwilioForm(cfg) {
  // auth_token is never returned by GET — leave the password field blank
  document.getElementById('twilioAccountSid').value  = cfg.account_sid  || '';
  document.getElementById('twilioFromNumber').value  = cfg.from_number  || '';
  document.getElementById('twilioToNumber').value    = cfg.to_number    || '';
  document.getElementById('twilioEnabled').checked   = !!cfg.enabled;
}

async function saveTwilio(e) {
  e.preventDefault();
  const token = document.getElementById('twilioAuthToken').value;
  const body = {
    account_sid:  document.getElementById('twilioAccountSid').value,
    from_number:  document.getElementById('twilioFromNumber').value,
    to_number:    document.getElementById('twilioToNumber').value,
    enabled:      document.getElementById('twilioEnabled').checked ? 1 : 0,
  };
  if (token) body.auth_token = token;
  const r = await api('/api/settings/twilio', 'PUT', body);
  if (r?.ok) {
    toast('Twilio settings saved', 'success');
    document.getElementById('twilioAuthToken').value = '';
  }
}

async function testTwilio() {
  const toNumber = document.getElementById('twilioToNumber').value.trim() || null;
  const r = await api('/api/settings/twilio/test', 'POST', { to_number: toNumber });
  if (r?.ok) toast('Twilio test SMS sent!', 'success');
}

// ── Pagers ────────────────────────────────────────────────────

function renderPagers(pagers) {
  const el = document.getElementById('pagersList');
  el.innerHTML = pagers.length ? '' : '<p class="hint">No pagers configured yet.</p>';
  pagers.forEach(p => {
    const card = document.createElement('div');
    card.className = 'config-card';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(p.name)}</div>
        <div class="config-card-sub">${esc(p.protocol)} · ${esc(p.host)}:${p.port}  · Capcode: ${esc(p.default_capcode || '—')}</div>
      </div>
      <div class="config-card-actions">
        <button class="btn-secondary btn-sm" onclick="testPager(${p.id})">Test</button>
        <button class="btn-secondary btn-sm" onclick="openPagerModal(${p.id})">Edit</button>
        <button class="btn-secondary btn-sm" onclick="deletePager(${p.id})">Del</button>
      </div>`;
    el.appendChild(card);
  });
}

function openPagerModal(editId) {
  const isEdit = !!editId;
  document.getElementById('pagerModalTitle').textContent = isEdit ? 'Edit Pager' : 'Add Pager';
  document.getElementById('pagerEditId').value = editId || '';
  if (!isEdit) {
    ['pagerName','pagerHost','pagerCapcode'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('pagerPort').value = 9000;
    document.getElementById('pagerProto').value = 'TAP';
    document.getElementById('pagerEnabled').checked = true;
  }
  openModal('pagerModal');
}

async function submitPager() {
  const editId = document.getElementById('pagerEditId').value;
  const body = {
    name: document.getElementById('pagerName').value,
    host: document.getElementById('pagerHost').value,
    port: +document.getElementById('pagerPort').value,
    protocol: document.getElementById('pagerProto').value,
    default_capcode: document.getElementById('pagerCapcode').value || null,
    enabled: document.getElementById('pagerEnabled').checked ? 1 : 0,
  };
  if (editId) {
    await api(`/api/settings/pagers/${editId}`, 'PUT', body);
  } else {
    await api('/api/settings/pagers', 'POST', body);
  }
  closeModal('pagerModal');
  const pagers = await api('/api/settings/pagers');
  renderPagers(pagers || []);
}

async function deletePager(id) {
  if (!confirm('Delete this pager?')) return;
  await api(`/api/settings/pagers/${id}`, 'DELETE');
  const pagers = await api('/api/settings/pagers');
  renderPagers(pagers || []);
}

async function testPager(id) {
  const capcode = prompt('Capcode for test:');
  if (!capcode) return;
  const r = await api(`/api/settings/pagers/${id}/test`, 'POST', { capcode, message: 'TEST - Nurse Call System' });
  if (r?.ok) toast('Test page sent!', 'success');
}

// ── Relays ────────────────────────────────────────────────────

function renderRelays(relays) {
  const el = document.getElementById('relaysList');
  // Add Batch Add button above the list
  el.innerHTML = `<div style="margin-bottom:10px">
    <button class="btn-secondary btn-sm" onclick="openModal('relayBatchModal')">Batch Add</button>
  </div>`;
  if (!relays.length) {
    el.innerHTML += '<p class="hint">No dome light controllers configured yet.</p>';
    return;
  }
  relays.forEach(r => {
    const card = document.createElement('div');
    card.className = 'config-card';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(r.name)}</div>
        <div class="config-card-sub">${esc(r.relay_type.toUpperCase())} · ${esc(r.host)}:${r.port} · Relay #${r.relay_number}</div>
      </div>
      <div class="config-card-actions">
        <button class="btn-secondary btn-sm" onclick="testRelay(${r.id})">Test</button>
        <button class="btn-secondary btn-sm" onclick="cloneRelay(${r.id})">Clone</button>
        <button class="btn-secondary btn-sm" onclick="openRelayModal(${r.id})">Edit</button>
        <button class="btn-secondary btn-sm" onclick="deleteRelay(${r.id})">Del</button>
      </div>`;
    el.appendChild(card);
  });
}

function openRelayModal(editId) {
  const isEdit = !!editId;
  document.getElementById('relayModalTitle').textContent = isEdit ? 'Edit Controller' : 'Add Dome Light Controller';
  document.getElementById('relayEditId').value = editId || '';
  if (isEdit) {
    // Find the relay in the cached list from the last settings load
    const r = state.relayConfigs.find(x => x.id === editId);
    if (r) {
      document.getElementById('relayName').value    = r.name        || '';
      document.getElementById('relayType').value    = r.relay_type  || 'rcm';
      document.getElementById('relayHost').value    = r.host        || '';
      document.getElementById('relayPort').value    = r.port        || 23;
      document.getElementById('relayNumber').value  = r.relay_number || 1;
      document.getElementById('relayEnabled').checked = !!r.enabled;
    }
  } else {
    ['relayName','relayHost'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('relayType').value = 'rcm';
    document.getElementById('relayPort').value = 23;
    document.getElementById('relayNumber').value = 1;
    document.getElementById('relayEnabled').checked = true;
  }
  openModal('relayModal');
}

async function submitRelay() {
  const editId = document.getElementById('relayEditId').value;
  const body = {
    name:         document.getElementById('relayName').value,
    relay_type:   document.getElementById('relayType').value,
    host:         document.getElementById('relayHost').value,
    port:         +document.getElementById('relayPort').value,
    relay_number: +document.getElementById('relayNumber').value,
    enabled:      document.getElementById('relayEnabled').checked ? 1 : 0,
  };
  if (editId) {
    await api(`/api/settings/relays/${editId}`, 'PUT', body);
  } else {
    await api('/api/settings/relays', 'POST', body);
  }
  closeModal('relayModal');
  const relays = await api('/api/settings/relays');
  state.relayConfigs = relays || [];
  renderRelays(state.relayConfigs);
}

async function deleteRelay(id) {
  if (!confirm('Delete this controller?')) return;
  await api(`/api/settings/relays/${id}`, 'DELETE');
  const relays = await api('/api/settings/relays');
  state.relayConfigs = relays || [];
  renderRelays(state.relayConfigs);
}

async function cloneRelay(id) {
  const r = await api(`/api/settings/relays/${id}/clone`, 'POST');
  if (r?.id) {
    toast(`Cloned as "${r.name}"`, 'success');
    const relays = await api('/api/settings/relays');
    state.relayConfigs = relays || [];
    renderRelays(state.relayConfigs);
  }
}

async function submitBatchRelay() {
  const body = {
    name_prefix: document.getElementById('batchNamePrefix').value.trim(),
    relay_type:  document.getElementById('batchRelayType').value,
    host:        document.getElementById('batchRelayHost').value.trim(),
    port:        +document.getElementById('batchRelayPort').value,
    count:       +document.getElementById('batchRelayCount').value,
    enabled:     1,
  };
  if (!body.name_prefix) { toast('Name prefix is required', 'error'); return; }
  if (!body.host)         { toast('Host is required', 'error'); return; }
  if (!body.count || body.count < 1) { toast('Count must be at least 1', 'error'); return; }
  const created = await api('/api/settings/relays/batch', 'POST', body);
  if (Array.isArray(created)) {
    closeModal('relayBatchModal');
    toast(`Created ${created.length} relay entries`, 'success');
    const relays = await api('/api/settings/relays');
    state.relayConfigs = relays || [];
    renderRelays(state.relayConfigs);
  }
}

async function testRelay(id) {
  toast('Testing relay — it will activate for 3 seconds…', 'warn');
  const r = await api(`/api/settings/relays/${id}/test`, 'POST', { duration_seconds: 3 });
  if (r?.ok) toast('Relay test complete', 'success');
}

// ── Inputs ────────────────────────────────────────────────────

function renderInputs(inputs) {
  const el = document.getElementById('inputsList');
  if (!el) return;
  if (!inputs.length) {
    el.innerHTML = '<p class="hint">No input triggers configured yet.</p>';
    return;
  }
  el.innerHTML = '';
  inputs.forEach(i => {
    const card = document.createElement('div');
    card.className = 'config-card';
    const lastSeen = i.last_seen ? new Date(i.last_seen).toLocaleString() : 'Never';
    const modeLabel = +i.active_high ? 'Active High' : 'Active Low';
    const stateLabel = i.last_state === null || i.last_state === undefined
      ? 'Unknown'
      : ((`${i.last_state}` === '1' || `${i.last_state}`.toLowerCase() === 'true') ? 'ON' : 'OFF');
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(i.name)}</div>
        <div class="config-card-sub">${esc((i.input_type || 'esp').toUpperCase())} · ${esc(i.host)}:${i.port} · Input #${i.input_number}</div>
        <div class="config-card-meta">${modeLabel} · Last state: ${stateLabel} · Last seen: ${lastSeen}</div>
      </div>
      <div class="config-card-actions">
        <button class="btn-secondary btn-sm" onclick="testInput(${i.id})">Test</button>
        <button class="btn-secondary btn-sm" onclick="openInputModal(${i.id})">Edit</button>
        <button class="btn-secondary btn-sm" onclick="deleteInput(${i.id})">Del</button>
      </div>`;
    el.appendChild(card);
  });
}

function openInputModal(editId) {
  const isEdit = !!editId;
  document.getElementById('inputModalTitle').textContent = isEdit ? 'Edit Input Trigger' : 'Add Input Trigger';
  document.getElementById('inputEditId').value = editId || '';

  if (isEdit) {
    const i = state.inputConfigs.find(x => x.id === editId);
    if (i) {
      document.getElementById('inputName').value = i.name || '';
      document.getElementById('inputType').value = i.input_type || 'esp';
      document.getElementById('inputHost').value = i.host || '';
      document.getElementById('inputPort').value = i.port || 80;
      document.getElementById('inputNumber').value = i.input_number || 1;
      document.getElementById('inputInputName').value = i.input_name || '';
      document.getElementById('inputActiveHigh').checked = !!i.active_high;
      document.getElementById('inputEnabled').checked = !!i.enabled;
    }
  } else {
    ['inputName', 'inputHost', 'inputInputName'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('inputType').value = 'esp';
    document.getElementById('inputPort').value = 80;
    document.getElementById('inputNumber').value = 1;
    document.getElementById('inputActiveHigh').checked = true;
    document.getElementById('inputEnabled').checked = true;
  }

  openModal('inputModal');
}

async function submitInput() {
  const editId = document.getElementById('inputEditId').value;
  const body = {
    name: document.getElementById('inputName').value.trim(),
    input_type: document.getElementById('inputType').value,
    host: document.getElementById('inputHost').value.trim(),
    port: +document.getElementById('inputPort').value,
    input_number: +document.getElementById('inputNumber').value,
    input_name: document.getElementById('inputInputName').value.trim() || null,
    active_high: document.getElementById('inputActiveHigh').checked ? 1 : 0,
    enabled: document.getElementById('inputEnabled').checked ? 1 : 0,
  };

  if (!body.name) { toast('Name is required', 'error'); return; }
  if (!body.host) { toast('Host is required', 'error'); return; }
  if (!body.input_number || body.input_number < 1) { toast('Input Number must be at least 1', 'error'); return; }

  if (editId) {
    await api(`/api/settings/inputs/${editId}`, 'PUT', body);
  } else {
    await api('/api/settings/inputs', 'POST', body);
  }

  closeModal('inputModal');
  const inputs = await api('/api/settings/inputs');
  state.inputConfigs = inputs || [];
  renderInputs(state.inputConfigs);
}

async function deleteInput(id) {
  if (!confirm('Delete this input trigger?')) return;
  await api(`/api/settings/inputs/${id}`, 'DELETE');
  const inputs = await api('/api/settings/inputs');
  state.inputConfigs = inputs || [];
  renderInputs(state.inputConfigs);
}

async function testInput(id) {
  toast('Testing input — it will alarm for 3 seconds…', 'warn');
  const r = await api(`/api/settings/inputs/${id}/test`, 'POST', { duration_seconds: 3 });
  if (r?.ok) toast('Input test complete', 'success');
}

async function submitBatchInput() {
  const body = {
    name_prefix: document.getElementById('batchInputNamePrefix').value.trim(),
    host: document.getElementById('batchInputHost').value.trim(),
    port: +document.getElementById('batchInputPort').value,
    start_number: +document.getElementById('batchInputStartNumber').value,
    count: +document.getElementById('batchInputCount').value,
    input_name_prefix: document.getElementById('batchInputSensorPrefix').value.trim(),
    active_high: document.getElementById('batchInputActiveHigh').checked ? 1 : 0,
    enabled: 1,
  };
  if (!body.name_prefix) { toast('Alarm Name Prefix is required', 'error'); return; }
  if (!body.host) { toast('Host is required', 'error'); return; }
  if (!body.start_number || body.start_number < 1) { toast('Starting Input Number must be at least 1', 'error'); return; }
  if (!body.count || body.count < 1) { toast('Number of Inputs must be at least 1', 'error'); return; }

  const created = await api('/api/settings/inputs/batch', 'POST', body);
  if (Array.isArray(created)) {
    closeModal('inputBatchModal');
    toast(`Created ${created.length} input entries`, 'success');
    const inputs = await api('/api/settings/inputs');
    state.inputConfigs = inputs || [];
    renderInputs(state.inputConfigs);
  }
}

// ── Innovonics ────────────────────────────────────────────────

function loadInnoForm(cfg) {
  document.getElementById('innoMode').value      = cfg.mode        || 'tcp';
  document.getElementById('innoHost').value      = cfg.host        || '';
  document.getElementById('innoPort').value      = cfg.port        || 3000;
  document.getElementById('innoSerial').value    = cfg.serial_port || '';
  document.getElementById('innoBaud').value      = cfg.baud_rate   || 9600;
  document.getElementById('innoNid').value       = cfg.nid         || 16;
  document.getElementById('innoEnabled').checked = !!cfg.enabled;
  toggleInnoMode();
}

function toggleInnoMode() {
  const mode = document.getElementById('innoMode').value;
  document.getElementById('innoTcpFields').classList.toggle('hidden', mode !== 'tcp');
  document.getElementById('innoSerialFields').classList.toggle('hidden', mode === 'tcp');
}

async function saveInno(e) {
  e.preventDefault();
  const body = {
    mode:        document.getElementById('innoMode').value,
    host:        document.getElementById('innoHost').value || null,
    port:        +document.getElementById('innoPort').value,
    serial_port: document.getElementById('innoSerial').value || null,
    baud_rate:   +document.getElementById('innoBaud').value,
    nid:         +document.getElementById('innoNid').value || 16,
    enabled:     document.getElementById('innoEnabled').checked ? 1 : 0,
  };
  const r = await api('/api/settings/innovonics', 'PUT', body);
  if (r?.ok) toast('Coordinator settings saved. Restart server to apply.', 'success');
}

// ── Repeaters ─────────────────────────────────────────────────

async function loadRepeaters() {
  const repeaters = await api('/api/settings/repeaters');
  renderRepeaters(repeaters || []);
}

function renderRepeaters(repeaters) {
  const el = document.getElementById('repeatersList');
  if (!el) return;
  if (!repeaters.length) {
    el.innerHTML = '<p class="hint">No repeaters registered. Add repeaters by their serial number.</p>';
    return;
  }
  el.innerHTML = '';
  repeaters.forEach(r => {
    const card = document.createElement('div');
    card.className = 'config-card';
    const seen   = r.last_seen ? new Date(r.last_seen).toLocaleString() : 'Never';
    const status = r.status === 'online' ? '<span class="rep-status online">Online</span>'
                                         : '<span class="rep-status offline">Offline</span>';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(r.name || r.serial_number)} ${status}</div>
        <div class="config-card-meta">SN: ${esc(r.serial_number)} &bull; Last seen: ${seen}</div>
      </div>
      <div class="config-card-actions">
        <button type="button" class="btn-secondary btn-sm" title="Push NID to this repeater now"
          onclick="forceRepeaterNid(${r.id}, '${esc(r.serial_number)}')">Force NID</button>
        <button type="button" class="btn-danger btn-sm" onclick="deleteRepeater(${r.id})">Delete</button>
      </div>`;
    el.appendChild(card);
  });
}

function openAddRepeaterModal() {
  document.getElementById('repSerial').value = '';
  document.getElementById('repName').value = '';
  document.getElementById('repeaterModal').classList.remove('hidden');
}

function closeRepeaterModal() {
  document.getElementById('repeaterModal').classList.add('hidden');
}

async function submitAddRepeater(e) {
  e.preventDefault();
  const body = {
    serial_number: document.getElementById('repSerial').value.trim(),
    name:          document.getElementById('repName').value.trim() || null,
  };
  const r = await api('/api/settings/repeaters', 'POST', body);
  if (r?.id) {
    closeRepeaterModal();
    toast('Repeater added', 'success');
    loadRepeaters();
  }
}

async function deleteRepeater(id) {
  const r = await api(`/api/settings/repeaters/${id}`, 'DELETE');
  if (r?.ok) { toast('Repeater removed', 'success'); loadRepeaters(); }
}

async function forceRepeaterNid(id, serial) {
  const r = await api(`/api/settings/repeaters/${id}/force-nid`, 'POST');
  if (r?.ok) toast(`NID command sent to repeater ${serial} — awaiting confirmation`, 'success');
}

// ── Repeater learn mode ────────────────────────────────────────

function startRepeaterLearnMode() {
  if (state.learnActive) return;
  state.learnActive = true;
  state.learnTarget = 'repeater';

  const btn    = document.getElementById('btnRepDetect');
  const status = document.getElementById('repLearnStatus');
  const text   = document.getElementById('repLearnText');
  if (btn)    btn.disabled = true;
  if (status) status.classList.remove('hidden');
  if (text)   text.textContent = 'Waiting — activate the repeater now…';

  // Repeater detection uses coordinator.repeater_seen WS events only.
  // No HTTP long-poll needed — repeaters transmit check-ins naturally.
  // Auto-cancel after 30 s.
  state.learnTimer = setTimeout(() => {
    if (state.learnTarget === 'repeater') {
      toast('Repeater auto-detect timed out — no repeater heard', 'error');
      _endRepeaterLearnMode();
    }
  }, 30000);
}

function stopRepeaterLearnMode() {
  _endRepeaterLearnMode();
}

function _endRepeaterLearnMode() {
  state.learnActive = false;
  state.learnTarget = null;
  if (state.learnTimer) { clearTimeout(state.learnTimer); state.learnTimer = null; }
  const btn    = document.getElementById('btnRepDetect');
  const status = document.getElementById('repLearnStatus');
  if (btn)    btn.disabled = false;
  if (status) status.classList.add('hidden');
}

function _prefillRepeaterSerial(serial) {
  const inp = document.getElementById('repSerial');
  if (inp) {
    inp.value = serial;
    inp.classList.add('highlight-flash');
    setTimeout(() => inp.classList.remove('highlight-flash'), 1500);
  }
  toast(`Repeater detected: ${serial}`, 'success');
}

// ── Notification rules ────────────────────────────────────────

const RULE_HINTS = {
  email:    '{"recipients":"nurse@example.com","subject":"{apartment} — {PRIORITY} Alert","body":"{device_name} at {location}\\nPriority: {priority}\\nTime: {timestamp}"}',
  page:     '{"pager_id":1,"capcode":"1885","message":"{device_name} {location} [{PRIORITY}]"}',
  relay:    '{"relay_id":1}',
  telegram: '{"chat_id":"-100123456789","message":"*{PRIORITY} CALL*: {device_name}\\nLocation: {location}\\nApartment: {apartment}\\nArea: {area}\\nTime: {timestamp}"}',
  twilio:   '{"to_number":"+15559876543","message":"{PRIORITY}: {device_name} at {location} — {apartment}"}',
};

function updateRuleConfigHint() {
  const type = document.getElementById('ruleActionType').value;
  document.getElementById('ruleConfigHint').textContent = RULE_HINTS[type] || '';
  document.getElementById('ruleActionConfig').placeholder = RULE_HINTS[type] || '';
}

function renderRules(rules) {
  const el = document.getElementById('rulesList');
  el.innerHTML = rules.length ? '' : '<p class="hint">No notification rules configured.</p>';
  rules.forEach(r => {
    const card = document.createElement('div');
    card.className = 'config-card';
    const cfg = (() => { try { return JSON.parse(r.action_config); } catch { return {}; } })();
    const areaLabel = r.area_filter && r.area_filter !== 'all'
      ? ` · area: ${esc((state.areas.find(a => String(a.id) === String(r.area_filter)) || {}).name || r.area_filter)}`
      : '';
    const notifyOnLabel = { call: 'on alarm', clear: 'on clear', both: 'alarm + clear' }[r.notify_on] || 'on alarm';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(r.name)} ${r.enabled ? '' : '<span class="tag tag-acknowledged">disabled</span>'}</div>
        <div class="config-card-sub">${esc(r.action_type)} · ${notifyOnLabel} · devices: ${esc(r.device_filter)} · priority: ${esc(r.priority_filter)}${areaLabel}</div>
      </div>
      <div class="config-card-actions">
        <button class="btn-secondary btn-sm" onclick="openRuleModal(${r.id})">Edit</button>
        <button class="btn-secondary btn-sm" onclick="deleteRule(${r.id})">Del</button>
      </div>`;
    el.appendChild(card);
  });
}

async function openRuleModal(editId) {
  // Ensure areas are loaded for the area filter dropdown
  if (!state.areas.length) {
    state.areas = await api('/api/areas/').then(r => r || []);
  }
  // Populate area filter select
  const areaSel = document.getElementById('ruleAreaFilter');
  areaSel.innerHTML = '<option value="all">All Areas</option>' +
    state.areas.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');

  const isEdit = !!editId;
  document.getElementById('ruleModalTitle').textContent = isEdit ? 'Edit Rule' : 'Add Notification Rule';
  document.getElementById('ruleEditId').value = editId || '';
  if (isEdit) {
    // Find rule in settings (we need to fetch it since it's not cached)
    const rules = await api('/api/settings/rules');
    const rule = (rules || []).find(r => r.id === editId);
    if (rule) {
      document.getElementById('ruleName').value              = rule.name || '';
      document.getElementById('ruleDeviceFilter').value     = rule.device_filter || 'all';
      document.getElementById('rulePriorityFilter').value   = rule.priority_filter || 'all';
      areaSel.value                                          = rule.area_filter || 'all';
      document.getElementById('ruleNotifyOn').value         = rule.notify_on || 'call';
      document.getElementById('ruleActionType').value       = rule.action_type || 'email';
      document.getElementById('ruleActionConfig').value     = rule.action_config || '';
      document.getElementById('ruleEnabled').checked        = !!rule.enabled;
      updateRuleConfigHint();
    }
  } else {
    document.getElementById('ruleName').value = '';
    document.getElementById('ruleDeviceFilter').value = 'all';
    document.getElementById('rulePriorityFilter').value = 'all';
    areaSel.value = 'all';
    document.getElementById('ruleNotifyOn').value = 'call';
    document.getElementById('ruleActionType').value = 'email';
    document.getElementById('ruleActionConfig').value = '';
    document.getElementById('ruleEnabled').checked = true;
    updateRuleConfigHint();
  }
  openModal('ruleModal');
}

async function submitRule() {
  const editId = document.getElementById('ruleEditId').value;
  const body = {
    name:            document.getElementById('ruleName').value,
    device_filter:   document.getElementById('ruleDeviceFilter').value,
    priority_filter: document.getElementById('rulePriorityFilter').value,
    area_filter:     document.getElementById('ruleAreaFilter').value || 'all',
    notify_on:       document.getElementById('ruleNotifyOn').value || 'call',
    action_type:     document.getElementById('ruleActionType').value,
    action_config:   document.getElementById('ruleActionConfig').value,
    enabled:         document.getElementById('ruleEnabled').checked ? 1 : 0,
  };
  try { JSON.parse(body.action_config); } catch {
    toast('Action Config must be valid JSON', 'error'); return;
  }
  if (editId) {
    await api(`/api/settings/rules/${editId}`, 'PUT', body);
  } else {
    await api('/api/settings/rules', 'POST', body);
  }
  closeModal('ruleModal');
  const rules = await api('/api/settings/rules');
  renderRules(rules || []);
}

async function deleteRule(id) {
  if (!confirm('Delete this rule?')) return;
  await api(`/api/settings/rules/${id}`, 'DELETE');
  const rules = await api('/api/settings/rules');
  renderRules(rules || []);
}

// ── Users ─────────────────────────────────────────────────────

function renderUsers(users) {
  const el = document.getElementById('usersList');
  el.innerHTML = users.length ? '' : '<p class="hint">No users.</p>';
  users.forEach(u => {
    const card = document.createElement('div');
    card.className = 'config-card';
    const statusTag = u.active ? '' : ' <span class="tag tag-acknowledged">inactive</span>';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(u.username)}${statusTag}</div>
        <div class="config-card-sub">Role: ${esc(u.role)}</div>
      </div>
      <div class="config-card-actions">
        <button type="button" class="btn-secondary btn-sm" onclick="openEditUserModal(${u.id})">Edit</button>
        ${u.id !== 1 ? `<button type="button" class="btn-secondary btn-sm" onclick="deleteUser(${u.id})">Remove</button>` : ''}
      </div>`;
    el.appendChild(card);
  });
}

function openUserModal() {
  document.getElementById('userModalTitle').textContent = 'Add Staff User';
  document.getElementById('userSubmitBtn').textContent  = 'Add User';
  document.getElementById('userEditId').value   = '';
  document.getElementById('newUsername').value  = '';
  document.getElementById('newPassword').value  = '';
  document.getElementById('newPassword').placeholder = 'Optional — leave blank for no password';
  document.getElementById('newRole').value      = 'staff';
  document.getElementById('newActive').checked  = true;
  openModal('userModal');
}

function openEditUserModal(id) {
  const u = state.users.find(x => x.id === id);
  if (!u) return;
  document.getElementById('userModalTitle').textContent = 'Edit User';
  document.getElementById('userSubmitBtn').textContent  = 'Save Changes';
  document.getElementById('userEditId').value   = u.id;
  document.getElementById('newUsername').value  = u.username;
  document.getElementById('newPassword').value  = '';
  document.getElementById('newPassword').placeholder = 'Leave blank to keep current password';
  document.getElementById('newRole').value      = u.role;
  document.getElementById('newActive').checked  = !!u.active;
  openModal('userModal');
}

async function submitUser() {
  const editId   = document.getElementById('userEditId').value;
  const username = document.getElementById('newUsername').value.trim();
  const password = document.getElementById('newPassword').value;
  const role     = document.getElementById('newRole').value;
  const active   = document.getElementById('newActive').checked ? 1 : 0;
  if (!username) { toast('Username required', 'error'); return; }

  if (editId) {
    const body = { username, role, active };
    if (password) body.password = password;
    await api(`/api/users/${editId}`, 'PATCH', body);
  } else {
    await api('/api/users/', 'POST', { username, password, role, active });
  }
  closeModal('userModal');
  const users = await api('/api/users/');
  state.users = users || [];
  renderUsers(state.users);
}

async function deleteUser(id) {
  if (!confirm('Remove this user?')) return;
  await api(`/api/users/${id}`, 'DELETE');
  const users = await api('/api/users/');
  state.users = users || [];
  renderUsers(state.users);
}

// ── Auth ──────────────────────────────────────────────────────

function _clearSession() {
  state.token = null;
  state.currentUser = null;
  localStorage.removeItem('cc_token');
  document.getElementById('sidebarUser').classList.add('hidden');
}

function showLogin() {
  document.getElementById('loginError').classList.add('hidden');
  document.getElementById('loginError').textContent = '';
  document.getElementById('loginUsername').value = '';
  document.getElementById('loginPassword').value = '';
  document.getElementById('loginOverlay').classList.remove('hidden');
}

function hideLogin() {
  document.getElementById('loginOverlay').classList.add('hidden');
}

async function submitLogin() {
  const username = document.getElementById('loginUsername').value.trim();
  const password = document.getElementById('loginPassword').value;
  const errEl    = document.getElementById('loginError');
  errEl.classList.add('hidden');

  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });

  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    errEl.textContent = data.detail || 'Login failed';
    errEl.classList.remove('hidden');
    return;
  }

  const data = await res.json();
  state.token = data.token;
  state.currentUser = { username: data.username, role: data.role };
  localStorage.setItem('cc_token', data.token);
  hideLogin();
  _updateUserBadge();
  _applyRoleUI(data.role);
}

async function logout() {
  await api('/api/auth/logout', 'POST');
  _clearSession();
  if (state.authRequired) showLogin();
}

function _updateUserBadge() {
  const el = document.getElementById('sidebarUser');
  if (state.currentUser) {
    document.getElementById('sidebarUsername').textContent = state.currentUser.username;
    el.classList.remove('hidden');
  } else {
    el.classList.add('hidden');
  }
}

async function saveAuthRequired(checked) {
  const result = await api('/api/auth/config', 'PUT', { auth_required: checked });
  if (result != null) {
    state.authRequired = result.auth_required;
    const toggle = document.getElementById('authRequiredToggle');
    if (toggle) toggle.checked = state.authRequired;
  }
}

async function _bootAuth() {
  const cfg = await fetch('/api/auth/config').then(r => r.json()).catch(() => ({ auth_required: false }));
  state.authRequired = cfg.auth_required;

  // Update users-page toggle when settings loads
  const toggle = document.getElementById('authRequiredToggle');
  if (toggle) toggle.checked = state.authRequired;

  if (!state.authRequired) return;   // no auth needed — proceed normally

  // Auth is required — validate stored token
  if (state.token) {
    const me = await fetch('/api/auth/me', {
      headers: { Authorization: `Bearer ${state.token}` },
    }).then(r => r.ok ? r.json() : null).catch(() => null);

    if (me && me.authenticated) {
      state.currentUser = { username: me.username, role: me.role };
      _updateUserBadge();
      _applyRoleUI(me.role);
      return;   // valid session — continue
    }
  }

  // No valid token — show login
  _clearSession();
  showLogin();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function populateUserSelect(selectId) {
  if (!state.users.length) state.users = await api('/api/users/') || [];
  const sel = document.getElementById(selectId);
  sel.innerHTML = state.users.map(u =>
    `<option value="${esc(u.username)}">${esc(u.username)}</option>`
  ).join('') || '<option value="staff">staff</option>';
}

async function api(path, method = 'GET', body = null) {
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
    const opts = { method, headers };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(API + path, opts);
    if (res.status === 401) {
      // Token expired or revoked — show login only if auth is actually required
      _clearSession();
      if (state.authRequired) showLogin();
      return null;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      toast(err.detail || 'Request failed', 'error');
      return null;
    }
    return await res.json();
  } catch (err) {
    toast(`Network error: ${err.message}`, 'error');
    return null;
  }
}

function openModal(id) {
  document.getElementById(id)?.classList.remove('hidden');
}
function closeModal(id) {
  document.getElementById(id)?.classList.add('hidden');
}

// Close modal on backdrop click — never close the login overlay this way
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  if (overlay.id === 'loginOverlay') return;
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.classList.add('hidden');
  });
});

function toast(msg, type = '') {
  const container = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast${type ? ' ' + type : ''}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Parse timestamps from server: Python isoformat() → +00:00 suffix, SQLite → no suffix.
// Strip any tz offset and re-append Z so Date() always parses as UTC.
function _parseIso(iso) {
  if (!iso) return new Date(NaN);
  const s = iso.replace(' ', 'T').replace(/[+-]\d{2}:\d{2}$/, '') + 'Z';
  return new Date(s);
}

function fmtTime(iso) {
  if (!iso) return '—';
  return _parseIso(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDateTime(iso) {
  if (!iso) return '—';
  return _parseIso(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function elapsedStr(iso) {
  const secs = Math.floor((Date.now() - _parseIso(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

// ---------------------------------------------------------------------------
// Hard refresh — clears all caches + unregisters service workers, then reloads.
// Triggered by double-clicking the sidebar logo (useful on PWA where Shift+F5
// is not available).
// ---------------------------------------------------------------------------

async function hardRefresh() {
  try {
    // Clear all Cache Storage entries
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    }
    // Unregister any service workers so they re-install cleanly
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
  } catch (e) { /* ignore — reload regardless */ }
  location.reload(true);
}

// ---------------------------------------------------------------------------
// Alarm sound — Web Audio API synthesized beeps, no audio file required
// ---------------------------------------------------------------------------

const ALARM_CONFIG = {
  normal:    { beeps: 1, interval: 20000 },
  urgent:    { beeps: 2, interval: 10000 },
  emergency: { beeps: 3, interval:  7000 },
};

// Shared AudioContext — created once on first user gesture, reused thereafter.
// Browsers suspend audio until after a user interaction; resume() before playing.
let _audioCtx = null;

function _getAudioCtx() {
  if (!_audioCtx) {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return _audioCtx;
}

// Unlock audio on first click/keydown so the repeat timers can play freely.
['click', 'keydown', 'touchstart'].forEach(evt =>
  document.addEventListener(evt, () => {
    try { _getAudioCtx().resume(); } catch (e) {}
  }, { once: false, passive: true })
);

function _playBeeps(count) {
  if (!state.soundEnabled) return;
  try {
    const ctx = _getAudioCtx();
    ctx.resume().then(() => {
      const beepDuration = 0.15;  // seconds per beep
      const beepGap      = 0.1;   // seconds between beeps
      for (let i = 0; i < count; i++) {
        const t    = ctx.currentTime + i * (beepDuration + beepGap);
        const osc  = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type            = 'sine';
        osc.frequency.value = 880;
        gain.gain.setValueAtTime(0.6, t);
        gain.gain.setValueAtTime(0,   t + beepDuration);
        osc.start(t);
        osc.stop(t + beepDuration + 0.01);
      }
    });
  } catch (e) { /* audio not available */ }
}

function playAlert(priority) {
  const cfg = ALARM_CONFIG[priority] || ALARM_CONFIG.normal;
  _playBeeps(cfg.beeps);
}

// Per-priority repeat timers — each priority runs independently
const _alarmTimers = {};

function _startAlarmRepeat(priority) {
  if (_alarmTimers[priority]) return;  // already ticking
  const cfg = ALARM_CONFIG[priority];
  if (!cfg) return;
  _alarmTimers[priority] = setInterval(() => {
    const hasActive = Object.values(state.calls).some(
      c => c.status !== 'cleared' && c.priority === priority
    );
    if (hasActive) {
      _playBeeps(cfg.beeps);
    } else {
      _stopAlarmRepeat(priority);
    }
  }, cfg.interval);
}

function _stopAlarmRepeat(priority) {
  clearInterval(_alarmTimers[priority]);
  delete _alarmTimers[priority];
}

// ---------------------------------------------------------------------------
// Staff Messaging
// ---------------------------------------------------------------------------

const _chatMessages = [];  // in-memory ring buffer

function _appendChatMessage(data, isEmergency = false) {
  _chatMessages.push({ ...data, isEmergency });

  const list = document.getElementById('chatMessages');
  if (!list) return;

  document.getElementById('chatEmpty')?.remove();

  const time = new Date(data.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const div = document.createElement('div');
  div.className = 'chat-msg' + (isEmergency ? ' is-emergency' : '');
  div.innerHTML = `
    <div class="chat-msg-header">
      <span class="chat-msg-user">${esc(data.username)}</span>
      <span class="chat-msg-time">${time}</span>
    </div>
    <div class="chat-msg-body">${esc(data.message)}</div>`;
  list.appendChild(div);

  if (state.currentPage === 'messages') _scrollChatToBottom();
}

function _scrollChatToBottom() {
  const el = document.getElementById('chatMessages');
  if (el) el.scrollTop = el.scrollHeight;
}

async function sendStaffMessage() {
  const input = document.getElementById('chatInput');
  const msg = input?.value.trim();
  if (!msg) return;
  input.value = '';
  await api('/api/staff/message', 'POST', { message: msg });
}

// ---------------------------------------------------------------------------
// Staff Emergency
// ---------------------------------------------------------------------------

function showEmergencyModal() {
  const ta = document.getElementById('emergencyMessage');
  if (ta) ta.value = '';
  openModal('emergencyModal');
}

async function submitStaffEmergency() {
  const msg = document.getElementById('emergencyMessage')?.value.trim();
  if (!msg) { toast('Please describe the emergency.', 'error'); return; }
  closeModal('emergencyModal');
  await api('/api/staff/emergency', 'POST', { message: msg });
}

function _showEmergencyBanner(data) {
  const banner = document.getElementById('emergencyBanner');
  const body   = document.getElementById('emergencyBannerBody');
  if (!banner || !body) return;
  const time = new Date(data.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  body.textContent = `${data.username}: ${data.message}  (${time})`;
  banner.classList.remove('hidden');
}

function dismissEmergencyBanner() {
  document.getElementById('emergencyBanner')?.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Coordinator Monitor
// ---------------------------------------------------------------------------

const MON_MAX_LINES = 500;

function updateCoordinatorStatus(status) {
  const dot   = document.getElementById('monDot');
  const label = document.getElementById('monStatusLabel');
  if (!dot) return;
  const labels = {
    connected:    'Coordinator: Connected',
    connecting:   'Coordinator: Connecting…',
    disconnected: 'Coordinator: Disconnected',
    disabled:     'Coordinator: Disabled',
    error:        'Coordinator: Error',
  };
  dot.className = `ws-dot ${status === 'connected' ? 'connected' : status === 'disabled' ? 'disconnected' : 'error'}`;
  label.textContent = labels[status] || `Coordinator: ${status}`;
}

function handleMonitorLine(data) {
  // Only capture when logging is enabled
  if (!document.getElementById('monLoggingEnabled')?.checked) return;

  // Track stats
  state.monStats.total++;
  if (!data.parsed || !data.parsed.device_id) state.monStats.unknown++;
  if (data.is_call) state.monStats.calls++;

  // Track unique device IDs
  const devId = data.parsed && data.parsed.device_id;
  if (devId) {
    if (!state.monSeenDevices[devId]) {
      state.monSeenDevices[devId] = { count: 0, isCall: false };
      state.monStats.devices++;
    }
    state.monSeenDevices[devId].count++;
    if (data.is_call) state.monSeenDevices[devId].isCall = true;
    // Mark if registered
    state.monSeenDevices[devId].isKnown = state.devices.some(d => d.device_id === devId);
  }

  // Store line
  const idx = state.monLines.length;
  state.monLines.push({ ...data, idx });
  if (state.monLines.length > MON_MAX_LINES) state.monLines.shift();

  // Only render if the monitor sub-tab is currently visible
  if (_monitorTabVisible()) {
    appendMonitorRow(data, idx);
    updateMonitorStats();
    renderSeenDevices();
  }
}

function appendMonitorRow(data, idx) {
  const log = document.getElementById('monitorLog');
  if (!log) return;

  // Remove empty state
  const empty = log.querySelector('.mon-empty');
  if (empty) empty.remove();

  const callsOnly    = document.getElementById('monCallsOnly')?.checked;
  if (callsOnly && !data.is_call) return;

  const devIdFilter  = (document.getElementById('monDeviceFilter')?.value || '').trim();
  const p0           = data.parsed || {};
  const rowDevId     = p0.device_id || (p0.serial >= 0 ? String(p0.serial) : '');
  if (devIdFilter && !rowDevId.includes(devIdFilter)) return;

  const showHex = document.getElementById('monShowHex')?.checked;
  const ts = new Date(data.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const row = document.createElement('div');
  row.className = `mon-row${data.is_call ? ' is-call' : ''}${showHex ? ' show-hex' : ''}`;
  row.dataset.idx = idx;

  const p     = data.parsed || {};
  const devId = p.device_id || (p.serial >= 0 ? String(p.serial) : '');
  const ftype = p.class || p.format || '';
  const evts  = Array.isArray(p.events) && p.events.length ? p.events.join(' ') : (p.event || '');
  // Show hex as the primary "raw" content since these are binary frames
  const displayRaw = data.hex || data.raw || '';
  row.innerHTML = `
    <span class="mon-ts">${ts}</span>
    <span class="mon-raw">${esc(displayRaw)}
      ${ftype ? `<span class="mon-ftype">[${esc(ftype)}]</span>` : ''}
      ${devId ? `<span class="mon-devid">#${esc(devId)}</span>` : ''}
      ${evts  ? `<span class="mon-evts">${esc(evts)}</span>` : ''}
    </span>
    ${data.is_call ? '<span class="mon-call-badge">CALL</span>' : ''}
    <span class="mon-hex">${esc(data.hex || '')}</span>`;

  row.addEventListener('click', () => selectMonitorRow(row, data));
  log.appendChild(row);

  // Enforce max DOM rows
  while (log.children.length > MON_MAX_LINES) {
    log.removeChild(log.firstChild);
  }

  // Auto-scroll
  if (document.getElementById('monAutoScroll')?.checked) {
    log.scrollTop = log.scrollHeight;
  }

  // Update line count badge
  const badge = document.getElementById('monLineCount');
  if (badge) badge.textContent = `${state.monStats.total} lines`;
}

function selectMonitorRow(row, data) {
  document.querySelectorAll('.mon-row.selected').forEach(r => r.classList.remove('selected'));
  row.classList.add('selected');

  const insp = document.getElementById('monInspector');
  if (!insp) return;

  const p = data.parsed || {};
  const fields = [
    ['Time',       data.ts],
    ['Hex bytes',  data.hex || data.raw || '(none)'],
    ['Frame type', p.class  || p.format || 'unknown'],
    ['Device ID',  p.device_id || (p.serial >= 0 ? String(p.serial) : '—')],
    ['Events',     Array.isArray(p.events) ? (p.events.join(', ') || 'none') : (p.event || '—')],
    ['STAT1',      p.STAT1  || '—'],
    ['STAT0',      p.STAT0  || '—'],
    ['Level/RSSI', p.level  != null ? p.level : (p.rssi || '—')],
    ['Battery',    p.battery || '—'],
    ['PAL text',   p.text   || ''],
    ['Is call?',   data.is_call ? 'YES' : 'no'],
  ].filter(([, v]) => v !== '');

  insp.innerHTML = fields.map(([label, val]) => `
    <div class="inspector-field">
      <label>${label}</label>
      <div class="val${label === 'Is call?' && data.is_call ? ' call' : ''}">${esc(String(val))}</div>
    </div>`).join('');

  // Add "use this ID" button if device_id found
  if (p.device_id) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-primary btn-sm';
    btn.style.cssText = 'margin-top:8px;width:100%';
    btn.textContent = `Register device ${p.device_id}`;
    btn.onclick = () => { prefillDeviceModal(p.device_id); openDeviceModal(); };
    insp.appendChild(btn);
  }
}

function _monitorTabVisible() {
  return !document.getElementById('ctab-monitor')?.classList.contains('hidden');
}

function _flushMonitorBuffer() {
  const log = document.getElementById('monitorLog');
  if (!log) return;
  log.innerHTML = '';
  if (!state.monLines.length) {
    log.innerHTML = '<div class="mon-empty">No data received yet.</div>';
    return;
  }
  state.monLines.forEach((data, idx) => appendMonitorRow(data, idx));
  updateMonitorStats();
  renderSeenDevices();
  if (document.getElementById('monAutoScroll')?.checked) {
    log.scrollTop = log.scrollHeight;
  }
}

function updateMonitorStats() {
  const s = state.monStats;
  const el = id => document.getElementById(id);
  if (el('mstatTotal'))   el('mstatTotal').textContent   = s.total;
  if (el('mstatCalls'))   el('mstatCalls').textContent   = s.calls;
  if (el('mstatDevices')) el('mstatDevices').textContent = s.devices;
  if (el('mstatUnknown')) el('mstatUnknown').textContent = s.unknown;
  const badge = document.getElementById('monLineCount');
  if (badge) badge.textContent = `${s.total} lines`;
}

function renderSeenDevices() {
  const el = document.getElementById('monSeenDevices');
  if (!el) return;
  el.innerHTML = '';
  Object.entries(state.monSeenDevices).forEach(([devId, info]) => {
    // Refresh known status against current device list each render
    info.isKnown = state.devices.some(d => d.device_id === devId);
    const chip = document.createElement('span');
    chip.className = `seen-device-chip${info.isKnown ? ' is-known' : ''}`;
    if (info.isKnown) {
      const dev = state.devices.find(d => d.device_id === devId);
      chip.title = `${dev ? dev.name + ' — ' : ''}${info.count} packet(s) · registered (click to edit)`;
      chip.addEventListener('click', () => openEditDeviceModal(devId));
    } else {
      chip.title = `${info.count} packet(s) · unregistered (click to register)`;
      chip.addEventListener('click', () => { prefillDeviceModal(devId); openDeviceModal(); });
    }
    chip.textContent = devId;
    el.appendChild(chip);
  });
}

function clearMonitorLog() {
  state.monLines = [];
  state.monStats = { total: 0, calls: 0, devices: 0, unknown: 0 };
  state.monSeenDevices = {};
  const log = document.getElementById('monitorLog');
  if (log) log.innerHTML = '<div class="mon-empty">Log cleared.</div>';
  updateMonitorStats();
  renderSeenDevices();
}

// Re-render the full log when toggling hex/calls-only filters
function refreshMonitorLog() {
  const log = document.getElementById('monitorLog');
  if (!log) return;
  log.innerHTML = '';
  state.monLines.forEach((data, idx) => {
    appendMonitorRow(data, idx);
  });
  if (!log.children.length) {
    log.innerHTML = '<div class="mon-empty">No lines match current filter.</div>';
  }
}

document.getElementById('monCallsOnly')?.addEventListener('change', refreshMonitorLog);
document.getElementById('monDeviceFilter')?.addEventListener('input', refreshMonitorLog);
document.getElementById('devVendorType')?.addEventListener('change', function() {
  document.getElementById('auxLabelRow').style.display = this.value === 'arial_900' ? '' : 'none';
});
document.getElementById('monShowHex')?.addEventListener('change', () => {
  const showHex = document.getElementById('monShowHex').checked;
  document.querySelectorAll('.mon-row').forEach(r => r.classList.toggle('show-hex', showHex));
});

// ---------------------------------------------------------------------------
// Learn mode (auto-detect device ID)
// ---------------------------------------------------------------------------

async function startLearnMode() {
  if (state.learnActive) return;
  state.learnActive = true;

  const btn    = document.getElementById('btnDetect');
  const status = document.getElementById('learnStatus');
  const text   = document.getElementById('learnStatusText');

  if (btn)    btn.disabled = true;
  if (status) status.classList.remove('hidden');
  if (text)   text.textContent = 'Waiting — activate the device now…';

  // Use AbortController so we can cancel the fetch
  state.learnAbortCtrl = new AbortController();
  try {
    const res = await fetch('/api/devices/learn/start?timeout=30', {
      method: 'POST',
      signal: state.learnAbortCtrl.signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      toast(err.detail || 'Learn mode failed', 'error');
      _endLearnMode();
      return;
    }
    const data = await res.json();
    // handleDeviceSeen will also fire via WS, but handle the HTTP response too
    if (data.device_id) prefillDeviceId(data.device_id);
  } catch (err) {
    if (err.name !== 'AbortError') toast(`Learn mode error: ${err.message}`, 'error');
  }
  _endLearnMode();
}

function stopLearnMode() {
  if (state.learnAbortCtrl) state.learnAbortCtrl.abort();
  api('/api/devices/learn/stop', 'POST').catch(() => {});
  _endLearnMode();
}

function _endLearnMode() {
  state.learnActive = false;
  state.learnAbortCtrl = null;
  const btn    = document.getElementById('btnDetect');
  const status = document.getElementById('learnStatus');
  if (btn)    btn.disabled = false;
  if (status) status.classList.add('hidden');
}

function handleRepeaterSeen(serialNumber) {
  // Only act when the repeater add modal is open and learn mode is active for repeaters
  if (state.learnActive && state.learnTarget === 'repeater') {
    _prefillRepeaterSerial(serialNumber);
    _endRepeaterLearnMode();
  }
}

function handleDeviceSeen(deviceId, raw) {
  if (state.learnActive) {
    // Repeater learn mode is handled exclusively by coordinator.repeater_seen
    if (state.learnTarget === 'repeater') return;
    prefillDeviceId(deviceId);
    _endLearnMode();
    toast(`Device detected: ${deviceId}`, 'success');
  } else if (!document.getElementById('deviceModal').classList.contains('hidden')) {
    prefillDeviceId(deviceId);
    toast(`Device detected: ${deviceId}`, 'success');
  }
  // Also update the monitor's seen-device list
  if (!state.monSeenDevices[deviceId]) {
    state.monSeenDevices[deviceId] = { count: 0, isCall: true };
    state.monStats.devices++;
  }
  if (state.currentSettingsTab === 'innovonics') renderSeenDevices();
}

function prefillDeviceId(deviceId) {
  const field = document.getElementById('devId');
  if (field) {
    field.value = deviceId;
    field.classList.add('highlight-flash');
    setTimeout(() => field.classList.remove('highlight-flash'), 1200);
  }
  const text = document.getElementById('learnStatusText');
  if (text) text.textContent = `Detected: ${deviceId}`;
}

function prefillDeviceModal(deviceId) {
  document.getElementById('devId').value = deviceId;
}

// ---------------------------------------------------------------------------
// Mobile sidebar
// ---------------------------------------------------------------------------

function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebarOverlay').classList.add('visible');
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('visible');
}

// Close sidebar when a nav link is tapped on mobile
document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', () => {
    if (window.innerWidth < 768) closeSidebar();
  });
});

// ---------------------------------------------------------------------------
// Roam Alert — monitoring page
// ---------------------------------------------------------------------------

// State
const raState = {
  networks: [],
  doors:    [],
  tags:     [],
  codes:    [],
  events:   [],
};

// WS events push into the live feed
function handleRaWsEvent(data) {
  prependRaEvent(data);
  // Update door chip status in real time
  const chip = document.getElementById(`ra-door-chip-${data.door_id}`);
  if (chip) {
    const isAlarm = ['DAL', 'DBR'].includes(data.event_code);
    const isOpen  = data.event_code === 'DOP';
    const isClear = ['DOC', 'DAR', 'DRB', 'BCP'].includes(data.event_code);
    chip.classList.toggle('alarm', isAlarm);
    chip.classList.toggle('open',  isOpen && !isAlarm);
    if (isClear) { chip.classList.remove('alarm'); }
  }
}

function prependRaEvent(ev) {
  const feed = document.getElementById('raEventFeed');
  if (!feed) return;
  // Remove placeholder
  feed.querySelector('.hint')?.remove();

  const codeClass = {
    DAL: 'alarm', DBR: 'alarm',
    DAR: 'clear', DRB: 'clear', DOC: 'clear', BCP: 'clear',
    DOP: 'door',
    DBY: 'bypass',
  }[ev.event_code] || '';

  const row = document.createElement('div');
  row.className = 'ra-event-row';
  const t = ev.timestamp ? fmtDateTime(ev.timestamp) : '—';
  const detail = ev.resident
    ? `${esc(ev.resident)} — ${esc(ev.door_name || '')}`
    : esc(ev.door_name || '');
  row.innerHTML = `
    <span class="ra-event-time">${t}</span>
    <span class="ra-event-code ${codeClass}">${esc(ev.event_code)}</span>
    <span class="ra-event-detail">${detail}</span>`;
  feed.prepend(row);

  // Trim to 100 visible rows
  while (feed.children.length > 100) feed.lastChild.remove();
}

async function loadRoamAlertPage() {
  const [doors, events] = await Promise.all([
    api('/api/ra/doors'),
    api('/api/ra/events?limit=100'),
  ]);
  raState.doors  = doors  || [];
  raState.events = events || [];
  renderRaDoorGrid();
  renderRaEventFeed();
}

function renderRaDoorGrid() {
  const el = document.getElementById('raDoorGrid');
  if (!el) return;
  if (!raState.doors.length) {
    el.innerHTML = '<p class="hint">No door controllers configured.</p>';
    return;
  }
  el.innerHTML = '';
  raState.doors.forEach(d => {
    const chip = document.createElement('div');
    chip.className = `ra-door-chip ${d.online ? 'online' : 'offline'}`;
    chip.id = `ra-door-chip-${d.id}`;
    chip.innerHTML = `
      <div class="ra-door-name">${esc(d.name)}</div>
      <div class="ra-door-meta">${esc(d.serial_number)}${d.location ? ' · ' + esc(d.location) : ''}</div>
      <div class="ra-door-meta">${d.online ? 'Online' : 'Offline'}</div>`;
    el.appendChild(chip);
  });
}

function renderRaEventFeed() {
  const feed = document.getElementById('raEventFeed');
  if (!feed) return;
  feed.innerHTML = '';
  if (!raState.events.length) {
    feed.innerHTML = '<p class="hint ra-hint-pad">No events yet.</p>';
    return;
  }
  raState.events.forEach(ev => prependRaEvent(ev));
}

async function clearRaEvents() {
  if (!confirm('Clear all Roam Alert events?')) return;
  await api('/api/ra/events', 'DELETE');
  raState.events = [];
  renderRaEventFeed();
}

// ---------------------------------------------------------------------------
// Roam Alert — settings tab
// ---------------------------------------------------------------------------

async function loadRaSettings() {
  const [networks, doors, tags, codes, apts] = await Promise.all([
    api('/api/ra/networks'),
    api('/api/ra/doors'),
    api('/api/ra/tags'),
    api('/api/ra/codes'),
    api('/api/apartments/'),
  ]);
  if (apts) state.apartments = apts;
  raState.networks = networks || [];
  raState.doors    = doors    || [];
  raState.tags     = tags     || [];
  raState.codes    = codes    || [];
  renderRaNetworks();
  renderRaDoors();
  renderRaTags();
  renderRaCodes();
}

function renderRaNetworks() {
  const el = document.getElementById('raNetworksList');
  if (!el) return;
  el.innerHTML = raState.networks.length ? '' : '<p class="hint">No networks configured.</p>';
  raState.networks.forEach(n => {
    const card = document.createElement('div');
    card.className = 'config-card';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(n.name)} ${n.enabled ? '' : '<span class="tag tag-acknowledged">disabled</span>'}</div>
        <div class="config-card-sub">${esc(n.host)}:${n.port} · ${n.online ? '<span style="color:var(--ok)">Online</span>' : 'Offline'}</div>
      </div>
      <div class="config-card-actions">
        <button type="button" class="btn-secondary btn-sm" onclick="openRaNetworkModal(${n.id})">Edit</button>
        <button type="button" class="btn-secondary btn-sm" onclick="deleteRaNetwork(${n.id})">Remove</button>
      </div>`;
    el.appendChild(card);
  });
}

function renderRaDoors() {
  const el = document.getElementById('raDoorsList');
  if (!el) return;
  el.innerHTML = raState.doors.length ? '' : '<p class="hint">No door controllers configured.</p>';
  raState.doors.forEach(d => {
    const card = document.createElement('div');
    card.className = 'config-card';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(d.name)} ${d.enabled ? '' : '<span class="tag tag-acknowledged">disabled</span>'}</div>
        <div class="config-card-sub">SN: ${esc(d.serial_number)} · Network: ${esc(d.network_name || d.network_id)}${d.location ? ' · ' + esc(d.location) : ''}</div>
      </div>
      <div class="config-card-actions">
        <button type="button" class="btn-secondary btn-sm" onclick="openRaDoorModal(${d.id})">Edit</button>
        <button type="button" class="btn-secondary btn-sm" onclick="deleteRaDoor(${d.id})">Remove</button>
      </div>`;
    el.appendChild(card);
  });
}

function renderRaTags() {
  const el = document.getElementById('raTagsList');
  if (!el) return;
  el.innerHTML = raState.tags.length ? '' : '<p class="hint">No wander tags configured.</p>';
  raState.tags.forEach(t => {
    const card = document.createElement('div');
    card.className = 'config-card';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(t.resident_name || '(unnamed)')} ${t.enabled ? '' : '<span class="tag tag-acknowledged">disabled</span>'}</div>
        <div class="config-card-sub">Tag: ${esc(t.tag_serial)}${t.apartment_name ? ' · ' + esc(t.apartment_name) : ''}</div>
      </div>
      <div class="config-card-actions">
        <button type="button" class="btn-secondary btn-sm" onclick="openRaTagModal(${t.id})">Edit</button>
        <button type="button" class="btn-secondary btn-sm" onclick="deleteRaTag(${t.id})">Remove</button>
      </div>`;
    el.appendChild(card);
  });
}

function renderRaCodes() {
  const el = document.getElementById('raCodesList');
  if (!el) return;
  el.innerHTML = raState.codes.length ? '' : '<p class="hint">No keypad codes configured.</p>';
  raState.codes.forEach(c => {
    const card = document.createElement('div');
    card.className = 'config-card';
    card.innerHTML = `
      <div class="config-card-info">
        <div class="config-card-name">${esc(c.label || 'Code')} <span class="tag tag-acknowledged">${esc(c.code_type)}</span></div>
        <div class="config-card-sub">Door: ${esc(c.door_name || c.door_id)} · Slot ${c.slot} · ${'•'.repeat(c.code.length)}</div>
      </div>
      <div class="config-card-actions">
        <button type="button" class="btn-secondary btn-sm" onclick="sendRaCode(${c.id})">Send to Door</button>
        <button type="button" class="btn-secondary btn-sm" onclick="deleteRaCode(${c.id})">Remove</button>
      </div>`;
    el.appendChild(card);
  });
}

// ── Network modal ──────────────────────────────────────────────

function openRaNetworkModal(id) {
  const n = id ? raState.networks.find(x => x.id === id) : null;
  document.getElementById('raNetworkModalTitle').textContent = n ? 'Edit Network Controller' : 'Add Network Controller';
  document.getElementById('raNetworkEditId').value   = n ? n.id : '';
  document.getElementById('raNetworkName').value     = n ? n.name : '';
  document.getElementById('raNetworkHost').value     = n ? n.host : '';
  document.getElementById('raNetworkPort').value     = n ? n.port : 10001;
  document.getElementById('raNetworkEnabled').checked = n ? !!n.enabled : true;
  openModal('raNetworkModal');
}

async function submitRaNetwork() {
  const editId = document.getElementById('raNetworkEditId').value;
  const body = {
    name:    document.getElementById('raNetworkName').value.trim(),
    host:    document.getElementById('raNetworkHost').value.trim(),
    port:    +document.getElementById('raNetworkPort').value,
    enabled: document.getElementById('raNetworkEnabled').checked ? 1 : 0,
  };
  if (!body.name || !body.host) { toast('Name and host are required', 'error'); return; }
  if (editId) await api(`/api/ra/networks/${editId}`, 'PATCH', body);
  else        await api('/api/ra/networks', 'POST', body);
  closeModal('raNetworkModal');
  await loadRaSettings();
}

async function deleteRaNetwork(id) {
  if (!confirm('Remove this network controller?')) return;
  await api(`/api/ra/networks/${id}`, 'DELETE');
  await loadRaSettings();
}

// ── Door modal ─────────────────────────────────────────────────

function openRaDoorModal(id) {
  const d = id ? raState.doors.find(x => x.id === id) : null;
  document.getElementById('raDoorModalTitle').textContent = d ? 'Edit Door Controller' : 'Add Door Controller';
  document.getElementById('raDoorEditId').value    = d ? d.id : '';
  document.getElementById('raDoorName').value      = d ? d.name : '';
  document.getElementById('raDoorSerial').value    = d ? d.serial_number : '';
  document.getElementById('raDoorLocation').value  = d ? (d.location || '') : '';
  document.getElementById('raDoorSanity').checked  = d ? !!d.monitor_sanity : true;
  document.getElementById('raDoorEnabled').checked = d ? !!d.enabled : true;
  // Populate network select
  const sel = document.getElementById('raDoorNetworkId');
  sel.innerHTML = raState.networks.map(n =>
    `<option value="${n.id}" ${d && d.network_id === n.id ? 'selected' : ''}>${esc(n.name)}</option>`
  ).join('') || '<option value="">No networks</option>';
  openModal('raDoorModal');
}

async function submitRaDoor() {
  const editId = document.getElementById('raDoorEditId').value;
  const body = {
    network_id:     +document.getElementById('raDoorNetworkId').value,
    name:           document.getElementById('raDoorName').value.trim(),
    serial_number:  document.getElementById('raDoorSerial').value.trim(),
    location:       document.getElementById('raDoorLocation').value.trim() || null,
    monitor_sanity: document.getElementById('raDoorSanity').checked ? 1 : 0,
    enabled:        document.getElementById('raDoorEnabled').checked ? 1 : 0,
  };
  if (!body.name || !body.serial_number) { toast('Name and serial number are required', 'error'); return; }
  if (editId) await api(`/api/ra/doors/${editId}`, 'PATCH', body);
  else        await api('/api/ra/doors', 'POST', body);
  closeModal('raDoorModal');
  await loadRaSettings();
}

async function deleteRaDoor(id) {
  if (!confirm('Remove this door controller?')) return;
  await api(`/api/ra/doors/${id}`, 'DELETE');
  await loadRaSettings();
}

// ── Tag modal ──────────────────────────────────────────────────

function openRaTagModal(id) {
  const t = id ? raState.tags.find(x => x.id === id) : null;
  document.getElementById('raTagModalTitle').textContent = t ? 'Edit Wander Tag' : 'Add Wander Tag';
  document.getElementById('raTagEditId').value     = t ? t.id : '';
  document.getElementById('raTagSerial').value     = t ? t.tag_serial : '';
  document.getElementById('raTagResident').value   = t ? (t.resident_name || '') : '';
  document.getElementById('raTagEnabled').checked  = t ? !!t.enabled : true;
  // Populate apartment select
  const sel = document.getElementById('raTagApartment');
  sel.innerHTML = '<option value="">— None —</option>' +
    state.apartments.map(a =>
      `<option value="${a.id}" ${t && t.apartment_id === a.id ? 'selected' : ''}>${esc(a.name)}</option>`
    ).join('');
  openModal('raTagModal');
}

async function submitRaTag() {
  const editId = document.getElementById('raTagEditId').value;
  const aptVal = document.getElementById('raTagApartment').value;
  const body = {
    tag_serial:    document.getElementById('raTagSerial').value.trim().toLowerCase(),
    resident_name: document.getElementById('raTagResident').value.trim() || null,
    apartment_id:  aptVal ? +aptVal : null,
    enabled:       document.getElementById('raTagEnabled').checked ? 1 : 0,
  };
  if (!body.tag_serial) { toast('Tag serial required', 'error'); return; }
  if (editId) await api(`/api/ra/tags/${editId}`, 'PATCH', body);
  else        await api('/api/ra/tags', 'POST', body);
  closeModal('raTagModal');
  await loadRaSettings();
}

async function deleteRaTag(id) {
  if (!confirm('Remove this tag?')) return;
  await api(`/api/ra/tags/${id}`, 'DELETE');
  await loadRaSettings();
}

// ── Code modal ─────────────────────────────────────────────────

function openRaCodeModal() {
  document.getElementById('raCodeSlot').value  = 1;
  document.getElementById('raCodeValue').value = '';
  document.getElementById('raCodeLabel').value = '';
  document.getElementById('raCodeType').value  = 'access';
  const sel = document.getElementById('raCodeDoorId');
  sel.innerHTML = raState.doors.map(d =>
    `<option value="${d.id}">${esc(d.name)}</option>`
  ).join('') || '<option value="">No doors</option>';
  openModal('raCodeModal');
}

async function submitRaCode() {
  const body = {
    door_id:   +document.getElementById('raCodeDoorId').value,
    slot:      +document.getElementById('raCodeSlot').value,
    code:      document.getElementById('raCodeValue').value.trim(),
    label:     document.getElementById('raCodeLabel').value.trim() || null,
    code_type: document.getElementById('raCodeType').value,
  };
  if (!body.code || !/^\d{1,6}$/.test(body.code)) { toast('Code must be 1–6 digits', 'error'); return; }
  await api('/api/ra/codes', 'POST', body);
  closeModal('raCodeModal');
  await loadRaSettings();
}

async function sendRaCode(id) {
  const r = await api(`/api/ra/codes/${id}/send`, 'POST');
  if (r?.ok) toast('Code sent to door controller', 'success');
}

async function deleteRaCode(id) {
  if (!confirm('Remove this code?')) return;
  await api(`/api/ra/codes/${id}`, 'DELETE');
  await loadRaSettings();
}

async function reloadRaListener() {
  const r = await api('/api/ra/reload', 'POST');
  if (r?.ok) toast('Roam Alert listener restarted', 'success');
}

// ---------------------------------------------------------------------------
// Wander Management — sub-tab switching
// ---------------------------------------------------------------------------

function showWanderTab(tab) {
  // Toggle buttons
  document.getElementById('wtab-ra')?.classList.toggle('active', tab === 'ra');
  document.getElementById('wtab-ale')?.classList.toggle('active', tab === 'ale');
  // Toggle panels
  document.getElementById('wander-panel-ra')?.classList.toggle('hidden', tab !== 'ra');
  document.getElementById('wander-panel-ale')?.classList.toggle('hidden', tab !== 'ale');
  // Load ALE data when switching to ALE panel
  if (tab === 'ale') loadAlePanel();
}

// ---------------------------------------------------------------------------
// AeroScout ALE — status helpers (shared with settings badge)
// ---------------------------------------------------------------------------

function updateAleStatus(status) {
  const badge = (id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = status.charAt(0).toUpperCase() + status.slice(1);
    el.className = `ale-status-badge ${status}`;
  };
  badge('aleStatusBadge');
  badge('aleStatusBadgeSettings');
}

async function refreshAleStatus() {
  const r = await api('/api/aeroscout/status');
  if (r) updateAleStatus(r.status);
}

// Raw monitor helpers — used by /aeroscoutraw debug page via WS
const aleMonLines = [];

function appendAleMonitorRow(data) {
  const monitor = document.getElementById('aleMonitor');
  if (!monitor) return;
  monitor.querySelector('.hint')?.remove();
  aleMonLines.push(data);
  const row = document.createElement('div');
  row.className = 'ale-monitor-row';
  row.innerHTML =
    `<span class="ale-mon-ts">${esc(data.ts || '')}</span>` +
    `<span class="ale-mon-len">[${data.length}B]</span>` +
    `<span class="ale-mon-hex">${esc(data.hex || '')}</span>` +
    `<span class="ale-mon-txt">${esc(data.text || '')}</span>`;
  monitor.appendChild(row);
  monitor.scrollTop = monitor.scrollHeight;
  while (monitor.children.length > 500) monitor.firstChild.remove();
}

function clearAleMonitor() {
  aleMonLines.length = 0;
  const monitor = document.getElementById('aleMonitor');
  if (monitor) monitor.innerHTML = '<p class="hint ra-hint-pad">Waiting for data…</p>';
}

// ---------------------------------------------------------------------------
// AeroScout ALE — WanderGuard Blue panel
// ---------------------------------------------------------------------------

// In-memory caches keyed by device_id / mac
const _aleControllers = {};
const _aleTags        = {};

async function loadAlePanel() {
  await refreshAleStatus();
  const [devices, tags] = await Promise.all([
    api('/api/aeroscout/devices'),
    api('/api/aeroscout/tags'),
  ]);
  if (devices) {
    _aleControllers;  // reset
    Object.keys(_aleControllers).forEach(k => delete _aleControllers[k]);
    devices.forEach(d => { _aleControllers[d.device_id] = d; });
    renderAleControllers();
  }
  if (tags) {
    Object.keys(_aleTags).forEach(k => delete _aleTags[k]);
    tags.forEach(t => { _aleTags[t.mac] = t; });
    renderAleTags();
  }
}

// ── Controllers ──────────────────────────────────────────────────────────────

const _WANDER_MODELS = ['DC1000', 'EX5500', 'EX5700'];

function _controllerStatusClass(d) {
  const s = (d.general_status || '').toLowerCase();
  if (s === 'ok') return 'online';
  if (s.includes('unreach')) return 'offline';
  return '';
}

function _renderControllerCard(d) {
  const statusCls  = _controllerStatusClass(d);
  const statusText = d.general_status || 'Unknown';
  const secBadge   = d.security_enabled
    ? '<span class="ale-sec-badge secure">Secured</span>'
    : '<span class="ale-sec-badge open">Open</span>';
  const lastSeen   = d.last_seen ? fmtDate(d.last_seen) : '—';
  const alert      = d.last_alert_type
    ? `<div class="ale-ctrl-alert">${esc(d.last_alert_type)}: ${esc(d.last_alert_desc || '')}</div>`
    : '';

  const card = document.createElement('div');
  card.className = `ale-ctrl-card ${statusCls}`;
  card.id = `ale-ctrl-${d.device_id}`;
  card.innerHTML = `
    <div class="ale-ctrl-header">
      <span class="ale-ctrl-model">${esc(d.model || '?')}</span>
      <span class="ale-ctrl-status ${statusCls}">${esc(statusText)}</span>
    </div>
    <div class="ale-ctrl-name">${esc(d.name || d.device_id)}</div>
    <div class="ale-ctrl-meta">
      <span>MAC: ${esc(d.mac || '—')}</span>
      ${secBadge}
    </div>
    <div class="ale-ctrl-meta">Last seen: ${lastSeen}</div>
    ${alert}
    <div class="ale-ctrl-actions">
      <button class="btn-sm btn-secondary" title="Night Mode On"
        onclick="sendAleCmd('${esc(d.device_id)}','night_mode_on')">Night On</button>
      <button class="btn-sm btn-secondary" title="Night Mode Off"
        onclick="sendAleCmd('${esc(d.device_id)}','night_mode_off')">Night Off</button>
      <button class="btn-sm btn-secondary" title="Override On"
        onclick="sendAleCmd('${esc(d.device_id)}','override_on')">Override On</button>
      <button class="btn-sm btn-secondary" title="Override Off"
        onclick="sendAleCmd('${esc(d.device_id)}','override_off')">Override Off</button>
      <button class="btn-sm btn-danger" title="Restart controller"
        onclick="sendAleCmd('${esc(d.device_id)}','restart')">Restart</button>
    </div>`;
  return card;
}

function renderAleControllers() {
  const grid = document.getElementById('aleControllerGrid');
  if (!grid) return;
  grid.innerHTML = '';
  const devices = Object.values(_aleControllers)
    .filter(d => _WANDER_MODELS.includes(d.model))
    .sort((a, b) => (a.model + a.name).localeCompare(b.model + b.name));
  if (!devices.length) {
    grid.innerHTML = '<p class="hint ra-hint-pad">No WanderGuard Blue controllers discovered yet.</p>';
    return;
  }
  devices.forEach(d => grid.appendChild(_renderControllerCard(d)));
}

function upsertAleControllerCard(d) {
  _aleControllers[d.device_id] = d;
  const grid = document.getElementById('aleControllerGrid');
  if (!grid) return;
  if (!_WANDER_MODELS.includes(d.model)) return;
  grid.querySelector('.hint')?.remove();
  const existing = document.getElementById(`ale-ctrl-${d.device_id}`);
  const card = _renderControllerCard(d);
  if (existing) existing.replaceWith(card);
  else grid.appendChild(card);
}

async function sendAleCmd(deviceId, command) {
  const ctrl = _aleControllers[deviceId];
  const label = ctrl?.name || deviceId;
  if (ctrl?.security_enabled) {
    // Command will be sent but ALE may reject with StatusCode 107 — warn user
    toast(`${label} has security enabled — command sent but may be rejected by ALE`, 'error');
  }
  const r = await api(`/api/aeroscout/devices/${deviceId}/command`, 'POST', { command });
  if (r?.ok) toast(`"${command}" sent to ${label}`, 'success');
}

// ── Tags ─────────────────────────────────────────────────────────────────────

function _renderTagRow(t) {
  const lastSeen = t.last_seen ? fmtDate(t.last_seen) : '—';
  const zone     = [t.last_zone_id, t.last_map_id].filter(Boolean).join(' / ') || '—';
  const battery  = t.battery_status || '—';
  const tr = document.createElement('tr');
  tr.id = `ale-tag-${t.mac}`;
  tr.innerHTML = `
    <td><code>${esc(t.mac)}</code></td>
    <td>${esc(t.strap_address || '—')}</td>
    <td>${esc(t.resident_name || '—')}</td>
    <td>${lastSeen}</td>
    <td>${esc(zone)}</td>
    <td>${esc(battery)}</td>
    <td>
      <button class="btn-sm btn-secondary" onclick="openAleTagModal(${JSON.stringify(t)})">Edit</button>
    </td>`;
  return tr;
}

function renderAleTags() {
  const tbody = document.getElementById('aleTagBody');
  if (!tbody) return;
  tbody.innerHTML = '';
  const tags = Object.values(_aleTags)
    .sort((a, b) => (b.last_seen || '').localeCompare(a.last_seen || ''));
  if (!tags.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="hint" style="text-align:center;padding:1rem">No tags discovered yet.</td></tr>';
    return;
  }
  tags.forEach(t => tbody.appendChild(_renderTagRow(t)));
}

function upsertAleTagRow(t) {
  _aleTags[t.mac] = t;
  const tbody = document.getElementById('aleTagBody');
  if (!tbody) return;
  tbody.querySelector('.hint')?.closest('tr')?.remove();
  const existing = document.getElementById(`ale-tag-${t.mac}`);
  const row = _renderTagRow(t);
  if (existing) existing.replaceWith(row);
  else tbody.prepend(row);
}

// ── Tag modal ─────────────────────────────────────────────────────────────────

let _aleApartments = [];

async function openAleTagModal(tag = null) {
  // Load apartments for the select if not yet loaded
  if (!_aleApartments.length) {
    const apts = await api('/api/apartments');
    if (apts) _aleApartments = apts;
  }
  const sel = document.getElementById('aleTagApartment');
  sel.innerHTML = '<option value="">— None —</option>';
  _aleApartments.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.id;
    opt.textContent = a.name;
    sel.appendChild(opt);
  });

  const isEdit = tag && tag.mac;
  document.getElementById('aleTagModalTitle').textContent = isEdit ? 'Edit Wander Tag' : 'Add / Pre-seed Tag';
  document.getElementById('aleTagEditMac').value   = isEdit ? tag.mac : '';
  document.getElementById('aleTagMac').value        = isEdit ? tag.mac : '';
  document.getElementById('aleTagMacRow').classList.toggle('hidden', isEdit);
  document.getElementById('aleTagStrap').value      = tag?.strap_address  || '';
  document.getElementById('aleTagResident').value   = tag?.resident_name  || '';
  sel.value = tag?.apartment_id || '';
  openModal('aleTagModal');
}

async function submitAleTag() {
  const mac       = document.getElementById('aleTagEditMac').value || document.getElementById('aleTagMac').value;
  const strap     = document.getElementById('aleTagStrap').value.trim() || null;
  const resident  = document.getElementById('aleTagResident').value.trim() || null;
  const aptId     = +document.getElementById('aleTagApartment').value || null;

  if (!mac) { toast('MAC address is required', 'error'); return; }

  const isEdit = !!document.getElementById('aleTagEditMac').value;
  let r;
  if (isEdit) {
    r = await api(`/api/aeroscout/tags/${mac}`, 'PUT', { strap_address: strap, resident_name: resident, apartment_id: aptId });
  } else {
    r = await api('/api/aeroscout/tags', 'POST', { mac, strap_address: strap, resident_name: resident, apartment_id: aptId });
  }
  if (r) {
    upsertAleTagRow(r);
    closeModal('aleTagModal');
    toast('Tag saved', 'success');
  }
}

// ---------------------------------------------------------------------------
// AeroScout ALE — settings
// ---------------------------------------------------------------------------

async function loadAeroscoutSettings() {
  const cfg = await api('/api/aeroscout/config');
  if (!cfg) return;
  document.getElementById('aleHost').value      = cfg.host     || '';
  document.getElementById('alePort').value      = cfg.port     || 1411;
  document.getElementById('aleUsername').value  = cfg.username || '';
  document.getElementById('alePassword').value  = '';   // never pre-fill password
  document.getElementById('aleEnabled').checked = !!cfg.enabled;
  updateAleStatus(cfg.enabled ? 'disconnected' : 'disabled');
  refreshAleStatus();
}

async function saveAeroscoutConfig(e) {
  e.preventDefault();
  const body = {
    host:     document.getElementById('aleHost').value.trim() || null,
    port:     +document.getElementById('alePort').value || 1411,
    username: document.getElementById('aleUsername').value.trim() || null,
    password: document.getElementById('alePassword').value || null,
    enabled:  document.getElementById('aleEnabled').checked ? 1 : 0,
  };
  const r = await api('/api/aeroscout/config', 'PUT', body);
  if (r) toast('AeroScout config saved', 'success');
}

async function reloadAleListener() {
  const r = await api('/api/aeroscout/reload', 'POST');
  if (r?.ok) toast('AeroScout listener restarting…', 'success');
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

// Also allow pressing Enter in the login form
document.getElementById('loginPassword')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') submitLogin();
});

// Register PWA service worker
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

_bootAuth().then(() => { connectWS(); _initNotifications(); });

// ---------------------------------------------------------------------------
// OS Notifications
// ---------------------------------------------------------------------------
// Uses the Notification API + service worker showNotification() so notifications
// fire from the SW — no internet relay required, works when the PWA is
// backgrounded on Android/desktop as long as the browser is running.
// ---------------------------------------------------------------------------

function _initNotifications() {
  const btn = document.getElementById('notifBtn');
  if (!btn || !('Notification' in window)) return;
  btn.classList.remove('hidden');
  _updateNotifBtn();
}

function _updateNotifBtn() {
  const btn  = document.getElementById('notifBtn');
  const icon = document.getElementById('notifIcon');
  if (!btn) return;

  const perm = Notification.permission;
  if (perm === 'denied') {
    btn.title = 'Notifications blocked — allow them in browser/OS settings';
    btn.classList.add('notif-blocked');
    btn.classList.remove('notif-active');
    icon.setAttribute('fill', 'none');
  } else if (perm === 'granted') {
    btn.title = 'Notifications enabled — click to disable';
    btn.classList.add('notif-active');
    btn.classList.remove('notif-blocked');
    icon.setAttribute('fill', 'currentColor');
  } else {
    btn.title = 'Click to enable notifications';
    btn.classList.remove('notif-active', 'notif-blocked');
    icon.setAttribute('fill', 'none');
  }
}

async function togglePushNotifications() {
  const perm = Notification.permission;

  if (perm === 'denied') {
    toast('Notifications are blocked — allow them in your browser/OS settings, then reload.', 'warn');
    return;
  }

  if (perm === 'granted') {
    // Already granted — fire a test notification so user can confirm it works
    _showOsNotification({ id: 'test', device_name: 'Community Call', location: 'Notifications are working', priority: 'normal' });
    toast('Notifications are enabled. To disable, revoke permission in browser settings.', 'success');
    return;
  }

  // Request permission
  const result = await Notification.requestPermission();
  _updateNotifBtn();
  if (result === 'granted') {
    toast('Notifications enabled.', 'success');
    _showOsNotification({ id: 'test', device_name: 'Community Call', location: 'Notifications are working', priority: 'normal' });
  } else {
    toast('Notification permission denied.', 'warn');
  }
}

async function _showOsNotification(call) {
  if (Notification.permission !== 'granted') return;

  const priority = call.priority || 'normal';
  const label    = { emergency: 'EMERGENCY', urgent: 'URGENT' }[priority] || 'Call';
  const title    = `${label} — ${call.device_name || ''}`;
  const body     = call.location || '';

  const vibrate =
    priority === 'emergency' ? [200, 100, 200, 100, 200, 100, 200] :
    priority === 'urgent'    ? [200, 100, 200] : [200];

  // Absolute URL required — Android Chrome silently drops notifications if icon 404s
  const iconUrl = `${location.origin}/static/icons/icon-192.png`;
  const options = {
    body,
    icon:               iconUrl,
    badge:              iconUrl,
    tag:                `call-${call.id}`,
    renotify:           true,
    requireInteraction: priority === 'emergency',
    vibrate,
  };

  // Try SW registration first (shows even when tab is backgrounded)
  if ('serviceWorker' in navigator) {
    try {
      const reg = await navigator.serviceWorker.ready;
      await reg.showNotification(title, options);
      return;
    } catch (err) {
      console.warn('[CommCall] SW showNotification failed, falling back:', err);
    }
  }
  // Direct Notification fallback (tab must be visible)
  try {
    new Notification(title, options);
  } catch (err) {
    console.warn('[CommCall] Notification fallback failed:', err);
  }
}
