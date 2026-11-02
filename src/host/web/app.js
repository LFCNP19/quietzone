/* =====================================================================
   静域 QuietZone · 统计网页前端（052+ 轮改版：大气简洁）
   口径：页面上每个数字都能指回方案书 §4.11 的某个寄存器字段；
        页面自己只定义"判读阈值"（display_thresholds），并与 PL 阈值对照列出。
   数据来源只有一个：GET /api/state；没有第二个数据源，也没有本地缓存。
   ===================================================================== */
'use strict';

var $ = function (id) { return document.getElementById(id); };

var STATE_CN  = { IDLE: '待机', QUIET: '安静区', WARN: '注意区', ALARM: '干扰区' };
var LEVEL_CN  = { critical: '需立即处理', warning: '需要关注', info: '提示', ok: '正常' };
var LEVEL_ORD = { critical: 0, warning: 1, info: 2, ok: 3 };
var MIC_REG    = ['0xC8', '0xCC', '0xD0', '0xD4'];
var MIC_PK_REG = ['0xD8', '0xDC', '0xE0', '0xE4'];

var charts = {};
var lastPayload = null;
var failCount = 0;
var calendarState = {
  year: null,
  month: null,
  selected: null,
  available: {},
  data: null,
  requestSeq: 0
};

/* ================= 通用小工具 ================= */
function pad(n) { return (n < 10 ? '0' : '') + n; }
function isNum(v) { return !(v === null || v === undefined || v === '' || isNaN(v)); }
function d1(v) { return isNum(v) ? Number(v).toFixed(1) : '—'; }
function d2(v) { return isNum(v) ? Number(v).toFixed(2) : '—'; }
function iv(v) { return isNum(v) ? String(Math.round(Number(v))) : '—'; }
function db10(v) { return isNum(v) ? v / 10 : null; }
function q88(v) { return isNum(v) ? (Number(v) / 256).toFixed(3) : '—'; }

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function dur(sec) {
  if (!isNum(sec)) { return '—'; }
  var s = Math.max(0, Math.round(Number(sec)));
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  if (h) { return h + ' 小时 ' + pad(m) + ' 分'; }
  if (m) { return m + ' 分 ' + pad(ss) + ' 秒'; }
  return ss + ' 秒';
}

function fmtClock(ts) {
  var d = ts ? new Date(Number(ts) * 1000) : new Date();
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function hhmmss(iso) { return String(iso || '').slice(11); }

function agoText(sec) {
  if (!isNum(sec)) { return '时间未知'; }
  var s = Math.max(0, Math.round(Number(sec)));
  if (s < 5) { return '刚刚'; }
  if (s < 60) { return s + ' 秒前'; }
  if (s < 3600) { return Math.round(s / 60) + ' 分钟前'; }
  return Math.floor(s / 3600) + ' 小时 ' + Math.round((s % 3600) / 60) + ' 分钟前';
}

function orderedAttention(d) {
  return (d.attention || []).slice().sort(function (a, b) {
    return (LEVEL_ORD[a.level] === undefined ? 9 : LEVEL_ORD[a.level]) -
           (LEVEL_ORD[b.level] === undefined ? 9 : LEVEL_ORD[b.level]);
  });
}

/* 最近采样明细里的平均时间间隔；图形更新不再依赖外部缺失函数。 */
function pointInterval(labels) {
  if (!labels || labels.length < 2) { return null; }
  var diffs = [];
  for (var i = 1; i < labels.length; i++) {
    var a = String(labels[i - 1] || '').split(':').map(Number);
    var b = String(labels[i] || '').split(':').map(Number);
    if (a.length !== 3 || b.length !== 3 || a.concat(b).some(isNaN)) { continue; }
    var ta = a[0] * 3600 + a[1] * 60 + a[2];
    var tb = b[0] * 3600 + b[1] * 60 + b[2];
    var dt = tb - ta;
    if (dt < 0) { dt += 86400; }
    if (dt >= 0 && dt < 60) { diffs.push(dt); }
  }
  if (!diffs.length) { return null; }
  diffs.sort(function (x, y) { return x - y; });
  return diffs[Math.floor(diffs.length / 2)];
}

function toast(msg, ok) {
  var t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok ? 'ok' : 'bad');
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(function () { t.hidden = true; }, 4000);
}

function thSourceShort(k) {
  if (k === 'sound_loc') { return 'sound_loc UART 当前门控'; }
  if (k === 'pl') { return '旧 PL 阈值寄存器'; }
  if (k === 'demo') { return '模拟演示参数'; }
  return '尚未读到';
}
function thSourceLong(k) {
  if (k === 'sound_loc') { return '来自每一条 QZ 遥测行的 thr10 / floor10'; }
  if (k === 'pl') { return '来自旧 PL 寄存器回读'; }
  if (k === 'demo') { return '演示数据参数'; }
  return '尚未读到'; }


/* ================= 首屏大字 ================= */
function renderHero(d) {
  var s = d.snapshot || {};
  var st = s.state_name || 'IDLE';
  var now = db10(s.spl_now_db10);
  var sv = $('stateValue');
  sv.textContent = STATE_CN[st] || st;
  sv.className = 'heroValue st-' + st;
  $('stateSub').textContent = 'sound_loc 四态门控 · 已持续 ' + dur(s.state_dwell_sec) +
    (isNum(s.uart_seq) ? ' · 帧 ' + iv(s.uart_seq) : '');

  var cls = 'heroValue xl';
  if (st === 'ALARM') { cls += ' t-alarm'; }
  else if (st === 'WARN') { cls += ' t-warn'; }
  else if (st === 'QUIET') { cls += ' t-quiet'; }
  var sp = $('splValue');
  sp.textContent = d1(now);
  sp.className = cls;
  $('splSub').textContent = 'QZ spl10 ÷ 10 ｜ 固件门限 ' +
    d1(db10(s.gate_thr_dbfs10)) + ' dBFS ｜ 本底 ' +
    d1(db10(s.gate_floor_dbfs10)) + ' dBFS';

  var dom = d.mic ? ('麦 ' + (d.mic.dom + 1)) : '—';
  var rows = [
    ['10 秒均值', d1(db10(s.spl_avg_10s_db10)) + ' dB(A)', 'UART'],
    ['60 秒均值', d1(db10(s.spl_avg_60s_db10)) + ' dB(A)', 'UART'],
    ['10 秒峰值', d1(db10(s.spl_peak_10s_db10)) + ' dB(A)', 'UART'],
    ['安静指数', iv(s.quiet_index) + ' /100', '推导'],
    ['注意区升级', iv(s.disturb_cnt) + ' 次', '会话'],
    ['需干预时长', dur(s.occupy_sec), '会话'],
    ['最强声道', dom, '四麦']
  ];
  var html = '';
  rows.forEach(function (r) {
    html += '<div class="row"><span>' + esc(r[0]) +
      ' <span class="mono">(' + esc(r[2]) + ')</span></span><b>' + esc(r[1]) + '</b></div>';
  });
  $('heroSide').innerHTML = html;
}

