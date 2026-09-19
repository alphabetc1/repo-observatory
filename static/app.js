'use strict';

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
const names = {hicache: 'HiCache', unified: 'Unified Radix Cache', hisparse: 'HiSparse'};
const types = {issue: 'Issue', pr: 'Pull request', rfc: 'RFC'};
const icons = {issue: '◉', pr: '⑂', rfc: '◇'};
const labels = {P0: '正确性与隔离', P1: '服务可用性', P2: '性能与容量', P3: '能力与观测'};
const pageSize = 16;
let snapshot = null;
let currentPage = 1;
let visibleEntries = [];
let loading = false;
let lastFocus = null;
let detailRequest = 0;
const params = new URLSearchParams(location.search);
const state = {
  module: ['all', ...Object.keys(names)].includes(params.get('module')) ? params.get('module') : 'all',
  window: ['week', 'month', 'sixmonths'].includes(params.get('window')) ? params.get('window') : 'week',
  type: ['all', ...Object.keys(types)].includes(params.get('type')) ? params.get('type') : 'all',
  priority: ['all', ...Object.keys(labels)].includes(params.get('priority')) ? params.get('priority') : 'all',
  sort: ['priority', 'updated', 'created', 'confidence'].includes(params.get('sort')) ? params.get('sort') : 'priority',
  followup: ['unassigned','conflict','base_failed','review_required','changes_requested','draft'].includes(params.get('followup')) ? params.get('followup') : 'all',
  owner: params.get('owner') || '',
  query: params.get('q') || ''
};

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
}

function formatDate(date, withTime = false) {
  return new Intl.DateTimeFormat('zh-CN', {month:'2-digit', day:'2-digit', ...(withTime ? {hour:'2-digit', minute:'2-digit', hour12:false} : {})}).format(new Date(date));
}

function boundary(window, date = new Date()) {
  const result = new Date(date);
  if (window === 'week') result.setDate(result.getDate() - 7);
  else {
    const day = result.getDate();
    result.setDate(1);
    result.setMonth(result.getMonth() - (window === 'month' ? 1 : 6));
    const end = new Date(result.getFullYear(), result.getMonth() + 1, 0).getDate();
    result.setDate(Math.min(day, end));
  }
  return result;
}

function priorityBadge(priority) {
  return `<span class="priority-badge priority-${priority}" title="${labels[priority]}">${priority}</span>`;
}

function moduleTags(modules) {
  return modules.map(key => `<span class="tag"><span class="module-dot ${key}"></span>${names[key]}</span>`).join('');
}

function confidenceLabel(confidence) {
  if (!confidence) return '';
  return `<span class="confidence" title="${escapeHTML(confidence.meaning)}"><span class="confidence-bars confidence-${confidence.level}" aria-hidden="true"><i></i><i></i><i></i></span>置信度 · ${confidence.label}</span>`;
}

