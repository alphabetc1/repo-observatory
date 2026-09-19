"""Render workspace metadata and localized assets without changing layout."""
import html
import json
import re

from cacheboard import MODULES, REPO
from localization import translate
from settings import CONFIG


def render_asset(name, source, language):
    if name not in ('index.html', 'auth.html', 'app.js', 'auth.js'):
        return source
    if name == 'app.js':
        source = re.sub(r'^const names = .*?;$', lambda _: 'const names = ' + json.dumps(MODULES) + ';', source, flags=re.M)
        source = source.replace("$('#page-description').textContent = descriptions[state.module];", "$('#page-description').textContent = descriptions[state.module] || names[state.module];")
        source = source.replace('https://github.com/sgl-project/sglang', 'https://github.com/' + REPO)
        if REPO != 'sgl-project/sglang':
            source = source.replace('前往 SGLang 仓库', '查看仓库')
        if CONFIG.get('modules'):
            source = source.replace("'缓存工作台'", json.dumps(CONFIG.get('title', 'Repo Observatory')))
            source = source.replace('从问题到改进，持续关注缓存系统的每一步。', 'Follow issues, pull requests, and engineering progress.')
    if name == 'index.html' and CONFIG.get('modules'):
        start = source.index('      <button class="module-button" data-module="hicache"')
        end = source.index('    </nav>', start)
        buttons = []
        for key, label in MODULES.items():
            buttons.append(f'<button class="module-button" data-module="{key}"><span class="module-dot"></span><span>{html.escape(label)}</span><span class="nav-count" id="count-{key}">—</span></button>')
        source = source[:start] + '\n'.join(buttons) + '\n' + source[end:]
        source = source.replace('Cache Observatory', html.escape(CONFIG.get('title', 'Repo Observatory')))
        source = source.replace('SGLANG ENGINEERING', html.escape(REPO))
        source = source.replace('SGLang', html.escape(REPO.split('/')[1]))
        source = source.replace('THE CACHE WORKSPACE', 'THE ENGINEERING WORKSPACE')
        source = source.replace('A clearer view of cache engineering.', 'A clearer view of engineering.')
    if name.endswith('.html'):
        if CONFIG.get('modules'):
            source = source.replace('Cache Observatory', html.escape(CONFIG.get('title', 'Repo Observatory')))
        source = source.replace('sgl-project / sglang', html.escape(REPO.replace('/', ' / ')))
        source = source.replace('</head>', '<script src="/locale.js" defer></script></head>')
        control = '<label class="language-control"><select id="language-select" aria-label="Language"><option value="zh-CN">中文</option><option value="en">English</option></select></label>'
        if name == 'index.html':
            source = source.replace('    </nav>', '    </nav>' + control)
        else:
            source = source.replace('<div id="account-content">', control + '<div id="account-content">')
        source = source.replace('</head>', '<link rel="stylesheet" href="/locale.css"></head>')
    if language == 'en':
        source = translate(source).replace("'zh-CN'", "'en'").replace('lang="zh-CN"', 'lang="en"')
    return source