/* ================= KPI ================= */
function kpiCard(label, value, unit, tag, tone) {
  return '<div class="kpi">' +
    '<div class="kpiLabel">' + esc(label) + '</div>' +
    '<div class="kpiValue ' + (tone ? 't-' + tone : '') + '">' + value +
    (unit ? '<small>' + esc(unit) + '</small>' : '') + '</div>' +
    '<div class="kpiTag">' + esc(tag) + '</div></div>';
}

function renderKpis(d) {
  var s = d.snapshot || {};
  var qi = s.quiet_index, sr = s.speech_ratio;
  var a10 = db10(s.spl_avg_10s_db10), a60 = db10(s.spl_avg_60s_db10);
  var pk = db10(s.spl_peak_10s_db10);
  var tone = s.state_name === 'ALARM' ? 'alarm' : (s.state_name === 'WARN' ? 'warn'
             : (s.state_name === 'QUIET' ? 'quiet' : 'info'));
  var qiTone = !isNum(qi) ? '' : (qi >= 70 ? 'quiet' : (qi >= 40 ? 'warn' : 'alarm'));
  var srTone = !isNum(sr) ? '' : (sr >= 70 ? 'quiet' : (sr >= 40 ? 'info' : 'warn'));
  var html = '';
  html += kpiCard('安静指数', iv(qi), '/100', '由当前 dB(A) 映射，仅用于展示', qiTone);
  html += kpiCard('10 秒均值', d1(a10), 'dB(A)', 'QZ spl10 的 10 秒滑动平均', tone);
  html += kpiCard('60 秒均值', d1(a60), 'dB(A)', 'QZ spl10 的 60 秒滑动平均', tone);
  html += kpiCard('10 秒峰值', d1(pk), 'dB(A)', '最近 10 秒最大 spl10', tone);
  html += kpiCard('注意区升级', iv(s.disturb_cnt), '次', '会话内由安静进入注意/干扰的次数',
    isNum(s.disturb_cnt) && s.disturb_cnt > 0 ? 'warn' : 'quiet');
  html += kpiCard('需干预时长', dur(s.occupy_sec), '',
    '状态为注意区或干扰区的时间', tone);
  html += kpiCard('最强声道', d.mic ? ('麦 ' + (d.mic.dom + 1)) : '—', '',
    d.mic && d.mic.imb_db !== null ? '相对差 ' + d1(d.mic.imb_db) + ' dB' : '四路当前 dBFS',
    'info');
  $('kpis').innerHTML = html;
}

/* ================= 状态占比 ================= */
function renderShare(d) {
  var sh = (d.stats || {}).state_share || {};
  var order = ['QUIET', 'IDLE', 'WARN', 'ALARM'];
  var bar = '', legend = '';
  order.forEach(function (k) {
    var v = Number(sh[k] || 0);
    bar += '<i class="s-' + k + '" style="width:' + v.toFixed(2) + '%"></i>';
    legend += '<span><i class="s-' + k + '"></i>' + esc(STATE_CN[k]) + ' ' + v.toFixed(1) + '%</span>';
  });
  $('shareBar').innerHTML = bar;
  $('shareLegend').innerHTML = legend +
    '<span><i></i>安静或更安静合计 ' + d1((d.stats || {}).pct_quiet_or_better) + '%</span>';
}

/* ================= 需要关注 ================= */
function renderFocus(d) {
  var list = orderedAttention(d);
  var bar = $('focusBar'), tag = $('focusTag'), title = $('focusTitle');
  var metric = $('focusMetric'), detail = $('focusDetail'), action = $('focusAction');
  if (!list.length) {
    bar.className = 'focusBar ok';
    tag.textContent = '状态正常';
    title.textContent = '当前没有需要处理的管理事件';
    metric.textContent = '';
    detail.textContent = '系统正在持续监测声压、四麦电平和声源方位。';
    action.textContent = '查看判读区域';
    return;
  }
  var first = list[0];
  var level = first.level === 'critical' ? 'critical' :
    (first.level === 'warning' ? 'warning' : 'info');
  bar.className = 'focusBar ' + level;
  tag.textContent = LEVEL_CN[first.level] || '管理提醒';
  title.textContent = first.title || '存在需要关注的事件';
  metric.textContent = first.metric || '';
  detail.textContent = first.action ? ('建议：' + first.action) :
    (first.detail || '请查看下方管理事项。');
  action.textContent = list.length > 1 ? ('查看 ' + list.length + ' 项处置建议') : '查看处置建议';
}

function renderAttention(d) {
  var list = orderedAttention(d);
  if (!list.length) {
    $('attList').innerHTML = '<div class="empty">暂无判读结果。页面刚启动时还没有形成' +
      '可判读的统计量，稍等几个采样周期即可。</div>';
    return;
  }
  var html = '';
  list.forEach(function (a) {
    html += '<div class="att ' + esc(a.level) + '">' +
      '<div class="attHead"><span class="attTitle">' + esc(a.title) +
      '<span class="levelTag">' + esc(LEVEL_CN[a.level] || a.level) + '</span></span>' +
      '<span class="attSince">' +
      (a.since_sec > 0 ? '已持续 ' + dur(a.since_sec) : hhmmss(a.since_iso)) + '</span></div>' +
      (a.metric ? '<div class="attMetric">' + esc(a.metric) + '</div>' : '') +
      (a.detail ? '<div class="attBody">' + esc(a.detail) + '</div>' : '') +
      (a.action ? '<div class="attAct">处理建议：' + esc(a.action) + '</div>' : '') +
      (a.since_sec > 0 ? '<div class="attSince" style="margin-top:6px">起始 ' +
        esc(a.since_iso) + '</div>' : '') +
      '</div>';
  });
  $('attList').innerHTML = html;
}

/* ================= 四路麦克风 ================= */
function renderMic(mic) {
  var box = $('micBars');
  if (!mic) {
    box.innerHTML = '<div class="empty">还没有收到 QZ 四麦字段 r0_10…r3_10。</div>';
    return;
  }
  var dbs = mic.rms_db10 || [], pct = mic.rms_pct || [], dom = mic.dom | 0;
  var html = '';
  for (var i = 0; i < 4; i++) {
    var w = Math.max(1.5, Math.min(100, Number(pct[i] || 0)));
    var value = dbs.length ? d1(dbs[i] / 10) + ' dBFS' : iv((mic.rms_raw || [])[i]);
    html += '<div class="micRow' + (i === dom ? ' dom' : '') + '">' +
      '<span class="micLabel">麦 ' + (i + 1) + '</span>' +
      '<span class="micTrack"><span class="micFill" style="width:' + w.toFixed(1) + '%"></span></span>' +
      '<span class="micVal" title="UART 字段 r' + i + '_10">' + value + '</span></div>';
  }
  html += '<div class="micNote">条长按四路线性幅度归一；数值直接来自 UART 的 r0_10…r3_10。' +
    '当前最强是 <b>麦 ' + (dom + 1) + '</b>' +
    (mic.imb_db !== null && mic.imb_db !== undefined ? '，四路最大差 <b>' + d1(mic.imb_db) + ' dB</b>' : '') +
    '。四麦应保持同一增益、同一平面，连续相差 12 dB 以上会进入关注项。</div>';
  box.innerHTML = html;
}