const ciLabels = {success:'通过', failure:'失败', timed_out:'超时', action_required:'需操作', in_progress:'运行中', queued:'排队中', waiting:'等待中', requested:'等待中', pending:'等待中', not_run:'未运行', skipped:'已跳过', cancelled:'已取消', neutral:'中性结果', stale:'旧版本结果', unknown:'未知'};
const reviewLabels = {APPROVED:'已批准', CHANGES_REQUESTED:'需修改', REVIEW_REQUIRED:'待评审', UNREVIEWED:'未评审', REVIEW_ACTIVITY:'已有评审意见'};
function statusOld(w) {return !w?.checked_at || Date.now() - new Date(w.checked_at) > 45 * 60000;}
function ciText(w) {return w?.error ? '查询失败' : ciLabels[w?.base_ci?.state] || '待同步';}
function workflowRow(e) {
  const w = e.workflow;
  const owners = w?.assignees;
  const owner = owners ? owners.length ? '负责人 ' + owners.map(v=>'@'+v).join(' · ') : '未分配' : '负责人待同步';
  const old = statusOld(w);
  const ci = w?.base_ci;
  const ciBadge = e.is_pr ? `<span class="workflow-badge ${!old && !w?.error && ['failure','timed_out'].includes(ci?.state) ? 'status-danger' : !old && !w?.error && ci?.state==='success' ? 'status-success' : ''}">Base CI · ${ciText(w)}</span>` : '';
  return `<div class="entry-status"><span class="owner-label">${escapeHTML(owner)}</span>${e.is_pr ? `<span class="workflow-badge ${w?.conflict==='conflict' ? 'status-danger' : ''}">${{conflict:'有冲突',clear:'无冲突',unknown:'冲突检测中'}[w?.conflict] || '冲突待同步'}</span>${ci?.url ? `<a class="ci-run-link" href="${escapeHTML(ci.url)}" target="_blank" rel="noopener noreferrer" title="打开对应 Base CI 运行">${ciBadge} ↗</a>` : ciBadge}<span class="workflow-badge ${w?.review==='CHANGES_REQUESTED' ? 'status-warning' : ''}">${reviewLabels[w?.review] || '评审待同步'}</span>` : ''}<span class="workflow-time ${old || w?.error ? 'status-warning' : ''}">${w?.checked_at ? `${old ? '状态可能滞后 · ' : ''}${w.error ? '更新失败 · ' : ''}${formatDate(w.checked_at, true)}` : '状态尚未同步'}</span></div>`;
}
function matchesFollowup(e) {
  const w = e.workflow;
  if (state.owner && !(w?.assignees || []).some(v=>v.toLowerCase().includes(state.owner.toLowerCase().replace(/^@/,'')))) return false;
  switch (state.followup) {
    case 'unassigned': return Array.isArray(w?.assignees) && !w.assignees.length;
    case 'conflict': return e.is_pr && w?.conflict==='conflict';
    case 'base_failed': return e.is_pr && !w?.error && ['failure','timed_out','action_required'].includes(w?.base_ci?.state);
    case 'review_required': return e.is_pr && w?.review==='REVIEW_REQUIRED';
    case 'changes_requested': return e.is_pr && w?.review==='CHANGES_REQUESTED';
    case 'draft': return e.is_pr && e.draft;
    default: return true;
  }
}
function workflowDetails(e) {
  const w = e.workflow;
  const ci = w?.base_ci;
  return `<section class="workflow-details"><div class="workflow-heading"><h3 class="section-label">跟进状态</h3><button id="refresh-entry-status" class="status-refresh" ${e.status_refresh_enabled ? '' : 'disabled title="尚未配置 GitHub 状态查询凭据"'}>刷新状态 ↻</button></div>${workflowRow(e)}<p id="status-refresh-message" role="status" class="evidence-note">${e.status_refresh_enabled ? '每 30 分钟检查' : '定时刷新待配置，当前展示最近采集的状态'}；无冲突或 Base CI 通过不代表已满足全部合并条件。</p>${e.is_pr && w ? `<div class="workflow-facts"><span>目标分支 <strong>${escapeHTML(w.base_ref || '未知')}</strong></span><span>当前提交 <code>${escapeHTML(w.head_sha?.slice(0,10) || '未知')}</code></span><span>阶段 <strong>${e.draft ? 'Draft' : 'Ready for review'}</strong></span></div><p class="evidence-note">待评审人：${escapeHTML(w.reviewers?.map(v=>'@'+v).join('、') || '未指定')}</p>${ci?.head_sha ? `<p class="evidence-note">CI 对应提交：<code>${escapeHTML(ci.head_sha.slice(0,10))}</code>${ci.attempt ? ' · 第 '+ci.attempt+' 次运行' : ''}${ci.previous_state ? ' · 旧结果：'+escapeHTML(ciLabels[ci.previous_state] || ci.previous_state) : ''}${ci.updated_at ? ' · 运行更新于 '+formatDate(ci.updated_at,true) : ''}</p>` : ''}${ci?.failed_jobs?.length ? '<ul class="workflow-jobs">'+ci.failed_jobs.map(j=>`<li><a href="${escapeHTML(j.url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(j.name)} ↗</a><span>${escapeHTML(ciLabels[j.state] || j.state)}</span></li>`).join('')+'</ul>' : ''}${w.reviews?.length ? '<div class="review-list">'+w.reviews.map(r=>`<a href="${escapeHTML(r.url)}" target="_blank" rel="noopener noreferrer">@${escapeHTML(r.author)} · ${escapeHTML({APPROVED:'批准',CHANGES_REQUESTED:'要求修改',COMMENTED:'已评论',DISMISSED:'已撤销',PENDING:'未提交'}[r.state] || r.state)}${r.at ? ' · '+formatDate(r.at,true) : ''} ↗</a>`).join('')+'</div>' : ''}` : ''}</section>`;
}
async function refreshEntryStatus(number) {
  const button = $('#refresh-entry-status');
  const message = $('#status-refresh-message');
  button.disabled = true;
  message.textContent = '正在向 GitHub 查询当前状态…';
  try {
    const response = await fetch(`/api/entry/${number}/refresh-status`, {method:'POST', headers:{'X-Cache-Refresh':'1', 'X-CSRF-Token':window.cacheAccount?.csrf || ''}});
    if (response.status===429) throw new Error('刷新正在进行或间隔不足，请一分钟后重试。');
    if (!response.ok) throw new Error('GitHub 状态查询未完成，保留已有信息，请稍后重试。');
    await load();
    if ($('#detail-dialog').open && Number($('#detail-dialog').dataset.number)===number) {
      await openDetail(number);
      if ($('#status-refresh-message')) $('#status-refresh-message').textContent = '已从 GitHub 刷新状态。';
    }
  } catch (error) {
    if ($('#detail-dialog').open && Number($('#detail-dialog').dataset.number)===number) message.textContent = error.message;
  } finally {button.disabled = false;}
}

