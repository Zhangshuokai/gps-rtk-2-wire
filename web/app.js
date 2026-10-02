'use strict';

/* ============================ 常量与工具 ============================ */

const SYS_COLORS = {
  GPS: '#38bdf8', BeiDou: '#4ade80', GLONASS: '#f87171', Galileo: '#facc15',
  QZSS: '#c084fc', NavIC: '#fb923c', SBAS: '#94a3b8', GNSS: '#38bdf8', '?': '#64748b',
};
const SYS_LABEL = {
  GPS: 'GPS', BeiDou: '北斗', GLONASS: 'GLONASS', Galileo: 'Galileo',
  QZSS: 'QZSS', NavIC: 'NavIC', SBAS: 'SBAS', GNSS: 'GNSS', '?': '未知',
};
const DASH = '—';

const $ = (id) => document.getElementById(id);

// 串口字节与设备字符串都是不可信输入，任何进入 innerHTML 的值都必须先转义
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));

const fmt = (v, digits = 2, dash = DASH) =>
  (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) ? dash : Number(v).toFixed(digits);
const raw = (v, dash = DASH) => (v === null || v === undefined || v === '') ? dash : v;

const systemColor = (system) => SYS_COLORS[system] || SYS_COLORS['?'];
const systemLabel = (system) => SYS_LABEL[system] || system || '未知';