/* ================= 声源方位 ================= */
function compassSvg() {
  // 盘面只画一次；指针用 CSS transform 旋转，角度更新时连续过渡。
  // sound_loc 约定：0°=东，90°=北，逆时针为正 -> 屏幕旋转取负角。
  var g = '#e6e9ec', g2 = '#f0f2f4';
  var s = '<svg viewBox="0 0 176 176" role="img" aria-label="声源方位">';
  s += '<circle cx="88" cy="90" r="62" fill="none" stroke="' + g + '" stroke-width="1.5"/>';
  s += '<circle cx="88" cy="90" r="39" fill="none" stroke="' + g2 + '" stroke-width="1"/>';
  s += '<line x1="26" y1="90" x2="150" y2="90" stroke="' + g2 + '" stroke-width="1"/>';
  s += '<line x1="88" y1="28" x2="88" y2="152" stroke="' + g2 + '" stroke-width="1"/>';
  var ticks = [45, 135, 225, 315], i, tr, tx, ty;
  for (i = 0; i < ticks.length; i++) {
    tr = ticks[i] * Math.PI / 180;
    tx = 88 + 62 * Math.cos(tr);
    ty = 90 - 62 * Math.sin(tr);
    s += '<circle cx="' + tx.toFixed(1) + '" cy="' + ty.toFixed(1) + '" r="2" fill="#ccd5de"/>';
  }
  s += '<text x="88" y="18" text-anchor="middle" font-size="10" fill="#8d97a2">北 90°</text>';
  s += '<text x="172" y="94" text-anchor="end" font-size="10" fill="#8d97a2">东 0°</text>';
  s += '<text x="88" y="171" text-anchor="middle" font-size="10" fill="#8d97a2">南 -90°</text>';
  s += '<text x="4" y="94" font-size="10" fill="#8d97a2">西 ±180°</text>';
  s += '<g class="doaNeedle held">' +
    '<line x1="88" y1="90" x2="150" y2="90" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>' +
    '<circle cx="150" cy="90" r="5.5" fill="currentColor"/>' +
    '</g>';
  s += '<circle cx="88" cy="90" r="3.5" fill="#101820"/>';
  s += '</svg>';
  return s;
}

var doaView = null;      /* {needle, badge, rows}，盘面只建一次 */
var doaShownDeg = null;  /* 连续累积角，保证旋转走最短弧 */

function renderDoa(doa) {
  var box = $('doaBox');
  if (!doa || !doa.has_value) {
    box.innerHTML = '<div class="empty">近 6 秒没有达到定位门限的帧。请发出一次短促声音；静默期间页面保留上一次方位。</div>';
    doaView = null;
    doaShownDeg = null;
    return;
  }
  var dState = doa.state || null;
  var ang = isNum(doa.angle) ? Number(doa.angle) : null;
  var ageText = agoText(doa.age_sec);
  var ageSec = isNum(doa.age_sec) ? Number(doa.age_sec) : Infinity;
  var live = dState === 'confirmed';
  var weak = dState === 'weak';
  var stale = (dState === 'none') || (!dState && ageSec > 30);
  var badgeClass = live ? 'live' : (stale && !weak ? 'stale' : 'held');
  var badgeText = live ? ('已确认方位 · ' + ageText)
    : (weak ? ('待复核 · ' + ageText)
    : (stale ? ((ageSec > 30 ? '已过期 · ' : '待更新 · ') + ageText)
    : ('沿用上次方位 · ' + ageText)));

  if (!doaView) {
    box.innerHTML = '<div><div class="compass">' + compassSvg() + '</div>' +
      '<div class="doaBadge"></div>' +
      '<div class="muted doaLegend">0° = 东 · 90° = 北 · 逆时针为正 · 指针随最新解算帧更新</div></div>' +
      '<div class="doaRows"></div>';
    doaView = {
      needle: box.querySelector('.doaNeedle'),
      badge: box.querySelector('.doaBadge'),
      rows: box.querySelector('.doaRows')
    };
    doaShownDeg = ang;
    if (ang === null) { doaShownDeg = 0; }
  }

  if (ang !== null && doaShownDeg !== null) {
    var dAng = ang - doaShownDeg;
    while (dAng > 180) { dAng -= 360; }
    while (dAng < -180) { dAng += 360; }
    doaShownDeg += dAng;
    doaView.needle.style.transform = 'rotate(' + (-doaShownDeg).toFixed(2) + 'deg)';
  }
  if (doaView.needle.classList.contains('live') !== live) {
    doaView.needle.classList.toggle('live', live);
    doaView.needle.classList.toggle('held', !live);
  }
  doaView.badge.className = 'doaBadge ' + badgeClass;
  doaView.badge.textContent = badgeText;

  doaView.rows.innerHTML =
    '<div class="row"><span>方位角（最新帧）</span><b>' + (ang === null ? '—' : d1(ang) + '°') + '</b></div>' +
    '<div class="row"><span>复核窗口帧数（30 s）</span><b>' + (isNum(doa.frames) ? iv(doa.frames) + ' 帧' : '—') + '</b></div>' +
    '<div class="row"><span>角度离散度</span><b>' + (isNum(doa.spread_deg) ? d1(doa.spread_deg) + '°' : '—') + '</b></div>' +
    '<div class="row"><span>时延 t(' + (doa.pair_0 || '0-2') + ')</span><b>' + d2(doa.delay_x) + ' 采样点</b></div>' +
    '<div class="row"><span>时延 t(' + (doa.pair_1 || '1-3') + ')</span><b>' + d2(doa.delay_y) + ' 采样点</b></div>' +
    '<div class="row"><span>有效麦克风</span><b>' + micMaskText(doa.mask) + '</b></div>' +
    '<div class="row"><span>自检残差</span><b>' + (isNum(doa.resid_pct) ? d1(doa.resid_pct) + ' %' : '—') + '</b></div>' +
    '<div class="row"><span>阵列孔径比（实测/标称）</span><b>' + (isNum(doa.ap_ratio) && doa.ap_ratio > 0 ? d2(doa.ap_ratio) + '×' : '—') + '</b></div>' +
    '<div class="row"><span>本帧自检</span><b>' + (doa.ok ? '通过' : '未通过') + '</b></div>' +
    '<div class="row"><span>本页累计定位帧</span><b>' + iv(isNum(doa.host_total) ? doa.host_total : doa.solve_cnt) + ' 帧</b></div>' +
    '<div class="row"><span>最后更新</span><b>' + ageText + '</b></div>';
}