function urlState() {
  const p = new URLSearchParams();
  for (const [key, value] of Object.entries(state)) {
    if (value && value !== 'all') p.set(key === 'query' ? 'q' : key, value);
  }
  const detail = $('#detail-dialog').open ? $('#detail-dialog').dataset.number : null;
  if (detail) p.set('entry', detail);
  history.replaceState(null, '', `${location.pathname}?${p}`);
}

function render() {
  if (!snapshot) return;
  const threshold = boundary(state.window);
  const timed = snapshot.entries.filter(e => new Date(e.created_at) >= threshold);
  $('#count-all').textContent = timed.length;
  for (const key of Object.keys(names)) $('#count-' + key).textContent = timed.filter(e => e.modules.includes(key)).length;
  const scoped = timed.filter(e => state.module === 'all' || e.modules.includes(state.module));
  $('#stat-total').textContent = scoped.length;
  $('#stat-urgent').textContent = scoped.filter(e => e.priority === 'P0' || e.priority === 'P1').length;
  $('#stat-pr').textContent = scoped.filter(e => e.kind === 'pr').length;
  $('#stat-rfc').textContent = scoped.filter(e => e.kind === 'rfc').length;
  $('#tab-all').textContent = scoped.length;
  for (const kind of Object.keys(types)) $('#tab-' + kind).textContent = scoped.filter(e => e.kind === kind).length;
  const query = state.query.trim().toLowerCase().replace(/^#/, '');
  visibleEntries = scoped.filter(e => matchesFollowup(e) && (state.type === 'all' || e.kind === state.type) && (state.priority === 'all' || e.priority === state.priority) && (!query || (/^\d+$/.test(query) ? e.number === Number(query) : [e.number, e.title, e.summary, e.author, e.body].join(' ').toLowerCase().includes(query))));
  const confidenceRank = {high: 0, medium: 1, low: 2};
  visibleEntries.sort((a, b) => {
    if (state.sort === 'priority') return a.priority.localeCompare(b.priority) || new Date(b.updated_at) - new Date(a.updated_at);
    if (state.sort === 'confidence') return (confidenceRank[a.confidence?.level] ?? 3) - (confidenceRank[b.confidence?.level] ?? 3) || a.priority.localeCompare(b.priority);
    const field = state.sort === 'created' ? 'created_at' : 'updated_at';
    return new Date(b[field]) - new Date(a[field]);
  });
  const pages = Math.max(1, Math.ceil(visibleEntries.length / pageSize));
  currentPage = Math.min(currentPage, pages);
  $('#result-count').textContent = `${visibleEntries.length} 个条目${query || state.priority !== 'all' ? ' · 已筛选' : ''}`;
  all('[data-module]').forEach(button => {button.classList.toggle('active', button.dataset.module === state.module); button.setAttribute('aria-pressed', String(button.dataset.module === state.module));});
  all('[data-window]').forEach(button => {button.classList.toggle('selected', button.dataset.window === state.window); button.setAttribute('aria-pressed', String(button.dataset.window === state.window));});
  all('[data-type]').forEach(button => {button.classList.toggle('selected', button.dataset.type === state.type); button.setAttribute('aria-pressed', String(button.dataset.type === state.type));});
  $('#priority-filter').value = state.priority;
  $('#sort').value = state.sort;
  $('#followup-filter').value = state.followup;
  $('#owner-filter').value = state.owner;
  $('#page-title').innerHTML = `${state.module === 'all' ? '缓存工作台' : names[state.module]}<span class="heading-dot">.</span>`;
  const descriptions = {all: '从问题到改进，持续关注缓存系统的每一步。', hicache: '从设备到主机，再到存储。关注每一层的状态与效率。', unified: '一棵树，多种状态。关注前缀复用、分配与生命周期。', hisparse: '让稀疏注意力与缓存协同，释放长上下文的可能性。'};
  $('#page-description').textContent = descriptions[state.module];
  if (!visibleEntries.length) {
    $('#entries').innerHTML = '<div class="empty-state"><div class="empty-symbol">⌑</div><h3>这里暂时很安静</h3><p>当前条件下没有开放条目。<br>试试扩大时间范围，或清除筛选。</p><button id="reset-filters">查看六个月内全部条目</button></div>';
    $('#reset-filters').onclick = () => {Object.assign(state, {window:'sixmonths', module:'all', type:'all', priority:'all', followup:'all', owner:'', query:''}); $('#search').value = ''; changed();};
  } else {
    $('#entries').innerHTML = visibleEntries.slice((currentPage - 1) * pageSize, currentPage * pageSize).map(e => `<article class="entry-card"><button class="entry-button" data-entry="${e.number}" aria-label="查看 #${e.number} ${escapeHTML(e.title)}"><span class="entry-top"><span class="kind-badge kind-${e.kind}"><span class="kind-icon" aria-hidden="true">${icons[e.kind]}</span>${types[e.kind]}</span><span class="item-number">#${e.number}</span>${priorityBadge(e.priority)}${moduleTags(e.modules)}${e.draft ? '<span class="draft-tag">Draft</span>' : ''}<span class="entry-date">创建于 ${formatDate(e.created_at)}</span></span><span class="entry-title">${escapeHTML(e.title)}</span><span class="entry-summary">${escapeHTML(e.summary)}</span><span class="entry-bottom"><span class="entry-author"><span class="author-avatar" aria-hidden="true">${escapeHTML((e.author || 'G').slice(0, 1).toUpperCase())}</span>作者 ${escapeHTML(e.author || 'GitHub')}</span>${confidenceLabel(e.confidence)}<span class="note-origin">${e.editorial ? '人工整理 · ' + e.editorial.reviewed_at : '源文摘要 · 自动分类'}</span><span class="entry-arrow" aria-hidden="true">↗</span></span></button>${workflowRow(e)}</article>`).join('');
    all('[data-entry]').forEach(button => button.onclick = () => openDetail(Number(button.dataset.entry)));
  }
  $('#pagination').hidden = pages <= 1;
  $('#page-info').textContent = `${currentPage} / ${pages}`;
  $('#previous-page').disabled = currentPage === 1;
  $('#next-page').disabled = currentPage === pages;
  urlState();
}

function inline(text) {
  return escapeHTML(text)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1 ↗</a>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function markdown(source) {
  const blocks = source.split(/```/);
  return blocks.map((block, index) => {
    if (index % 2) return `<pre><code>${escapeHTML(block.replace(/^[^\n]*\n/, ''))}</code></pre>`;
    return block.split(/\n\s*\n/).map(paragraph => {
      const trimmed = paragraph.trim();
      if (!trimmed) return '';
      if (/^#{1,6}\s/.test(trimmed)) {
        const [heading, ...rest] = trimmed.split('\n');
        return `<h3>${inline(heading.replace(/^#+\s*/, ''))}</h3>${rest.length ? '<p>' + inline(rest.join('\n')).replace(/\n/g, '<br>') + '</p>' : ''}`;
      }
      if (/^[-*] /.test(trimmed)) return '<ul>' + trimmed.split(/\n(?=[-*] )/).map(line => '<li>' + inline(line.replace(/^[-*] /, '')).replace(/\n/g, '<br>') + '</li>').join('') + '</ul>';
      if (/^\|/.test(trimmed)) return '<pre class="raw-table">' + escapeHTML(trimmed) + '</pre>';
      if (/^>/.test(trimmed)) return '<blockquote>' + inline(trimmed.replace(/^>\s?/gm, '')) + '</blockquote>';
      return '<p>' + inline(trimmed).replace(/\n/g, '<br>') + '</p>';
    }).join('');
  }).join('');
}

async function openDetail(number) {
  const brief = snapshot?.entries.find(e => e.number === number);
  if (!brief) return;
  const requestId = ++detailRequest;
  renderDetail({...brief, body: '', loading: true});
  try {
    const response = await fetch('/api/entry/' + number, {cache:'no-cache'});
    if (!response.ok) throw new Error('Details unavailable');
    const value = await response.json();
    if (requestId === detailRequest && $('#detail-dialog').open) renderDetail(value.entry);
  } catch (error) {
    if (requestId === detailRequest && $('#detail-dialog').open) renderDetail({...brief, body:'', load_error:true});
  }
}

function renderDetail(entry) {
  const number = entry.number;
  const dialog = $('#detail-dialog');
  if (!dialog.open) lastFocus = document.activeElement;
  dialog.dataset.number = number;
  $('#detail-eyebrow').textContent = `${types[entry.kind].toUpperCase()} / #${entry.number} / OPEN`;
  const stale = entry.editorial?.source_updated_at && new Date(entry.updated_at) > new Date(entry.editorial.source_updated_at);
  $('#detail-content').innerHTML = `<div class="detail-inner"><div class="entry-top">${priorityBadge(entry.priority)}${moduleTags(entry.modules)}${entry.draft ? '<span class="draft-tag">Draft</span>' : ''}</div><h2 id="detail-title">${escapeHTML(entry.title)}</h2><a class="detail-link" href="${escapeHTML(entry.url)}" target="_blank" rel="noopener noreferrer">在 GitHub 查看${entry.source_kind === 'pr' ? '改动与讨论' : '原始讨论'} <span>↗</span></a><div class="detail-properties"><div><span class="property-label">AUTHOR</span><span class="property-value">${escapeHTML(entry.author || 'GitHub')}</span></div><div><span class="property-label">PRIORITY</span><span class="property-value">${entry.priority} · ${labels[entry.priority]}</span></div><div><span class="property-label">CREATED</span><span class="property-value">${new Date(entry.created_at).toLocaleString('zh-CN', {hour12:false})}</span></div><div><span class="property-label">LAST ACTIVITY</span><span class="property-value">${new Date(entry.updated_at).toLocaleString('zh-CN', {hour12:false})}</span></div></div>${workflowDetails(entry)}<h3 class="section-label">${entry.editorial ? '整理说明' : '问题与实现摘要'}</h3><p class="detail-summary">${escapeHTML(entry.summary)}</p>${entry.editorial ? '<p class="evidence-note">人工整理于 ' + entry.editorial.reviewed_at + ' · 来源：项目待办审阅。非本轮新运行的实验。</p>' : '<p class="evidence-note">摘自作者正文；自动分类的条目尚待人工审阅。</p>'}${stale ? '<p class="stale-note">源条目在人工整理后有新活动。下方原文已同步，请结合最新讨论复核这段说明。</p>' : ''}<div class="evidence-box"><strong>${entry.priority} · 为什么优先关注</strong><div>${escapeHTML(entry.priority_reason)}</div></div>${entry.confidence ? '<h3 class="section-label">ISSUE 置信度 · ' + entry.confidence.label + '</h3><div class="evidence-box">' + confidenceLabel(entry.confidence) + '<ul>' + entry.confidence.evidence.map(e => '<li>' + escapeHTML(e) + '</li>').join('') + '</ul><div class="evidence-note">' + escapeHTML(entry.confidence.meaning) + '</div></div>' : ''}${entry.related?.length ? '<h3 class="section-label">关联的开放条目</h3><div class="related-links">' + entry.related.map(n => '<button data-related="' + n + '">#' + n + ' ↗</button>').join('') + '</div>' : ''}<h3 class="section-label">GITHUB 原文 <span class="evidence-note">· 本次快照已同步</span></h3><div class="source-document">${entry.loading ? '<p><span class="loading-ring"></span> 正在读取完整正文…</p>' : entry.load_error ? '<p>正文暂时无法加载。<button id="detail-retry" class="quiet-button">重新加载 ↻</button>也可以打开 GitHub 查看原文。</p>' : entry.body ? markdown(entry.body) : '<p>作者尚未填写正文，可打开 GitHub 查看评论。</p>'}</div></div>`;
  all('[data-related]').forEach(button => button.onclick = () => openDetail(Number(button.dataset.related)));
  $('#refresh-entry-status').onclick = () => refreshEntryStatus(number);
  if ($('#detail-retry')) $('#detail-retry').onclick = () => openDetail(number);
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  urlState();
}

function changed() {currentPage = 1; render();}

async function load() {
  if (loading) return;
  loading = true;
  $('#refresh-button').disabled = true;
  try {
    const response = await fetch('/api/entries', {cache:'no-cache'});
    if (response.status === 401) { location.replace('/login'); return; }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const value = await response.json();
    if (!Array.isArray(value.entries)) throw new Error('Invalid snapshot');
    const isNew = !snapshot || snapshot.completed_at !== value.completed_at || snapshot.workflow_updated_at !== value.workflow_updated_at;
    snapshot = value;
    $('.workflow-interval').textContent = value.workflow_refresh_enabled ? '跟进状态每 30 分钟更新' : '跟进状态自动刷新待配置';
    const stale = Date.now() - new Date(value.synced_at) > 3.5 * 3600 * 1000;
    const error = value.sync_status?.ok === false;
    $('#sync-text').textContent = `最近同步 ${formatDate(value.completed_at, true)}`;
    $('#next-sync').textContent = `下次计划 ${formatDate(value.next_sync_at, true)}`;
    $('#sync-icon').classList.toggle('warning', stale || error);
    $('#status-banner').hidden = !stale && !error;
    $('#status-banner').textContent = error ? '最近一次 GitHub 同步未完成，当前展示上次完整快照。服务会自动重试；已关闭状态可能尚未反映。' : '快照已超过正常更新间隔，当前数据可能滞后。请检查服务的同步状态。';
    $('#method-body').innerHTML = Object.entries({scope:'收录范围', priority:'优先级', confidence:'置信度', updates:'更新与状态', date_filter:'时间范围'}).map(([key, label]) => `<h3>${label}</h3><p>${escapeHTML(value.methodology[key])}</p>`).join('') + '<h3>当前快照</h3><p>覆盖 ' + snapshot.entries.length + ' 个开放条目。跨模块条目只计入总数一次，模块数可以相加超过总数。讨论、测试结果和改动详情以 GitHub 原文为准。</p><p><a href="https://github.com/sgl-project/sglang" target="_blank" rel="noopener noreferrer">前往 SGLang 仓库 ↗</a></p>';
    if (isNew) {
      const detailNumber = Number($('#detail-dialog').dataset.number);
      render();
      if ($('#detail-dialog').open) {
        if (snapshot.entries.some(e => e.number === detailNumber)) openDetail(detailNumber);
        else {$('#detail-dialog').close(); $('#status-banner').hidden = false; $('#status-banner').textContent = '刚才查看的条目已关闭、合并或移出收录范围，已从当前快照移除。';}
      }
    }
  } catch (error) {
    $('#status-banner').hidden = false;
    $('#status-banner').textContent = snapshot ? '暂时无法连接服务，保留当前快照。请稍后重试。' : '暂时无法读取数据。请检查服务后重试。';
    if (!snapshot) {$('#result-count').textContent = '数据暂不可用'; $('#entries').innerHTML = '<div class="empty-state"><h3>连接暂时中断</h3><p>请检查服务后重试。</p></div>';}
  } finally {loading = false; $('#refresh-button').disabled = false;}
}

all('[data-module]').forEach(button => button.onclick = () => {state.module = button.dataset.module; changed();});
all('[data-window]').forEach(button => button.onclick = () => {state.window = button.dataset.window; changed();});
all('[data-type]').forEach(button => button.onclick = () => {state.type = button.dataset.type; changed();});
$('#priority-filter').onchange = (event) => {state.priority = event.target.value; changed();};
$('#followup-filter').onchange = event => {state.followup = event.target.value; changed();};
$('#owner-filter').oninput = event => {state.owner = event.target.value; changed();};
$('#sort').onchange = (event) => {state.sort = event.target.value; changed();};
$('#search').value = state.query;
let searchTimer;
$('#search').oninput = (event) => {state.query = event.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(changed, 120);};
$('#previous-page').onclick = () => {currentPage--; render(); $('.entries-heading').scrollIntoView({behavior:'smooth'});};
$('#next-page').onclick = () => {currentPage++; render(); $('.entries-heading').scrollIntoView({behavior:'smooth'});};
$('#refresh-button').onclick = load;
$('#method-button').onclick = () => $('#method-dialog').showModal();
all('.close-dialog').forEach(button => button.onclick = () => button.closest('dialog').close());
all('dialog').forEach(dialog => dialog.addEventListener('click', event => {if (event.target === dialog) {const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();}}));
$('#detail-dialog').addEventListener('close', () => {delete $('#detail-dialog').dataset.number; urlState(); lastFocus?.focus();});
document.addEventListener('keydown', event => {if (event.key === '/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName) && !all('dialog').some(d => d.open)) {event.preventDefault(); $('#search').focus();}});
const requestedEntry = Number(params.get('entry'));
load().then(() => {if (requestedEntry) openDetail(requestedEntry);});
setInterval(load, 60000);
