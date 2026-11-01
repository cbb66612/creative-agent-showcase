const state = { resources: [], kind: 'all', source: 'all', category: 'all', query: '', page: 1, pageSize: 24 };
const $ = (selector) => document.querySelector(selector);
const grid = $('#resource-grid');
const dialog = $('#detail-dialog');
const countFormat = new Intl.NumberFormat('zh-CN');

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function sourceLabel(item) {
  if (item.platform === 'tv') return 'TV';
  return '外部资源';
}

function kindLabel(item) { return item.kind === 'skill' ? 'Skill' : '预设'; }

function coverUrl(item) {
  return item.cover ? `data-export/covers/${encodeURIComponent(item.cover)}` : '';
}

function resultSet() {
  const q = state.query.trim().toLocaleLowerCase();
  return state.resources.filter((item) =>
    (state.kind === 'all' || item.kind === state.kind) &&
    (state.source === 'all' || item.platform === state.source) &&
    (state.category === 'all' || item.grp === state.category) &&
    (!q || item.search.includes(q))
  );
}

function card(item) {
  const cover = coverUrl(item);
  const visual = cover ? `<img src="${escapeHtml(cover)}" alt="" loading="lazy">` : `<span class="thumb-fallback" aria-hidden="true">${item.kind === 'skill' ? '✳' : '✦'}</span>`;
  const category = [item.grp, item.cat].filter(Boolean).join(' / ') || '创作资源';
  return `<button class="resource-card" type="button" data-id="${item.id}" aria-label="查看 ${escapeHtml(item.title)} 的详情"><div class="thumb" data-variant="${item.id % 4}">${visual}<span class="kind-pill">${kindLabel(item)}</span></div><div class="card-content"><span class="card-category">${escapeHtml(category)}</span><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.summary || item.body?.slice(0, 80) || '点击查看完整内容')}</p><div class="card-bottom"><span>${sourceLabel(item)}</span><span class="arrow" aria-hidden="true">↗</span></div></div></button>`;
}

function render() {
  const found = resultSet();
  const pages = Math.max(1, Math.ceil(found.length / state.pageSize));
  state.page = Math.min(state.page, pages);
  const slice = found.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
  grid.innerHTML = slice.map(card).join('');
  $('#result-count').textContent = `找到 ${countFormat.format(found.length)} 份资源`;
  $('#empty-state').hidden = found.length > 0;
  $('#page-info').textContent = `${state.page} / ${pages}`;
  $('#prev-page').disabled = state.page === 1;
  $('#next-page').disabled = state.page === pages;
}

function openDetail(id) {
  const item = state.resources.find((entry) => entry.id === id);
  if (!item) return;
  const cover = coverUrl(item);
  $('#detail-cover').innerHTML = cover ? `<img src="${escapeHtml(cover)}" alt="">` : '<span aria-hidden="true">✳</span>';
  $('#detail-meta').innerHTML = [kindLabel(item), sourceLabel(item), item.grp, item.cat].filter(Boolean).map((part) => `<span>${escapeHtml(part)}</span>`).join('');
  $('#detail-title').textContent = item.title;
  $('#detail-summary').textContent = item.summary || '';
  $('#detail-body').textContent = item.body || '这份资源暂无正文。';
  $('#detail-source').textContent = item.source ? `来源：${item.source}` : `来源：${sourceLabel(item)}`;
  $('#copy-body').textContent = '复制内容 ↗';
  $('#copy-body').dataset.body = item.body || '';
  dialog.showModal();
}

function reset() {
  state.kind = 'all'; state.source = 'all'; state.category = 'all'; state.query = ''; state.page = 1;
  $('#search-input').value = '';
  $('#source-filter').value = 'all';
  $('#category-filter').value = 'all';
  document.querySelectorAll('.type-tabs button').forEach((button) => button.classList.toggle('active', button.dataset.kind === 'all'));
  render();
}

let searchTimer;
$('#search-input').addEventListener('input', (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.query = event.target.value; state.page = 1; render(); }, 150);
});
$('#source-filter').addEventListener('change', (event) => { state.source = event.target.value; state.page = 1; render(); });
$('#category-filter').addEventListener('change', (event) => { state.category = event.target.value; state.page = 1; render(); });
document.querySelectorAll('.type-tabs button').forEach((button) => button.addEventListener('click', () => {
  state.kind = button.dataset.kind; state.page = 1;
  document.querySelectorAll('.type-tabs button').forEach((tab) => tab.classList.toggle('active', tab === button));
  render();
}));
grid.addEventListener('click', (event) => {
  const button = event.target.closest('.resource-card');
  if (button) openDetail(Number(button.dataset.id));
});
$('#prev-page').addEventListener('click', () => { state.page--; render(); $('#browse').scrollIntoView({ behavior: 'smooth' }); });
$('#next-page').addEventListener('click', () => { state.page++; render(); $('#browse').scrollIntoView({ behavior: 'smooth' }); });
$('#reset-filters').addEventListener('click', reset);
$('#close-dialog').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
$('#copy-body').addEventListener('click', async (event) => {
  try { await navigator.clipboard.writeText(event.target.dataset.body); event.target.textContent = '已复制 ✓'; }
  catch { event.target.textContent = '复制失败，请手动选择正文'; }
});
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !dialog.open && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    event.preventDefault(); $('#search-input').focus();
  }
});

fetch('data-export/resources.json').then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); }).then((items) => {
  state.resources = items.map((item) => ({ ...item, search: [item.title, item.summary, item.body, item.grp, item.cat, item.tags].join(' ').toLocaleLowerCase() }));
  const total = items.length;
  const skills = items.filter((item) => item.kind === 'skill').length;
  $('#hero-total').textContent = countFormat.format(total);
  $('#hero-presets').textContent = countFormat.format(total - skills);
  $('#hero-skills').textContent = countFormat.format(skills);
  const groups = [...new Set(items.map((item) => item.grp).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  const category = $('#category-filter');
  groups.forEach((group) => { const option = document.createElement('option'); option.value = group; option.textContent = group; category.append(option); });
  render();
}).catch((error) => {
  $('#result-count').textContent = '资源加载失败';
  $('#empty-state').hidden = false;
  $('#empty-state h3').textContent = '资源暂时无法加载';
  $('#empty-state p').textContent = '请刷新页面后重试。';
  console.error(error);
});