/* 麦克风健康掩码 -> 可读文字（bit i = CHi 有信号） */
function micMaskText(mask) {
  if (!isNum(mask)) return '—';
  var m = Number(mask) & 0xF;
  var dead = [];
  for (var i = 0; i < 4; i++) if (!((m >> i) & 1)) dead.push('CH' + i);
  if (!dead.length) return 'CH0-3 全部正常';
  return (4 - dead.length) + '/4 有效 · 无信号：' + dead.join(' ');
}

/* ================= 事件时间轴 ================= */
function renderEvents(d) {
  var evs = d.events || [];
  if (!evs.length) {
    $('eventList').innerHTML = '<div class="empty">本次会话还没有读到事件。事件只在状态发生变化' +
      '（有人落座 / 人员离开 / 变吵 / 恢复安静）时才产生，一直静止不动就不会有新事件。</div>';
    return;
  }
  var html = '';
  evs.slice(0, 40).forEach(function (e) {
    var lv = e.level || 'info';
    html += '<div class="ev">' +
      '<span class="t">' + esc(hhmmss(e.iso)) + '</span>' +
      '<span class="lv ' + esc(lv) + '">' + esc(LEVEL_CN[lv] || lv) + '</span>' +
      '<span>' + esc(e.text || e.name || '未知事件') +
      ' <span class="muted">事件码 ' + esc(e.code === undefined ? '?' : e.code) +
      ' · ' + esc(e.name || '') + ' · 进入 ' +
      esc(STATE_CN[e.level_after_name] || e.level_after_name || '?') + '</span></span></div>';
  });
  $('eventList').innerHTML = html;
}

/* ================= 明细 ================= */
function renderSummary(d) {
  var st = d.stats || {}, sv = d.server || {}, lm = st.loudest_minute, snap = d.snapshot || {};
  var rows = [
    ['页面运行时长', dur(st.session_s), '本进程启动至今'],
    ['固件帧号', iv(snap.uart_seq), 'UART QZ seq'],
    ['当前状态', STATE_CN[snap.state_name] || snap.state_name || '—', 'UART state'],
    ['10 秒固件 trust 占比', d1(snap.speech_ratio) + '%', '固件自检位 = 1 的帧占比'],
    ['当前固件门限', d1(db10(snap.gate_thr_dbfs10)) + ' dBFS', 'UART thr10 ÷ 10'],
    ['当前噪声本底', d1(db10(snap.gate_floor_dbfs10)) + ' dBFS', 'UART floor10 ÷ 10'],
    ['乒乓写/读计数', iv(snap.uart_ring_wr) + ' / ' + iv(snap.uart_ring_rd), 'AXI GPIO 握手账本，模 4'],
    ['会话内声压峰值', d1(st.peak_spl_db) + ' dB(A)', '页面收到的 spl10 最大值'],
    ['需干预时长', dur(st.occupy_sec) + '（占会话 ' + d1(st.occupy_ratio) + '%）', '注意区 + 干扰区'],
    ['安静或更安静占比', d1(st.pct_quiet_or_better) + '%', '待机 + 安静区'],
    ['最吵的一分钟', lm ? (lm.t + '（均值 ' + d1(lm.avg) + ' / 峰值 ' + d1(lm.max) + ' dB(A)）') : '—',
      lm ? ('该分钟内 ' + lm.loud_pct + '% 的页面采样处于注意区以上') : '还没有满一分钟的数据'],
    ['页面采样点', iv(sv.samples) + ' 个（每 ' + sv.poll_s + ' s 一次）', 'App 会话计数']
  ];
  var html = '<div class="sumbox">';
  rows.forEach(function (r) {
    html += '<div class="row" title="' + esc(r[2]) + '"><span>' + esc(r[0]) +
      '</span><b>' + esc(r[1]) + '</b></div>';
  });
  $('summaryBox').innerHTML = html + '</div>';
}

function renderSamples(d) {
  var s = d.series || {};
  var n = (s.t || []).length;
  var from = Math.max(0, n - 60);
  var step = pointInterval(s.t);
  var html = '<div class="hint">窗口内共 ' + n + ' 个采样点，下面列最新 ' + (n - from) +
    ' 个（最新在最上面）；服务端等间隔抽稀' +
    (step ? '，约 ' + step + ' s 一条' : '') + '。</div>';
  html += '<div class="scroll"><table class="tbl"><thead><tr>' +
    '<th>时间</th><th>SPL</th><th>10 s 均值</th><th>60 s 均值</th>' +
    '<th>定位可信率</th><th>安静指数</th><th>方位角</th>' +
    '</tr></thead><tbody>';
  for (var i = n - 1; i >= from; i--) {
    html += '<tr><td class="num">' + esc(s.t[i]) + '</td>' +
      '<td class="num">' + d1(s.spl_now ? s.spl_now[i] : null) + '</td>' +
      '<td class="num">' + d1(s.spl_avg_10s ? s.spl_avg_10s[i] : null) + '</td>' +
      '<td class="num">' + d1(s.spl_avg_60s ? s.spl_avg_60s[i] : null) + '</td>' +
      '<td class="num">' + iv(s.speech_ratio ? s.speech_ratio[i] : null) + '%</td>' +
      '<td class="num">' + iv(s.quiet_index ? s.quiet_index[i] : null) + '</td>' +
      '<td class="num">' + d1(s.doa_angle ? s.doa_angle[i] : null) + '°</td></tr>';
  }
  $('samplesBox').innerHTML = html + '</tbody></table></div>';
}