function hexToRgba(hex, alpha) {
  const value = hex.replace('#', '');
  const bigint = parseInt(value, 16);
  return `rgba(${(bigint >> 16) & 255}, ${(bigint >> 8) & 255}, ${bigint & 255}, ${alpha})`;
}

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  try {
    const response = await fetch(path, options);
    return await response.json();
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

function setBadge(element, text, tone) {
  if (!element) return;
  element.textContent = text;
  element.className = 'badge' + (tone ? ' ' + tone : '');
}

function fitCanvas(canvas, cssWidth, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(cssWidth));
  const height = Math.max(1, Math.round(cssHeight));
  if (canvas._w !== width || canvas._h !== height || canvas._dpr !== dpr) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.height = height + 'px';
    canvas._w = width; canvas._h = height; canvas._dpr = dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

/* ============================ 全局状态 ============================ */

let snapshot = null;
let serverConfig = null;
let rawPaused = false;
let rawLastRendered = '';
const ackLines = [];
const MAX_ACK_LINES = 200;

// 分区渲染缓存：快照是 5Hz 推的，但卫星/告警/设备等数据变化慢得多，
// 不做变更检测会让 36 行表格和 3 个画布每秒重画 5 次。
const sectionCache = { satellites: '', alerts: '', device: '', logList: '' };
const SAT_RENDER_INTERVAL_MS = 400;
let lastSatRender = 0;
let pitchTrackWidth = 0;

function limitOf(key, fallback) {
  const limit = snapshot && snapshot.limit ? snapshot.limit : null;
  const value = limit ? limit[key] : undefined;
  return (value === null || value === undefined) ? fallback : value;
}

function setIfChanged(key, signature, render) {
  if (sectionCache[key] === signature) return;
  sectionCache[key] = signature;
  render();
}

function satelliteSignature(satellites) {
  const list = satellites.list || [];
  let accumulator = `${list.length}|${satellites.used}|${satellites.max_cn0}|`;
  for (const sat of list) {
    accumulator += `${sat.system}${sat.prn}${sat.cn0}${sat.used ? 1 : 0},`;
  }
  return accumulator;
}

/* ============================ 连接与串口 ============================ */

async function loadPorts(keepSelection = true) {
  const select = $('portSelect');
  const previous = keepSelection ? select.value : null;
  const data = await api('/api/ports');
  select.innerHTML = '';
  const ports = data.ports || [];
  if (!ports.length) {
    const option = document.createElement('option');
    option.textContent = '（未发现串口）';
    option.value = '';
    select.append(option);
    return;
  }
  for (const port of ports) {
    const option = document.createElement('option');
    option.value = port.device;
    const tags = [];
    if (port.suggested) tags.push('推荐');
    if (port.bluetooth) tags.push('蓝牙虚拟口');
    option.textContent = `${port.device} — ${port.description || '未知设备'}${tags.length ? ' [' + tags.join('|') + ']' : ''}`;
    option.dataset.mode = port.mode || (port.simulated ? 'sim' : (port.replay ? 'replay' : 'serial'));
    select.append(option);
  }
  if (previous && ports.some((p) => p.device === previous)) {
    select.value = previous;
  } else {
    const first = ports.find((p) => p.suggested) || ports[0];
    select.value = first.device;
  }
}

async function doConnect() {
  const port = $('portSelect').value;
  if (!port) { pushAck('未选择串口'); return; }
  const option = $('portSelect').selectedOptions[0];
  const mode = (option && option.dataset.mode) || 'serial';
  const baud = parseInt($('baudSelect').value, 10);
  pushAck(`正在连接 ${port} @ ${baud} …`);
  const result = await api('/api/connect', {
    mode, port, baud,
    auto_reconnect: $('chkReconnect').checked,
    query_device: $('chkQuery').checked,
  });
  if (result.ok) {
    const label = { sim: '模拟器', replay: '报文回放' }[mode] || `${baud} bps`;
    pushAck(`已连接 ${port}（${label}）`);
  } else {
    pushAck(`连接失败：${result.error || '未知错误'}`);
  }
}

async function doIdentify() {
  const port = $('portSelect').value;
  if (!port) return;
  const option = $('portSelect').selectedOptions[0];
  if (option && (option.dataset.mode === 'sim' || option.dataset.mode === 'replay')) {
    pushAck('内置数据源无需试读');
    return;
  }
  const output = $('identifyOut');
  output.hidden = false;
  output.textContent = `正在试读 ${port} …`;
  const result = await api('/api/identify', { port, baud: parseInt($('baudSelect').value, 10) });
  if (result.error) {
    output.textContent = `试读失败：${result.error}`;
    return;
  }
  output.textContent =
    `串口：${result.port} @ ${result.baud}\n` +
    `读取：${result.bytes} 字节 / ${result.lines} 行（NMEA ${result.nmea} 条，Unicore ${result.unicore} 条）\n` +
    `判定：${result.ok ? '像 GNSS 接收机，可以连接' : '未发现 NMEA/Unicore 报文'}\n\n` +
    (result.samples || []).join('\n');
}

/* ============================ 顶栏 ============================ */

function renderTopbar() {
  if (!snapshot) return;
  const link = snapshot.link || {};
  const position = snapshot.position || {};
  const heading = snapshot.heading || {};

  const connected = (snapshot.mode && snapshot.mode !== 'offline');
  $('statLink').textContent = connected ? (link.active === false ? '无数据' : '正常') : '离线';
  $('statLink').className = 'stat-value ' + (connected ? (link.active === false ? 'bad' : 'ok') : '');

  const rates = snapshot.rates || {};
  const total = Object.values(rates).reduce((sum, value) => sum + (value || 0), 0);
  $('statRate').textContent = connected ? `${total.toFixed(1)} 条/秒` : DASH;

  $('statBytes').textContent = `${((link.rx_bytes || 0) / 1024).toFixed(1)} KB`;

  $('statHeading').textContent = heading.valid ? `${Number(heading.deg).toFixed(2)}°` : DASH;
  $('statFix').textContent = position.quality_text ? `${position.quality_text}` : DASH;

  const device = snapshot.device;
  $('subtitle').textContent = device && device.model
    ? `${device.model} · ${device.firmware || ''} · ${device.product || ''}`
    : (connected ? '已连接，等待设备信息…' : '等待连接…');

  $('footerLink').textContent = connected
    ? `${snapshot.mode} ${link.port || ''} @ ${link.baud || ''} · ${link.lines_read || 0} 行 / ${((link.bytes_read || 0) / 1024).toFixed(1)} KB`
    : '未连接';
  const flags = [];
  if (link.reconnects) flags.push(`重连 ${link.reconnects} 次`);
  if (link.dropped_binary_frames) flags.push(`丢弃二进制帧 ${link.dropped_binary_frames}`);
  if (link.dropped_bad_lines) flags.push(`丢弃坏报文 ${link.dropped_bad_lines}`);
  if (link.unknown_lines) flags.push(`未识别行 ${link.unknown_lines}`);
  if (link.checksum_checked) {
    flags.push(`NMEA 校验 ${link.checksum_checked} 条${link.checksum_errors ? ` / 错 ${link.checksum_errors}` : ' 全通过'}`);
  }
  if (link.device_fingerprint) flags.push(`指纹 ${link.device_fingerprint}`);
  $('footerNotes').textContent = flags.join(' · ');
  $('footerServer').textContent = serverConfig ? `服务器 v${serverConfig.version} · 运行 ${snapshot.uptime_s}s` : '';
}

/* ============================ 罗盘 ============================ */

function drawCompass() {
  const canvas = $('compass');
  const size = canvas.clientWidth || 320;
  const { ctx, width, height } = fitCanvas(canvas, size, size);
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(cx, cy) - 6;

  const heading = snapshot && snapshot.heading ? snapshot.heading : {};
  const valid = Boolean(heading.valid);
  const degrees = valid ? Number(heading.deg) : 0;

  ctx.save();
  ctx.translate(cx, cy);

  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.fillStyle = '#0d1420';
  ctx.fill();
  ctx.strokeStyle = '#24364f';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  for (let angle = 0; angle < 360; angle += 5) {
    const major = angle % 30 === 0;
    const middle = angle % 10 === 0;
    const length = major ? 14 : (middle ? 9 : 5);
    const position = (angle * Math.PI) / 180;
    const inner = radius - length;
    ctx.beginPath();
    ctx.moveTo(Math.sin(position) * inner, -Math.cos(position) * inner);
    ctx.lineTo(Math.sin(position) * radius, -Math.cos(position) * radius);
    ctx.strokeStyle = major ? '#4d6a92' : '#2b3f5c';
    ctx.lineWidth = major ? 1.6 : 1;
    ctx.stroke();

    if (major) {
      const labelRadius = radius - 30;
      ctx.fillStyle = angle % 90 === 0 ? '#cfe4ff' : '#7d93b0';
      ctx.font = `${angle % 90 === 0 ? 'bold 15px' : '11px'} ${'Consolas, monospace'}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const names = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
      ctx.fillText(names[angle] || String(angle), Math.sin(position) * labelRadius, -Math.cos(position) * labelRadius);
    }
  }

  const color = valid ? '#38bdf8' : '#3f4f66';
  ctx.save();
  ctx.rotate((degrees * Math.PI) / 180);
  ctx.beginPath();
  ctx.moveTo(0, -(radius - 46));
  ctx.lineTo(-11, 26);
  ctx.lineTo(0, 14);
  ctx.lineTo(11, 26);
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.shadowColor = valid ? 'rgba(56,189,248,.75)' : 'transparent';
  ctx.shadowBlur = 16;
  ctx.fill();
  ctx.restore();

  ctx.beginPath();
  ctx.arc(0, 0, valid ? 6 : 5, 0, Math.PI * 2);
  ctx.fillStyle = valid ? '#0b1017' : '#2a3648';
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.restore();
}

function renderHeading() {
  const heading = (snapshot && snapshot.heading) || {};
  const log = heading.log || {};
  const ths = heading.ths || {};

  $('headingValue').textContent = heading.valid ? Number(heading.deg).toFixed(2) : DASH;
  $('pitchValue').textContent = log.pitch_deg === null || log.pitch_deg === undefined
    ? DASH : `${Number(log.pitch_deg).toFixed(2)}°`;
  $('baselineValue').textContent = log.baseline_length_m === null || log.baseline_length_m === undefined
    ? DASH : `${Number(log.baseline_length_m).toFixed(4)} m`;
  $('headingSigma').textContent = log.heading_stddev_deg === null || log.heading_stddev_deg === undefined
    ? DASH : `${Number(log.heading_stddev_deg).toFixed(4)}°`;

  setBadge($('headingSrcBadge'), heading.source ? `来源 ${heading.source}` : '无有效航向', heading.valid ? 'ok' : 'warn');
  setBadge($('solBadge'), log.sol_status_text || '解状态未知', log.sol_tone || '');
  setBadge($('posTypeBadge'), log.pos_type_text || '解类型未知', log.pos_tone || '');

  $('thsValue').textContent = ths.valid
    ? `${Number(ths.deg).toFixed(4)}°（状态 ${ths.status}）`
    : `无效（状态 ${raw(ths.status)}）`;
  $('logHeadingValue').textContent = log.deg === null || log.deg === undefined
    ? DASH : `${Number(log.deg).toFixed(4)}°${log.valid ? '' : '（未收敛）'}`;

  const mismatch = heading.mismatch_deg;
  const element = $('mismatchValue');
  if (mismatch === null || mismatch === undefined) {
    element.textContent = '需要两路同时有效';
    element.style.color = '';
  } else {
    const limit = limitOf('heading_mismatch_deg', 2);
    element.textContent = `${Number(mismatch).toFixed(4)}°${mismatch > limit ? ' —— 超限' : '（一致）'}`;
    element.style.color = mismatch > limit ? 'var(--error)' : 'var(--ok)';
  }

  const pitch = Number(log.pitch_deg || 0);
  // 只在首次与窗口尺寸变化时测量，避免每帧读 clientWidth 触发同步重排
  if (!pitchTrackWidth) pitchTrackWidth = $('pitchBubble').parentElement.clientWidth || 200;
  const travel = Math.max(0, pitchTrackWidth / 2 - 12);
  const clamped = Math.max(-15, Math.min(15, pitch));
  const dot = $('pitchBubble');
  dot.style.transform = `translateX(${(clamped / 15) * travel}px)`;
  dot.style.background = Math.abs(clamped) > 10 ? '#fb923c' : '#4ade80';

  if (document.activeElement !== $('chkSwap')) {
    $('chkSwap').checked = Boolean(heading.swap_enabled);
  }

  drawCompass();
}
/* ============================ 定位 ============================ */

function renderPosition() {
  const position = (snapshot && snapshot.position) || null;
  const velocity = (snapshot && snapshot.velocity) || {};
  const satellites = (snapshot && snapshot.satellites) || {};

  if (!position) {
    setBadge($('fixBadge'), '无定位报文', 'warn');
  } else {
    setBadge($('fixBadge'), position.quality_text || '未知解', position.quality >= 4 ? 'fixed'
      : (position.quality >= 1 ? 'float' : 'warn'));
  }

  const lat = position && position.lat;
  const lon = position && position.lon;
  $('posLat').textContent = lat === null || lat === undefined ? DASH : Number(lat).toFixed(7);
  $('posLon').textContent = lon === null || lon === undefined ? DASH : Number(lon).toFixed(7);
  $('posAlt').textContent = position && position.alt != null ? `${Number(position.alt).toFixed(3)} m` : DASH;
  $('posSv').textContent = position && position.num_sv != null ? position.num_sv : DASH;
  $('posHdop').textContent = fmt(position && position.hdop, 2);
  $('posDiffAge').textContent = position && position.diff_age != null ? `${Number(position.diff_age).toFixed(1)} s` : DASH;
  $('posUtc').textContent = formatUtc(velocity.utc, velocity.date);

  const speedKmh = velocity.speed_kmh;
  $('posSpeed').textContent = speedKmh === null || speedKmh === undefined
    ? DASH : `${Number(speedKmh).toFixed(2)} km/h`;
  $('posCourse').textContent = velocity.course_deg === null || velocity.course_deg === undefined
    ? DASH : `${Number(velocity.course_deg).toFixed(2)}°`;
  $('posSource').textContent = position && position.source ? position.source : DASH;
  $('posSource').title = position && position.stale
    ? `数据已超过 ${limitOf('stale_after_s', 2)} 秒未更新`
    : '';
  $('posStation').textContent = (position && position.station) || DASH;
  const neu = velocity.vel_n_mps;
  $('posVelNeu').textContent = (neu === null || neu === undefined)
    ? DASH
    : `${fmt(velocity.vel_n_mps, 2)} / ${fmt(velocity.vel_e_mps, 2)} / ${fmt(velocity.vel_u_mps, 2)} m/s`;

  setBadge($('satCountBadge'), `${satellites.tracked || 0} 颗 / 参与解算 ${satellites.used || 0}`, '');
  const cn0Text = satellites.max_cn0 == null
    ? '无信号'
    : `最强 ${Number(satellites.max_cn0).toFixed(1)} / 平均 ${fmt(satellites.avg_cn0, 1)} dB-Hz`;
  setBadge($('cn0Badge'), cn0Text, '');
  setBadge($('satTableBadge'), `${satellites.tracked || 0} 颗`, '');
}

function formatUtc(utc, date) {
  if (!utc || String(utc).length < 6) return DASH;
  const text = String(utc).padStart(9, '0');
  const hh = text.slice(0, 2);
  const mm = text.slice(2, 4);
  const ss = text.slice(4);
  let result = `${hh}:${mm}:${ss}`;
  if (date && String(date).length === 6) {
    const day = date.slice(0, 2);
    const month = date.slice(2, 4);
    const year = `20${date.slice(4, 6)}`;
    result = `${year}-${month}-${day} ${result}`;
  }
  return result;
}

/* ============================ 星空图 ============================ */

function drawSkyplot() {
  const canvas = $('skyplot');
  const size = canvas.clientWidth || 440;
  const { ctx, width, height } = fitCanvas(canvas, size, size);
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(cx, cy) - 18;

  const satellites = ((snapshot && snapshot.satellites) || {}).list || [];

  ctx.save();
  ctx.translate(cx, cy);

  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.fillStyle = '#0a1018';
  ctx.fill();

  ctx.strokeStyle = '#26374e';
  ctx.lineWidth = 1;
  for (const elevation of [0, 30, 60]) {
    const ringRadius = radius * (90 - elevation) / 90;
    ctx.beginPath();
    ctx.arc(0, 0, ringRadius, 0, Math.PI * 2);
    ctx.strokeStyle = elevation === 0 ? '#3a5375' : '#22304a';
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(-radius, 0); ctx.lineTo(radius, 0);
  ctx.moveTo(0, -radius); ctx.lineTo(0, radius);
  ctx.strokeStyle = '#1d2941';
  ctx.stroke();

  ctx.fillStyle = '#6f86a6';
  ctx.font = '11px Consolas, monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('0°', 0, radius - 9);
  ctx.fillText('30°', 0, radius * 60 / 90 - 8);
  ctx.fillText('60°', 0, radius * 30 / 90 - 8);

  ctx.font = 'bold 14px Consolas, monospace';
  ctx.fillStyle = '#cfe4ff';
  ctx.fillText('N', 0, -radius - 9);
  ctx.fillText('S', 0, radius + 11);
  ctx.fillText('E', radius + 11, 0);
  ctx.fillText('W', -radius - 11, 0);

  for (const sat of satellites) {
    const elevation = sat.elev;
    const azimuth = sat.azim;
    if (elevation === null || elevation === undefined || azimuth === null || azimuth === undefined) continue;
    const normalized = radius * (90 - Math.max(0, Math.min(90, elevation))) / 90;
    const angle = ((azimuth - 90) * Math.PI) / 180;
    const x = normalized * Math.cos(angle);
    const y = normalized * Math.sin(angle);

    const cn0 = sat.cn0 == null ? 0 : sat.cn0;
    const dotRadius = 6 + Math.max(0, Math.min(1, (cn0 - 25) / 25)) * 6;
    const color = systemColor(sat.system);
    const used = Boolean(sat.used);

    ctx.beginPath();
    ctx.arc(x, y, dotRadius, 0, Math.PI * 2);
    ctx.fillStyle = used ? hexToRgba(color, 0.9) : 'rgba(90,110,140,.28)';
    ctx.fill();
    ctx.strokeStyle = used ? color : 'rgba(120,140,170,.5)';
    ctx.lineWidth = used ? 1.6 : 1;
    ctx.stroke();

    if (dotRadius >= 8) {
      ctx.fillStyle = used ? '#08121d' : '#8fa6c4';
      ctx.font = `bold ${Math.round(dotRadius * 0.95)}px Consolas, monospace`;
      ctx.fillText(String(sat.prn ?? ''), x, y + 0.5);
    }
  }

  if (!satellites.length) {
    ctx.fillStyle = '#5b6f8c';
    ctx.font = '13px sans-serif';
    ctx.fillText('暂无卫星数据', 0, 0);
  }

  ctx.restore();
}

function renderSkyLegend() {
  const satellites = (snapshot && snapshot.satellites) || {};
  const list = satellites.list || [];
  const bySystem = satellites.by_system || {};
  const systems = [...new Set(list.map((s) => s.system).filter(Boolean))];
  const legend = $('skyLegend');
  legend.innerHTML = '';
  const entries = systems.length ? systems : Object.keys(bySystem);
  for (const system of entries) {
    const count = bySystem[system] ? bySystem[system].total : 0;
    const span = document.createElement('span');
    span.innerHTML = `<i class="sys-dot" style="background:${systemColor(system)}"></i>`
      + `${esc(systemLabel(system))} ${count}`;
    legend.append(span);
  }
  const note = document.createElement('span');
  note.innerHTML = '<i class="sys-dot" style="background:rgba(90,110,140,.28);border:1px solid rgba(120,140,170,.6)"></i>未参与解算';
  legend.append(note);
}

/* ============================ 信噪比 ============================ */

function drawSnrChart() {
  const canvas = $('snrChart');
  const satellites = ((snapshot && snapshot.satellites) || {}).list || [];
  const rows = satellites.filter((s) => s.cn0 !== null && s.cn0 !== undefined);
  const rowHeight = 19;
  // 高度按 4 行取整：卫星数小幅波动时不重新分配画布后备存储（几十 MB 的反复分配）
  const height = Math.max(6, Math.ceil(rows.length / 4) * 4) * rowHeight + 34;
  const width = canvas.clientWidth || 900;
  const { ctx } = fitCanvas(canvas, width, height);

  const labelWidth = 104;
  const axisHeight = 24;
  const chartWidth = Math.max(60, width - labelWidth - 46);
  const chartHeight = height - axisHeight - 6;
  const maxCn0 = 55;

  ctx.font = '10px Consolas, monospace';
  ctx.textBaseline = 'middle';

  for (let value = 0; value <= maxCn0; value += 10) {
    const x = labelWidth + (value / maxCn0) * chartWidth;
    ctx.beginPath();
    ctx.moveTo(x, 4);
    ctx.lineTo(x, chartHeight + 4);
    ctx.strokeStyle = value === 0 ? '#3a5375' : '#1e2b41';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = '#5b6f8c';
    ctx.textAlign = 'center';
    ctx.fillText(String(value), x, chartHeight + 16);
  }
  ctx.textAlign = 'left';
  ctx.fillStyle = '#5b6f8c';
  ctx.fillText('dB-Hz', labelWidth, 6);

  if (!rows.length) {
    ctx.fillStyle = '#5b6f8c';
    ctx.font = '13px sans-serif';
    ctx.fillText('暂无卫星信号数据', labelWidth, chartHeight / 2);
    return;
  }

  rows.forEach((sat, index) => {
    const y = 4 + index * rowHeight;
    const barHeight = rowHeight - 6;
    const cn0 = Math.max(0, Math.min(maxCn0, sat.cn0));
    const barWidth = (cn0 / maxCn0) * chartWidth;
    const color = systemColor(sat.system);
    const used = Boolean(sat.used);

    ctx.fillStyle = used ? hexToRgba(color, 0.85) : hexToRgba(color, 0.3);
    ctx.fillRect(labelWidth, y, Math.max(1, barWidth), barHeight);

    ctx.fillStyle = used ? '#c8dcf5' : '#7d93b0';
    ctx.font = '10.5px Consolas, monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`${systemLabel(sat.system)} ${sat.prn ?? ''}`, labelWidth - 8, y + barHeight / 2);

    ctx.textAlign = 'left';
    ctx.fillStyle = '#9fb4d0';
    ctx.fillText(cn0.toFixed(1), labelWidth + barWidth + 5, y + barHeight / 2);
  });
}

/* ============================ 卫星表 ============================ */

function renderSatTable() {
  const satellites = (snapshot && snapshot.satellites) || {};
  const list = satellites.list || [];
  const usedAvailable = satellites.used_source != null;
  const header = $('satUsedHeader');
  if (header) {
    header.textContent = usedAvailable ? '参与解算' : '参与解算（GSA 不可用）';
    header.title = usedAvailable ? '' : '本模块固件不上报 GSA/VTG，无法逐星判定是否入解';
  }
  const body = $('satTableBody');
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#5b6f8c">暂无卫星数据</td></tr>';
    return;
  }
  const rows = list.map((sat) => {
    const color = systemColor(sat.system);
    const used = !usedAvailable
      ? '<span class="used-no">—</span>'
      : (sat.used ? '<span class="used-yes">是</span>' : '<span class="used-no">否</span>');
    const inferred = sat.system_source === 'prn' ? ' <small title="星座由 PRN 号段推断">*</small>' : '';
    return `<tr>
      <td><i class="sys-dot" style="background:${color}"></i>${esc(systemLabel(sat.system))}${inferred}</td>
      <td>${raw(sat.prn)}</td>
      <td>${fmt(sat.elev, 1)}°</td>
      <td>${fmt(sat.azim, 1)}°</td>
      <td>${fmt(sat.cn0, 1)}</td>
      <td>${used}</td>
    </tr>`;
  });
  body.innerHTML = rows.join('');
}

/* ============================ 告警 / 设备 / 记录 ============================ */

function renderAlerts() {
  const alerts = (snapshot && snapshot.alerts) || [];
  const list = $('alertList');
  const levels = { error: 0, warn: 0, info: 0 };
  alerts.forEach((alert) => { levels[alert.level] = (levels[alert.level] || 0) + 1; });

  const tone = levels.error ? 'error' : (levels.warn ? 'warn' : 'ok');
  setBadge($('alertBadge'),
    levels.error ? `${levels.error} 个错误` : (levels.warn ? `${levels.warn} 个警告` : '正常'), tone);

  list.innerHTML = alerts.map((alert) => {
    const level = ['info', 'warn', 'error'].includes(alert.level) ? alert.level : 'info';
    const detail = alert.detail ? `<small>${esc(alert.detail)}</small>` : '';
    return `<li class="alert ${level}"><b>${esc(alert.text)}</b>${detail}</li>`;
  }).join('') || '<li class="alert info">暂无提示</li>';
}

function renderDevice() {
  const device = (snapshot && snapshot.device) || {};
  $('devModel').textContent = raw(device.model);
  $('devFw').textContent = raw(device.firmware);
  $('devProduct').textContent = raw(device.product);
  $('devSerial').textContent = raw(device.serial);
  $('devDate').textContent = raw(device.build_date);
  const mode = (snapshot && snapshot.device_mode) || {};
  $('devMode').textContent = mode.text || DASH;
  $('devPort').textContent = ((snapshot && snapshot.module_ports) || []).join(' / ') || DASH;

  const config = (snapshot && snapshot.config) || {};
  const keys = Object.keys(config).sort();
  $('configCount').textContent = String(keys.length);
  $('configList').textContent = keys.map((key) => config[key]).sort().join('\n') || '（尚未查询，可发送 CONFIG 命令）';
}

function renderRecorder() {
  const recorder = (snapshot && snapshot.recorder) || {};
  $('recRawState').textContent = recorder.raw_active
    ? (recorder.raw_path || '').split(/[\\/]/).pop() : '未记录';
  $('recRawLines').textContent = recorder.raw_dropped
    ? `${recorder.raw_lines || 0}（丢弃 ${recorder.raw_dropped}）`
    : String(recorder.raw_lines || 0);
  $('recCsvState').textContent = recorder.csv_active
    ? (recorder.csv_path || '').split(/[\\/]/).pop() : '未记录';
  $('recCsvRows').textContent = String(recorder.csv_rows || 0);
}

/* ============================ 命令控制台 ============================ */

function pushAck(text) {
  const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  ackLines.push(`[${stamp}] ${text}`);
  while (ackLines.length > MAX_ACK_LINES) ackLines.shift();
  const element = $('ackLog');
  element.textContent = ackLines.join('\n');
  element.scrollTop = element.scrollHeight;
}

function renderPresets() {
  const row = $('presetRow');
  row.innerHTML = '';
  const presets = (serverConfig && serverConfig.presets) || [];
  for (const preset of presets) {
    const button = document.createElement('button');
    button.textContent = preset.name;
    button.title = preset.note || '';
    if (!preset.safe) button.className = 'unsafe';
    button.onclick = () => {
      const hasPortToken = preset.commands.some((line) => line.includes('{PORT}'));
      const portToken = currentModulePort();
      if (hasPortToken && !portToken) {
        pushAck(`⚠ 不能载入预设「${preset.name}」：尚未读到模块自报的串口名。`
          + '请先连接并点「查询设备信息」，再从「模块串口」下拉框确认实际目标端口——'
          + '程序不猜测、不写死端口。');
        return;
      }
      const commands = preset.commands.map((line) => line.split('{PORT}').join(portToken));
      $('cmdText').value = commands.join('\n');
      pushAck(`已载入预设「${preset.name}」到命令框（${commands.length} 条）—— 请核对后点「发送」。`);
      pushAck(`  说明：${preset.note || ''}`);
      if (hasPortToken) {
        pushAck(`  ✓ LOG/UNLOG 的目标端口取实际值：${portToken}（来自 LOGLIST，未写死）`);
      }
      pushAck('  ⚠ 该预设会修改模块配置，请确认参数正确。');
    };
    row.append(button);
  }
}

async function sendCommands() {
  const text = $('cmdText').value;
  if (!text.trim()) { pushAck('命令为空'); return; }
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  pushAck(`发送 ${lines.length} 条命令…`);
  const result = await api('/api/command', {
    text,
    eol: $('eolSelect').value,
    checksum: $('checksumSelect').value,
  });
  if (!result.ok && !result.results) {
    pushAck(`发送失败：${result.error || '未知错误'}`);
    return;
  }
  for (const item of result.results || []) {
    pushAck(item.ok ? `→ ${item.sent}` : `✗ ${item.error || '失败'}`);
  }
}

/* ============================ 原始报文 ============================ */

function renderRaw(lines) {
  const filter = $('rawFilter').value.trim().toUpperCase();
  const filtered = filter
    ? lines.filter((line) => line.toUpperCase().includes(filter))
    : lines;
  const signature = `${filtered.length}:${filtered[filtered.length - 1] || ''}`;
  if (signature === rawLastRendered) return;
  rawLastRendered = signature;

  const monitor = $('rawMonitor');
  monitor.innerHTML = filtered.map((line) => {
    let klass = 'l-other';
    if (line.startsWith('#')) klass = 'l-hash';
    else if (line.startsWith('$command')) klass = 'l-cmd';
    else if (line.startsWith('$')) klass = 'l-dollar';
    const escaped = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return `<span class="${klass}">${escaped}</span>`;
  }).join('\n');

  setBadge($('rawBadge'), `显示 ${filtered.length} / ${lines.length} 行`, '');
  if ($('chkRawAuto').checked && !rawPaused) monitor.scrollTop = monitor.scrollHeight;
}

/* ============================ 渲染总入口 ============================ */

function alertsSignature() {
  return (snapshot.alerts || []).map((a) => `${a.level}:${a.text}:${a.detail}`).join('|');
}

function deviceSignature() {
  const device = snapshot.device || {};
  const mode = snapshot.device_mode || {};
  return [device.model, device.firmware, device.product, device.serial,
    device.build_date, mode.text, (snapshot.module_ports || []).join(','),
    Object.keys(snapshot.config || {}).length].join('|');
}

function logListSignature() {
  const list = (snapshot && snapshot.log_list) || [];
  return `${snapshot && snapshot.active_module_port}|`
    + list.map((e) => `${e.port}:${e.name}:${e.period_s}`).join(',');
}

// 模块 LOGLIST：当前真实在输出的日志集合（判断定向/定位日志是否开启的权威依据）
function renderLogList() {
  const list = (snapshot && snapshot.log_list) || [];
  const active = snapshot && snapshot.active_module_port;
  const body = $('logListBody');
  if (!list.length) {
    setBadge($('logListBadge'), '未查询', '');
    body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#5b6f8c">'
      + '点「列出当前日志配置（只读）」查询</td></tr>';
    return;
  }
  const byPort = {};
  for (const entry of list) {
    if (!byPort[entry.port]) byPort[entry.port] = [];
    byPort[entry.port].push(entry);
  }
  const ports = Object.keys(byPort).sort();
  setBadge($('logListBadge'),
    `${list.length} 条 / ${ports.length} 个端口${active ? '（本连接 ' + active + '）' : ''}`, 'ok');
  const cells = [];
  for (const port of ports) {
    const isActive = Boolean(active) && port === active;
    for (const entry of byPort[port]) {
      const period = (entry.period_s === null || entry.period_s === undefined)
        ? DASH : `${entry.period_s} s`;
      cells.push(`<tr${isActive ? ' style="background:rgba(56,189,248,.07)"' : ''}>`
        + `<td>${esc(port)}${isActive ? ' ←' : ''}</td>`
        + `<td>${esc(entry.name)}</td><td>${period}</td></tr>`);
    }
  }
  body.innerHTML = cells.join('');
}

function renderLinkError() {
  const link = (snapshot && snapshot.link) || {};
  const message = link.identity_mismatch || link.last_error || '';
  const element = $('linkError');
  if (!message) {
    element.hidden = true;
    element.textContent = '';
    return;
  }
  element.hidden = false;
  element.textContent = message;
}

// 模块自报的串口名列表（COM1/COM2/COM3…）——用于 LOG/UNLOG 的目标端口
function renderModulePorts() {
  const select = $('modulePortSelect');
  const ports = ((snapshot && snapshot.module_ports) || []).slice();
  const active = snapshot && snapshot.active_module_port;
  // 不猜测端口：未读到模块自报串口名时给出占位并禁用，避免写死 COM1/COM3 而发错端口
  if (!ports.length) {
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '（未读取，请先查询设备信息）';
    select.append(option);
    select.disabled = true;
    select.dataset.signature = '';
    return;
  }
  select.disabled = false;
  const options = ports.slice();
  // 本连接实际所在的模块串口排到最前，作为默认选择
  if (active && options.includes(active)) {
    options.sort((a, b) => (a === active ? -1 : (b === active ? 1 : a.localeCompare(b))));
  }
  const signature = options.join(',');
  if (select.dataset.signature === signature) return;
  const previous = select.value;
  select.innerHTML = '';
  for (const port of options) {
    const option = document.createElement('option');
    option.value = port;
    option.textContent = port === active ? `${port}（本连接）` : port;
    select.append(option);
  }
  select.dataset.signature = signature;
  const preferred = [previous, active, options[0]].find((p) => p && options.includes(p));
  select.value = preferred || options[0];
}

function currentModulePort() {
  const chosen = $('modulePortSelect') && $('modulePortSelect').value;
  if (chosen) return chosen;
  if (snapshot && snapshot.active_module_port) return snapshot.active_module_port;
  const ports = (snapshot && snapshot.module_ports) || [];
  // 不写死兜底端口：未知就返回空串，由调用方拒绝而非猜测
  return ports[0] || '';
}

function renderAll() {
  if (!snapshot) return;
  // 顶栏与记录器只是几次 textContent 赋值，且展示的是实时递增的计数，每帧刷新
  renderTopbar();
  renderHeading();
  renderPosition();
  renderRecorder();
  renderModulePorts();
  renderLinkError();

  const satellites = snapshot.satellites || {};
  const signature = satelliteSignature(satellites);
  const now = (window.performance && performance.now) ? performance.now() : Date.now();
  if (signature !== sectionCache.satellites && now - lastSatRender > SAT_RENDER_INTERVAL_MS) {
    sectionCache.satellites = signature;
    lastSatRender = now;
    drawSkyplot();
    renderSkyLegend();
    drawSnrChart();
    renderSatTable();
  }

  setIfChanged('alerts', alertsSignature(), renderAlerts);
  setIfChanged('device', deviceSignature(), renderDevice);
  setIfChanged('logList', logListSignature(), renderLogList);
}

function handleEvent(event) {
  if (!event) return;
  if (event.type === 'ack') {
    pushAck(`${event.ok ? '✓' : '✗'} ${event.raw || `$command,${event.command},${event.response}`}`);
  } else if (event.type === 'device') {
    pushAck(`设备：${event.model} / ${event.firmware} / ${event.product} / SN ${event.serial}`);
  } else if (event.type === 'mode') {
    pushAck(`模式：${event.text}`);
  }
}

/* ============================ 数据流与初始化 ============================ */

function openStream() {
  const source = new EventSource('/api/stream');
  source.onmessage = (message) => {
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch (error) {
      return;
    }
    switch (payload.type) {
      case 'snapshot':
        snapshot = payload;
        renderAll();
        break;
      case 'event':
        handleEvent(payload.event);
        break;
      case 'link':
        if (payload.event === 'opened' || payload.event === 'disconnected' || payload.event === 'closed') {
          pushAck(`链路${payload.event === 'opened' ? '已建立' : (payload.event === 'disconnected' ? '中断' : '已关闭')}`);
        }
        break;
      case 'link_log':
        pushAck(`[链路] ${payload.text}`);
        break;
      case 'link_error':
        pushAck(`[错误] ${payload.message}`);
        break;
      case 'server_error':
        pushAck(`[服务端] ${payload.message}`);
        break;
      default:
        break;
    }
  };
  source.onerror = () => {
    $('footerNotes').textContent = 'SSE 数据流中断，浏览器正在自动重连…';
  };
  return source;
}

async function bootstrap() {
  serverConfig = await api('/api/config');
  await loadPorts(false);
  if (serverConfig && serverConfig.default_baud) {
    $('baudSelect').value = String(serverConfig.default_baud);
  }
  renderPresets();
  pushAck('界面就绪。选择串口后点「连接」；也可用「试读」先确认设备。');

  const initial = await api('/api/snapshot');
  if (initial && initial.link) { snapshot = initial; renderAll(); }

  openStream();

  setInterval(async () => {
    if (rawPaused) return;
    const data = await api('/api/recent_raw?limit=200');
    if (data && data.lines) renderRaw(data.lines);
  }, 1000);

  setInterval(loadPorts, 5000);
}

function bindEvents() {
  $('btnConnect').onclick = doConnect;
  $('btnDisconnect').onclick = async () => { await api('/api/disconnect', {}); pushAck('已断开'); };
  $('btnRefresh').onclick = () => loadPorts();
  $('btnIdentify').onclick = doIdentify;

  $('chkSwap').onchange = async (event) => {
    const result = await api('/api/heading/swap', { value: event.target.checked });
    pushAck(`航向/俯仰字段解释已${result.swap ? '交换' : '还原为默认顺序'}`);
  };

  $('btnSend').onclick = sendCommands;
  $('btnClearCmd').onclick = () => { $('cmdText').value = ''; };

  $('btnRecAll').onclick = async () => {
    const interval = parseFloat($('recInterval').value) || 0.2;
    await api('/api/record', { action: 'start_all', interval });
    pushAck(`已开始记录（原始日志 + CSV @ ${interval}s）`);
  };
  $('btnRecStop').onclick = async () => {
    await api('/api/record', { action: 'stop_all' });
    pushAck('已停止记录');
  };

  $('btnRawPause').onclick = () => {
    rawPaused = !rawPaused;
    $('btnRawPause').textContent = rawPaused ? '继续' : '暂停';
  };
  $('btnRawClear').onclick = () => {
    rawLastRendered = '';
    $('rawMonitor').innerHTML = '';
  };
  $('rawFilter').oninput = () => { rawLastRendered = ''; retryRaw(); };

  window.addEventListener('resize', () => {
    pitchTrackWidth = 0;
    sectionCache.satellites = '';
    lastSatRender = 0;
    if (snapshot) renderAll();
  });

  $('cmdText').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) sendCommands();
  });
}

async function retryRaw() {
  const data = await api('/api/recent_raw?limit=200');
  if (data && data.lines) renderRaw(data.lines);
}

bindEvents();
bootstrap();
