'use strict';
const languageSelect = document.querySelector('#language-select');
if (languageSelect) {
  languageSelect.value = document.documentElement.lang;
  languageSelect.onchange = () => {
    document.cookie = `repo_language=${languageSelect.value}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
    if (location.pathname === '/activate' && window.activationToken) {
      history.replaceState(null, '', '/activate#' + new URLSearchParams({token: window.activationToken}));
    }
    location.reload();
  };
}