var REG_META = {
  CTRL: { note: '写控制位：bit0 软复位 / bit1 清统计 / bit2 暂停统计' },
  STATUS: { note: 'bit0 FFT 忙 · bit1 LCD 忙 · bit2 LED 忙 · [4:3] 状态 · bit5 事件溢出' },
  SPL_NOW: { scale: 10, unit: 'dB(A)' },
  CONF_NOW: { unit: '（0–255）' },
  SPL_AVG_10S: { scale: 10, unit: 'dB(A)' },
  SPL_PEAK_10S: { scale: 10, unit: 'dB(A)' },
  SPL_AVG_60S: { scale: 10, unit: 'dB(A)' },
  SPEECH_RATIO: { unit: '%' },
  DISTURB_CNT: { unit: '次' },
  OCCUPY_SEC: { unit: '秒' },
  QUIET_INDEX: { unit: '/100' },
  STATE_DWELL: { unit: '秒' },
  EV_DROP_CNT: { unit: '次' },
  MIC_DOM: { note: 'bit1:0 最强声道编号 · bit2 优势是否明显' },
  MIC_RMS1: { scale: 256, dec: 3, unit: '（Q8.8）' },
  MIC_RMS2: { scale: 256, dec: 3, unit: '（Q8.8）' },
  MIC_RMS3: { scale: 256, dec: 3, unit: '（Q8.8）' },
  MIC_RMS4: { scale: 256, dec: 3, unit: '（Q8.8）' },
  MIC_PK1: { note: '窗内原始码值（24 位 I2S 的高位）' },
  MIC_PK2: { note: '窗内原始码值' },
  MIC_PK3: { note: '窗内原始码值' },
  MIC_PK4: { note: '窗内原始码值' },
  DOA_ANGLE: { note: 'bit8:0 整数度（0 度 = +X 轴，逆时针为正）· bit9 有效 · bit10 新鲜（读后清零）' },
  DOA_DELAY_X: { scale: 256, dec: 3, unit: '采样点' },
  DOA_DELAY_Y: { scale: 256, dec: 3, unit: '采样点' },
  DOA_PEAK: { note: '[31:16] Y 轴互相关峰 / [15:0] X 轴互相关峰' },
  DOA_CNT: { unit: '次', note: '读后清零的解算计数' }
};

function renderRegisters(d) {
  var regs = d.registers || [], s = d.snapshot || {};
  var html = '<div class="scroll"><table class="tbl"><thead><tr>' +
    '<th>接口字段</th><th>来源</th><th>当前值</th><th>含义</th>' +
    '</tr></thead><tbody>';
  regs.forEach(function (r) {
    html += '<tr><td class="mono">' + esc(r.name) + '</td>' +
      '<td class="num muted">' + esc(r.off) + '</td>' +
      '<td class="num">' + esc(r.value === null || r.value === undefined ? '—' : r.value) + '</td>' +
      '<td class="muted">' + esc(r.desc || '') + '</td></tr>';
  });
  html += '</tbody></table></div>';
  html += '<div class="muted" style="margin-top:10px">UART 已解析 ' +
    iv(s.uart_lines_total) + ' 行，忽略启动横幅/调试行 ' + iv(s.uart_lines_ignored) +
    ' 行；最近一帧年龄 ' + d2(s.uart_age_sec) + ' s。</div>';
  html += '<div class="muted" style="margin-top:4px">BRAM 0x40000000 保存 2048×64 bit 乒乓样本；' +
    '握手 GPIO 0x41200000；屏幕/灯环 GPIO 0x41210000。</div>';
  $('regBox').innerHTML = html;
}

function renderThresholds(d) {
  var s = d.snapshot || {}, dt = d.display_thresholds || {};
  if (s._schema === 'sound_loc_uart') {
    var rows = [
      ['方位范围', '-180°…+180°', '0°=东，90°=北，逆时针为正'],
      ['本帧门限', d1(db10(s.gate_thr_dbfs10)) + ' dBFS', '由自适应噪声本底和下限共同决定'],
      ['噪声本底', d1(db10(s.gate_floor_dbfs10)) + ' dBFS', '固件逐帧更新'],
      ['注意 / 干扰展示阈值', '50.0 / 60.0 dB(A)', '已下调；实际状态仍叠加持续时长与回差'],
      ['人声原始 / 平滑置信度', iv(s.classifier_conf) + ' / ' + iv(s.classifier_effective_conf), '原始值经约 150 ms 平滑，进入 60、退出 48'],
      ['平滑后人声判定', s.classifier_speech ? '是人声' : '非人声', 'spklass=1 时人声累计增加'],
      ['累计计时', '人声 ' + iv(s.speech_accum_ms) + ' ms ｜ 其他 ' + iv(s.other_accum_ms) + ' ms ｜ 低于门限 ' + iv(s.below_threshold_ms) + ' ms', '注意区保持 4000 ms 进入干扰区；静默 2000 ms/3000 ms 分别退出注意区/干扰区'],
      ['频谱特征', '平坦 ' + d2((s.classifier_flatness_1000 || 0) / 1000) + ' ｜ 质心 ' + iv(s.classifier_centroid_hz) + ' Hz ｜ 通量 ' + d2((s.classifier_flux_1000 || 0) / 1000), '用于现场校准人声分类'],
      ['频谱特征（续）', '低频 ' + d2((s.classifier_low_ratio_1000 || 0) / 1000) + ' ｜ 峰值占比 ' + d2((s.classifier_peak_ratio_1000 || 0) / 1000) + ' ｜ 过零 ' + d1((s.classifier_zcr_10 || 0) / 10) + '%', 'UART v2 诊断字段'],
      ['定位可信判定', 't02² + t13² 偏离标称值 ≤ 50%', '固件原有 suspect 逻辑'],
      ['四麦失衡提醒', '≥12.0 dB 连续 8 次页面采样', '本页展示判读，不改固件门控'],
      ['串口断流提醒', '> 2.0 s 未收到 QZ 行', '本页展示判读'],
      ['采样率 / 帧长', '48828.125 Hz / 1024 点', 'sound_loc PL 采集接口']
    ];
    $('thBox').innerHTML = '<div class="hint">阈值直接取当前 UART 行的 thr10 / floor10；' +
      '下面的提醒条件只影响「需要关注」的措辞。</div>' +
      tblOf(['项目', '当前值 / 条件', '说明'], rows);
    return;
  }
  var t = d.thresholds || {};
  $('thBox').innerHTML = '<div class="hint">兼容旧寄存器数据源；sound_loc 串口模式显示实时门控字段。</div>';
}

function tblOf(head, rows) {
  var h = '<table class="tbl"><thead><tr>';
  head.forEach(function (x) { h += '<th>' + esc(x) + '</th>'; });
  h += '</tr></thead><tbody>';
  rows.forEach(function (r) {
    h += '<tr><td class="mono">' + esc(r[0]) + '</td><td class="num">' +
      esc(isNum(r[1]) ? r[1] : (r[1] === undefined || r[1] === null ? '—' : r[1])) +
      '</td><td class="muted">' + esc(r[2]) + '</td></tr>';
  });
  return h + '</tbody></table>';
}

/* ================= 噪音日历 ================= */
function dateKey(year, month, day) {
  return year + '-' + pad(month) + '-' + pad(day);
}

function todayKey() {
  var now = new Date();
  return dateKey(now.getFullYear(), now.getMonth() + 1, now.getDate());
}

function parseDateKey(text) {
  var p = String(text || '').split('-').map(Number);
  return new Date(p[0], p[1] - 1, p[2]);
}

