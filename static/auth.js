'use strict';
let account;
const content = document.querySelector('#account-content');
const message = document.querySelector('#account-message');
const escapeText = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function request(path, payload) {
  const options = {cache:'no-store'};
  if (payload !== undefined) Object.assign(options, {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':account?.csrf || ''}, body:JSON.stringify(payload)});
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败，请稍后重试。');
  return result;
}
function bindForm(id, action) {
  document.getElementById(id).addEventListener('submit', async event => {
    event.preventDefault(); message.textContent = '';
    const button = event.target.querySelector('button'); button.disabled = true;
    try { await action(new FormData(event.target)); }
    catch(error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  });
}
const passwordInput = (label, name, autocomplete) => `<label>${label}<input name="${name}" type="password" minlength="12" maxlength="128" autocomplete="${autocomplete}" required></label>`;
async function members() {
  const users = await request('/api/admin/members');
  document.querySelector('#members').innerHTML = users.map(user => `<div class="member"><div>${escapeText(user.username)}${user.id === account.id ? '（你）' : ''}<small>${user.role === 'admin' ? '管理员' : '访客'} · ${user.disabled ? '已停用' : user.activated ? '已启用' : user.invite_expires_at * 1000 > Date.now() ? '等待激活' : '邀请已过期'}</small></div>${user.id !== account.id && !user.disabled ? `<button data-revoke="${user.id}">停用账号</button>` : ''}</div>`).join('');
  document.querySelectorAll('[data-revoke]').forEach(button => button.onclick = async () => {
    if (!confirm('停用后，该账号会立即退出登录。继续吗？')) return;
    button.disabled = true;
    try { await request('/api/admin/revoke', {user_id:Number(button.dataset.revoke)}); await members(); }
    catch(error) { message.textContent = error.message; button.disabled = false; }
  });
}
async function main() {
  if (!content) {
    try {
      account = await request('/api/auth/me');
      window.cacheAccount = account;
      if (account.enabled === false) return;
      const bar = document.querySelector('.topbar');
      const link = document.createElement('a'); link.href = '/access'; link.className = 'account-link'; link.textContent = `${account.username} · 账号`; bar.append(link);
    } catch { location.replace('/login'); }
    return;
  }
  if (location.pathname === '/activate') {
    const token = new URLSearchParams(location.hash.slice(1)).get('token');
    window.activationToken = token;
    history.replaceState(null, '', '/activate');
    const invite = await request('/api/auth/invitation', {token});
    content.innerHTML = `<h1>加入工作台</h1><p>为 <strong>${escapeText(invite.username)}</strong> 设置密码后即可访问。邀请只能使用一次。</p><form id="activate-form">${passwordInput('设置密码（12–128 个字符）', 'password', 'new-password')}${passwordInput('再次输入密码', 'confirm', 'new-password')}<button>激活账号并进入</button></form>`;
    bindForm('activate-form', async data => {
      if (data.get('password') !== data.get('confirm')) throw new Error('两次输入的密码不一致。');
      await request('/api/auth/activate', {token, password:data.get('password')}); location.replace('/');
    });
    return;
  }
  if (location.pathname === '/login') {
    content.innerHTML = `<h1>登录工作台</h1><p>使用受邀账号查看项目的最新动态。</p><form id="login-form"><label>用户名<input name="username" autocomplete="username" required maxlength="40"></label>${passwordInput('密码', 'password', 'current-password')}<button>登录</button></form><p>还没有账号或忘记密码？请联系管理员获取邀请链接。</p>`;
    bindForm('login-form', async data => { await request('/api/auth/login', Object.fromEntries(data)); location.replace('/'); });
    return;
  }
  try { account = await request('/api/auth/me'); } catch { location.replace('/login'); return; }
  content.innerHTML = `<h1>账号设置</h1><p>${escapeText(account.username)} · ${account.role === 'admin' ? '管理员' : '访客'}</p><div class="account-actions"><a href="/">← 返回工作台</a><button id="logout">退出登录</button></div><h2>修改密码</h2><form id="password-form">${passwordInput('当前密码', 'old_password', 'current-password')}${passwordInput('新密码（12–128 个字符）', 'password', 'new-password')}${passwordInput('再次输入新密码', 'confirm', 'new-password')}<button>更新密码</button></form>`;
  document.querySelector('#logout').onclick = async () => { try { await request('/api/auth/logout', {}); location.replace('/login'); } catch(error) { message.textContent = error.message; } };
  bindForm('password-form', async data => {
    if (data.get('password') !== data.get('confirm')) throw new Error('两次输入的密码不一致。');
    await request('/api/auth/password', Object.fromEntries(data)); account = await request('/api/auth/me'); document.querySelector('#password-form').reset(); message.textContent = '密码已更新，其他设备已退出登录。';
  });
  if (account.role === 'admin') {
    content.insertAdjacentHTML('beforeend', '<h2>邀请成员</h2><p>邀请链接 48 小时内有效。对方打开链接设置密码即可访问，无需 SSH。重新邀请已停用或未激活的用户名，会替换旧邀请。</p><form id="invite-form"><label>用户名<input name="username" pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]{2,39}" required minlength="3" maxlength="40" autocomplete="off"></label><label>角色<select name="role"><option value="viewer">访客 · 查看工作台</option><option value="admin">管理员 · 可邀请和停用成员</option></select></label><button>生成邀请链接</button></form><div id="invite-result" hidden></div><h2>成员</h2><div id="members"></div>');
    bindForm('invite-form', async data => {
      const result = await request('/api/admin/invite', Object.fromEntries(data));
      const box = document.querySelector('#invite-result'); box.hidden = false;
      box.innerHTML = `请将链接私下发给 ${escapeText(result.username)}：<a href="${escapeText(result.url)}">${escapeText(result.url)}</a><button id="copy-invite" type="button">复制链接</button>`;
      document.querySelector('#copy-invite').onclick = async () => { try { await navigator.clipboard.writeText(result.url); document.querySelector('#copy-invite').textContent = '已复制'; } catch { message.textContent = '请选中上方链接并复制。'; } };
      await members();
    });
    await members();
  }
}
main().catch(error => { if(message) message.textContent = error.message; if(content) content.innerHTML = '<h1>暂时无法打开</h1><a href="/login">返回登录</a>'; });
