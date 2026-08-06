/* 前端逻辑：计时器、留言板、Toast */
(function () {
  'use strict';

  // ===== Toast =====
  var toastEl = document.getElementById('toast');
  var toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove('show'); }, 2500);
  }

  // ===== 运行时长计时器（用 data-sec 秒数递增，修复 parseInt 跳变 bug）=====
  var elapsedEl = document.getElementById('elapsed');
  if (elapsedEl) {
    setInterval(function () {
      var s = parseInt(elapsedEl.getAttribute('data-sec'), 10) + 1;
      elapsedEl.setAttribute('data-sec', s);
      var h = Math.floor(s / 3600);
      var m = Math.floor((s % 3600) / 60);
      var sec = s % 60;
      elapsedEl.textContent =
        (h < 10 ? '0' : '') + h + ':' +
        (m < 10 ? '0' : '') + m + ':' +
        (sec < 10 ? '0' : '') + sec;
    }, 1000);
  }

  // ===== 留言板 =====
  var addBtn = document.getElementById('add-btn');
  var contentInput = document.getElementById('content');
  if (addBtn && contentInput) {
    addBtn.addEventListener('click', function () {
      var c = contentInput.value.trim();
      if (!c) { toast('请输入内容'); return; }
      fetch('/api/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: c })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) { toast('已添加，并加密备份'); setTimeout(function () { location.reload(); }, 600); }
          else { toast(d.error || '失败'); }
        })
        .catch(function () { toast('网络错误'); });
    });
  }
})();