function dateTitleCN(text) {
  var d = parseDateKey(text);
  var week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  return d.getFullYear() + ' 年 ' + (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日 · ' + week[d.getDay()];
}

function setCalendarMonth(dateText) {
  var d = parseDateKey(dateText);
  calendarState.year = d.getFullYear();
  calendarState.month = d.getMonth() + 1;
}

function renderCalendarMonth() {
  var year = calendarState.year, month = calendarState.month;
  if (!year || !month) { return; }
  $('calMonth').textContent = year + ' 年 ' + pad(month) + ' 月';
  var today = todayKey();
  var now = new Date();
  $('calNext').disabled = (year > now.getFullYear()) ||
    (year === now.getFullYear() && month >= now.getMonth() + 1);

  var first = new Date(year, month - 1, 1);
  var leading = (first.getDay() + 6) % 7;
  var start = new Date(year, month - 1, 1 - leading);
  var html = '';
  for (var i = 0; i < 42; i++) {
    var d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    var key = dateKey(d.getFullYear(), d.getMonth() + 1, d.getDate());
    var cls = ['calDay'];
    if (d.getMonth() + 1 !== month) { cls.push('outside'); }
    if (key === today) { cls.push('today'); }
    if (key === calendarState.selected) { cls.push('selected'); }
    if (calendarState.available[key]) { cls.push('has-data'); }
    html += '<button type="button" role="gridcell" class="' + cls.join(' ') +
      '" data-date="' + key + '" aria-label="' + esc(dateTitleCN(key)) +
      (calendarState.available[key] ? '，有数据' : '，无数据') + '">' + d.getDate() + '</button>';
  }
  $('calendarGrid').innerHTML = html;
}

function angleText(angle) {
  if (!isNum(angle)) { return '—'; }
  var value = Number(angle);
  if (Math.abs(Math.abs(value) - 180) < .05) { return '±180°'; }
  return (value > 0 ? '+' : '') + d1(value) + '°';
}

function polarPoint(cx, cy, radius, angleDeg) {
  var rad = angleDeg * Math.PI / 180;
  return { x: cx + radius * Math.cos(rad), y: cy - radius * Math.sin(rad) };
}

function ringSectorPath(cx, cy, innerRadius, outerRadius, startAngle, endAngle) {
  var a = polarPoint(cx, cy, outerRadius, startAngle);
  var b = polarPoint(cx, cy, outerRadius, endAngle);
  var c = polarPoint(cx, cy, innerRadius, endAngle);
  var d = polarPoint(cx, cy, innerRadius, startAngle);
  return 'M' + a.x.toFixed(2) + ' ' + a.y.toFixed(2) +
    ' A' + outerRadius + ' ' + outerRadius + ' 0 0 0 ' + b.x.toFixed(2) + ' ' + b.y.toFixed(2) +
    ' L' + c.x.toFixed(2) + ' ' + c.y.toFixed(2) +
    ' A' + innerRadius + ' ' + innerRadius + ' 0 0 1 ' + d.x.toFixed(2) + ' ' + d.y.toFixed(2) + ' Z';
}

function mixFrequencyBlue(t) {
  t = Math.max(0, Math.min(1, t));
  var r = Math.round(237 + (30 - 237) * t);
  var g = Math.round(243 + (90 - 243) * t);
  var b = Math.round(249 + (168 - 249) * t);
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

function renderDoaFrequency(doa, source) {
  doa = doa || {samples: 0, sectors: []};
  var sectors = doa.sectors || [];
  var maxCount = 0;
  sectors.forEach(function (sector) { maxCount = Math.max(maxCount, Number(sector.count || 0)); });
  var cx = 130, cy = 130, inner = 61, outer = 98;
  var svg = '<svg viewBox="0 0 260 260" role="img" aria-label="声源方位出现频率圆环">';
  svg += '<circle cx="' + cx + '" cy="' + cy + '" r="' + ((inner + outer) / 2).toFixed(1) +
    '" fill="none" stroke="#f0f2f4" stroke-width="' + (outer - inner) + '"/>';
  sectors.forEach(function (sector) {
    var count = Number(sector.count || 0);
    var intensity = count && maxCount ? .16 + .84 * Math.pow(count / maxCount, .62) : 0;
    var title = angleText(sector.start) + ' 至 ' + angleText(sector.end) +
      '：' + count + ' 次，占 ' + d1(sector.pct) + '%';
    svg += '<path class="doaSector" d="' + ringSectorPath(cx, cy, inner, outer,
      Number(sector.start), Number(sector.end)) + '" fill="' + mixFrequencyBlue(intensity) +
      '"><title>' + esc(title) + '</title></path>';
  });
  svg += '<circle cx="' + cx + '" cy="' + cy + '" r="' + inner +
    '" fill="#ffffff" stroke="#f0f2f4" stroke-width="1"/>';
  var labels = [
    [0, '东 0°', cx + 112, cy + 4, 'start'],
    [90, '北 90°', cx, cy - 111, 'middle'],
    [180, '西 ±180°', cx - 112, cy + 4, 'end'],
    [-90, '南 -90°', cx, cy + 122, 'middle']
  ];
  labels.forEach(function (item) {
    svg += '<text x="' + item[2] + '" y="' + item[3] + '" text-anchor="' + item[4] +
      '" font-size="10.5" fill="#8d97a2">' + item[1] + '</text>';
  });
  if (doa.samples) {
    svg += '<text x="' + cx + '" y="' + (cy - 6) + '" text-anchor="middle" font-size="10.5" fill="#8d97a2">峰值方向</text>';
    svg += '<text x="' + cx + '" y="' + (cy + 16) + '" text-anchor="middle" font-size="18" font-weight="600" fill="#1e5aa8">' +
      angleText(doa.peak_center) + '</text>';
    $('doaPeakText').textContent = '最大频次 ' + d1(doa.peak_pct) + '%';
    $('doaFrequencyNote').textContent = '可信定位 ' + iv(doa.samples) + ' 帧 · 10° 扇区 · ' +
      (source === 'event_log' ? '事件日志已固化' : '兼容历史主日志');
  } else {
    svg += '<text x="' + cx + '" y="' + (cy + 5) + '" text-anchor="middle" font-size="12" fill="#8d97a2">暂无可信方位</text>';
    $('doaPeakText').textContent = '无定位帧';
    $('doaFrequencyNote').textContent = '该日声压有记录，但尚无固件判定一致的定位结果。';
  }
  svg += '</svg>';
  $('doaRing').innerHTML = svg;
}

function renderCalendarDetail() {
  var selected = calendarState.selected || todayKey();
  var data = calendarState.data && calendarState.data.date === selected ? calendarState.data : null;
  $('dayTitle').textContent = dateTitleCN(selected);
  var summary = data ? (data.summary || {}) : {};
  var hasData = data && isNum(summary.samples) && summary.samples > 0;
  var empty = $('dayEmpty');
  var box = $('dayChartBox');
  var visuals = $('dayVisuals');

  if (!hasData) {
    $('daySummary').innerHTML = '';
    visuals.hidden = true;
    box.hidden = true;
    empty.hidden = false;
    empty.textContent = selected === todayKey()
      ? '今天还没有可展示的历史采样。服务启动后会每 10 秒写入一次；刚到页面时可以稍等片刻。'
      : '该日没有噪音历史记录。';
    $('dayFootnote').textContent = data && data.history_enabled === false
      ? '历史目录未启用，请通过 eval/start_jtag_dashboard.py 启动服务。'
      : '无数据的时间档不会画成 0 dB；它们会保留为空档。';
    return;
  }

  visuals.hidden = false;
  box.hidden = false;
  empty.hidden = true;
  var attention = Number(summary.attention_minutes || 0);
  var attCls = attention >= 60 ? ' alarm' : (attention > 0 ? ' warn' : '');
  $('daySummary').innerHTML =
    '<span class="dayStat">日均 <strong>' + d1(summary.avg_spl) + '</strong>dB(A)</span>' +
    '<span class="dayStat">峰值 <strong>' + d1(summary.peak_spl) + '</strong>dB(A)</span>' +
    '<span class="dayStat' + attCls + '">关注 <strong>' + d1(attention) + '</strong>分钟</span>' +
    '<span class="dayStat">覆盖 <strong>' + iv(summary.active_bins) + '</strong>个时段</span>';
  $('dayFootnote').textContent = '柱为 15 分钟平均声压，线为同档峰值；橙色/红色柱分别表示档内出现 WARN/ALARM。' +
    ' “关注分钟”按每个时段内 WARN 及以上采样的时间占比折算，用于管理概览。最近采样：' +
    (summary.latest_iso || '—');
  updateDayChart(data);
  renderDoaFrequency(data.doa, data.doa_source);
}

function loadCalendar(dateText, options) {
  options = options || {};
  dateText = dateText || calendarState.selected || todayKey();
  calendarState.selected = dateText;
  setCalendarMonth(dateText);
  renderCalendarMonth();
  renderCalendarDetail();
  var seq = ++calendarState.requestSeq;
  fetch('/api/calendar?date=' + encodeURIComponent(dateText), { cache: 'no-store' })
    .then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    })
    .then(function (data) {
      if (seq !== calendarState.requestSeq || data.date !== calendarState.selected) { return; }
      calendarState.data = data;
      calendarState.available = {};
      (data.available_dates || []).forEach(function (key) { calendarState.available[key] = true; });
      renderCalendarMonth();
      renderCalendarDetail();
    })
    .catch(function (error) {
      if (seq !== calendarState.requestSeq) { return; }
      calendarState.data = null;
      renderCalendarDetail();
      $('dayEmpty').textContent = '日历读取失败：' + error.message;
      if (!options.silent) { toast('日历读取失败：' + error.message, false); }
    });
}

function shiftCalendarMonth(delta) {
  var base = new Date(calendarState.year, calendarState.month - 1 + delta, 1);
  var now = new Date();
  if (base.getFullYear() > now.getFullYear() ||
      (base.getFullYear() === now.getFullYear() && base.getMonth() > now.getMonth())) { return; }
  var desired = parseDateKey(calendarState.selected);
  var maxDay = new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
  var d = new Date(base.getFullYear(), base.getMonth(), Math.min(desired.getDate(), maxDay));
  loadCalendar(dateKey(d.getFullYear(), d.getMonth() + 1, d.getDate()));
}

function initCalendar() {
  var today = todayKey();
  calendarState.selected = today;
  setCalendarMonth(today);
  renderCalendarMonth();
  renderCalendarDetail();
  $('calPrev').addEventListener('click', function () { shiftCalendarMonth(-1); });
  $('calNext').addEventListener('click', function () { shiftCalendarMonth(1); });
  $('calendarGrid').addEventListener('click', function (event) {
    var button = event.target.closest('[data-date]');
    if (!button) { return; }
    loadCalendar(button.getAttribute('data-date'));
  });
  loadCalendar(today, { silent: true });
  setInterval(function () {
    if (calendarState.selected === todayKey()) { loadCalendar(todayKey(), { silent: true }); }
  }, 30000);
}

/* ================= 图表 ================= */
function initCharts() {
  if (typeof Chart === 'undefined') { return; }
  Chart.defaults.animation = false;
  Chart.defaults.font.family = '-apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = '#8d97a2';
  var grid = { color: '#f0f2f4' };
  var axis = { grid: grid, border: { display: false },
               ticks: { maxRotation: 0, autoSkipPadding: 24, color: '#8d97a2' } };

  charts.spl = new Chart($('splChart'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'UART 当前 dB(A)', data: [], borderColor: '#1e5aa8', backgroundColor: '#1e5aa8',
        borderWidth: 2, pointRadius: 0, tension: .25 },
      { label: '10 s 均值', data: [], borderColor: '#1e8a5d', borderWidth: 1.5, pointRadius: 0, tension: .25 },
      { label: '60 s 均值', data: [], borderColor: '#8d97a2', borderWidth: 1.5, pointRadius: 0, tension: .25 },
      { label: '兼容旧数据源：WARN', data: [], borderColor: '#b07a10', borderWidth: 1, borderDash: [5, 4],
        pointRadius: 0 },
      { label: '兼容旧数据源：ALARM', data: [], borderColor: '#b03a2e', borderWidth: 1, borderDash: [5, 4],
        pointRadius: 0 }
    ] },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { boxWidth: 10, boxHeight: 2, usePointStyle: false, padding: 20, color: '#59646f' } } },
      scales: { x: axis, y: Object.assign({}, axis, { suggestedMin: 25, suggestedMax: 80,
        title: { display: true, text: 'dB(A)' } }) } }
  });

  charts.day = new Chart($('dayChart'), {
    type: 'bar',
    data: { labels: [], datasets: [
      { type: 'bar', label: '15 分钟平均', data: [], backgroundColor: '#1e5aa8',
        borderWidth: 0, barPercentage: .82, categoryPercentage: .96, order: 2 },
      { type: 'line', label: '15 分钟峰值', data: [], borderColor: '#101820',
        backgroundColor: '#101820', borderWidth: 1.5, pointRadius: 2, pointHoverRadius: 4,
        tension: .15, spanGaps: false, order: 1 }
    ] },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { boxWidth: 10, boxHeight: 2, usePointStyle: false,
          padding: 18, color: '#59646f' } },
        tooltip: { callbacks: {
          title: function (items) {
            var data = calendarState.data, i = items.length ? items[0].dataIndex : -1;
            return data && data.bins && data.bins[i] ? data.bins[i].range : '';
          },
          label: function (ctx) {
            return ctx.dataset.label + '：' + d1(ctx.parsed.y) + ' dB(A)';
          },
          afterBody: function (items) {
            var data = calendarState.data, i = items.length ? items[0].dataIndex : -1;
            var bin = data && data.bins ? data.bins[i] : null;
            if (!bin || !bin.samples) { return ''; }
            return ['WARN 及以上占比：' + d1(bin.warn_pct) + '%',
                    'ALARM 占比：' + d1(bin.alarm_pct) + '%'];
          }
        } }
      },
      scales: {
        x: Object.assign({}, axis, { ticks: { maxRotation: 0, autoSkipPadding: 18,
          color: '#8d97a2' } }),
        y: Object.assign({}, axis, { suggestedMin: 25, suggestedMax: 90,
          title: { display: true, text: 'dB(A)' } })
      }
    }
  });

}

function updateDayChart(data) {
  if (!charts.day || !data) { return; }
  var bins = data.bins || [];
  var colors = bins.map(function (bin) {
    if (!bin.samples) { return 'rgba(30,90,168,0)'; }
    if (bin.alarm_pct > 0) { return 'rgba(176,58,46,.72)'; }
    if (bin.warn_pct > 0) { return 'rgba(176,122,16,.68)'; }
    return 'rgba(30,90,168,.58)';
  });
  charts.day.data.labels = bins.map(function (bin) { return bin.time; });
  charts.day.data.datasets[0].data = bins.map(function (bin) { return bin.spl_avg; });
  charts.day.data.datasets[0].backgroundColor = colors;
  charts.day.data.datasets[1].data = bins.map(function (bin) { return bin.spl_peak; });
  charts.day.update('none');
}

function updateCharts(d) {
  if (!charts.spl) { return; }
  var s = d.series || {}, t = d.thresholds || {}, snap = d.snapshot || {};
  var w = db10(t.spl_warn), a = db10(t.spl_alarm);
  var labels = s.t || [];
  var showLines = snap._schema !== 'sound_loc_uart';

  charts.spl.data.labels = labels;
  charts.spl.data.datasets[0].data = s.spl_now || [];
  charts.spl.data.datasets[1].data = s.spl_avg_10s || [];
  charts.spl.data.datasets[2].data = s.spl_avg_60s || [];
  charts.spl.data.datasets[3].data = labels.map(function () { return showLines ? w : null; });
  charts.spl.data.datasets[4].data = labels.map(function () { return showLines ? a : null; });
  charts.spl.data.datasets[3].hidden = !showLines;
  charts.spl.data.datasets[4].hidden = !showLines;
  charts.spl.update('none');


}

/* ================= 主渲染 ================= */
function renderChrome(d) {
  var src = (d.server || {}).source || {};
  var badge = $('srcBadge');
  var srcText = String(src.label || '') + ' ' + String(src.note || '');
  badge.textContent = src.mode === 'serial' ? (/JTAG/i.test(srcText) ? 'JTAG 实时' : '串口实时') :
    (src.mode === 'board' ? '旧寄存器' : '模拟演示');
  $('srcNote').textContent = '数据源：' + (src.label || '—') +
    (src.addr && src.addr !== '—' ? ' ｜ ' + src.addr : '') +
    (src.note ? ' ｜ ' + src.note : '');
  $('demoBanner').hidden = (src.mode !== 'demo');
  document.querySelectorAll('[data-cmd]').forEach(function (el) {
    el.style.display = src.mode === 'serial' ? 'none' : '';
  });
  $('clock').textContent = fmtClock((d.server || {}).now);

  var err = $('errBanner');
  if (d.ok === false && d.error) {
    err.hidden = false;
    err.textContent = '读取 sound_loc 接口失败：' + d.error;
  } else {
    err.hidden = true;
  }

  $('footNote').textContent = '统计口径：sound_loc UART QZ CSV ｜ 门控来源：' +
    thSourceShort(d.thresholds_source) + ' ｜ 页面采样 ' + iv((d.server || {}).samples) +
    ' 点（每 ' + (d.server || {}).poll_s + ' s 一次）｜ 服务端时间 ' +
    ((d.server || {}).iso || '—');

}

/* 5 Hz 快通道：只碰位置感强的三块，DOM 开销小、不重排整页 */
function renderHot(d) {
  renderHero(d);
  renderMic(d.mic);
  renderDoa(d.doa);
}

/* 1 Hz 慢通道：统计卡与表格 */
function renderHeavy(d) {
  renderFocus(d);
  renderKpis(d);
  renderShare(d);
  renderAttention(d);
  renderEvents(d);
  renderSummary(d);
  renderSamples(d);
  renderRegisters(d);
  renderThresholds(d);
  updateCharts(d);
}

/* ================= 轮询 ================= */
var POLL_MS = 200;      /* 方位指针 5 Hz 刷新，肉眼连续 */
var HEAVY_EVERY = 5;    /* 统计卡/表格/图表 1 Hz */
var tickCount = 0;
var pollBusy = false;

function poll() {
  if (pollBusy) { return; }
  pollBusy = true;
  fetch('/api/state', { cache: 'no-store' })
    .then(function (r) { if (!r.ok) { throw new Error('HTTP ' + r.status); } return r.json(); })
    .then(function (d) {
      failCount = 0;
      lastPayload = d;
      $('linkDot').className = 'dot ' + (d.ok === false ? 'off' : 'on');
      $('linkText').textContent = d.ok === false ? '数据源异常' : '已连接';
      renderChrome(d);
      renderHot(d);
      if (tickCount % HEAVY_EVERY === 0) { renderHeavy(d); }
      tickCount++;
      pollBusy = false;
    })
    .catch(function (e) {
      pollBusy = false;
      failCount++;
      $('linkDot').className = 'dot off';
      $('linkText').textContent = '连接断开（第 ' + failCount + ' 次）';
    });
}

/* ================= 控制按钮 ================= */
function send(action, label) {
  var tips = {
    pause: '暂停后 PL 侧的 10 s / 60 s 窗口与占用时长都不再更新，页面数字会停住。确定暂停统计？',
    resume: '恢复统计：PL 侧继续累计窗口与时长。确定继续？',
    clear: '会清空 PL 里的当日计数（干扰事件数、有人时长）和事件 FIFO。确定清空统计？',
    soft_reset: '会对 PL 声学链做一次软复位，复位期间状态与统计会短暂归零。确定软复位？'
  };
  if (!window.confirm(tips[action] || ('确定执行 ' + label + '？'))) { return; }
  fetch('/api/control/' + action, { method: 'POST' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      toast(label + '：' + (res.j.msg || (res.ok ? '成功' : '失败')), res.ok && res.j.ok);
      poll();
    })
    .catch(function (e) { toast(label + ' 失败：' + e.message, false); });
}

window.addEventListener('DOMContentLoaded', function () {
  initCharts();
  initCalendar();
  document.querySelectorAll('[data-cmd]').forEach(function (el) {
    el.addEventListener('click', function () {
      send(el.getAttribute('data-cmd'), el.textContent.trim());
    });
  });
  poll();
  setInterval(poll, POLL_MS);
});
