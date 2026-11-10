/* 台股排行榜 —— 零依賴、零 CDN。
 *
 * ## 兩個不同的時間，一定要並列
 *   資料日期   資料本身的交易日（週末去抓，拿到的還是週五的）
 *   更新時間   Actions 實際執行的時刻
 * 只看其中一個都會誤判。
 *
 * ## 載入策略
 * 一進來只載 meta.json + latest.json + glossary.json（約 170 KB）。
 * 歷史月份用到才載 —— 1151022s-1 的教訓：一次載完，653 KB 就讓網頁變鈍。
 */
(function () {
  'use strict';

  var S = {
    meta: null, latest: null, gl: {}, idx: null,
    board: 'volume', topN: 100, date: null,
    monthCache: {}, analysis: null,
    sortKey: 'rank', sortDesc: false,
    rows: [], gone: []
  };

  var $ = function (s) { return document.querySelector(s); };

  function get(url) {
    return fetch(url, { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) throw new Error(url + ' → HTTP ' + r.status);
      return r.json();
    });
  }

  /* ---------------- 格式化 ---------------- */

  function money(v, unit) {
    if (v === null || v === undefined) return '';
    if (unit === '張') return Math.round(v / 1000).toLocaleString('en-US');
    if (unit === '億') return (v / 1e8).toFixed(2);
    if (unit === '%') return v.toFixed(2);
    return v.toLocaleString('en-US');
  }

  var UNIT = {
    volume: '張', amount: '億', swing: '%',
    chip_buy: '億', chip_sell: '億'
  };

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* ---------------- 名詞說明 ---------------- */

  function glFull(key) { return (S.gl[key] || {}).full || ''; }
  function glShort(key) { return (S.gl[key] || {}).short || ''; }
  function glLabel(key) { return (S.gl[key] || {}).label || key; }

  function boardGlKey(b) {
    return ({ volume: 'board.volume', amount: 'board.amount',
              swing: 'board.swing' })[b] || 'board.chip';
  }

  function renderLegend() {
    var keys = [boardGlKey(S.board), 'rank.new', 'rank.streak',
                'rank.back', 'rank.out', 'rank.move'];
    var html = '<h3>名詞說明</h3><div class="items">';
    keys.forEach(function (k) {
      if (!S.gl[k]) return;
      html += '<div data-gl="' + k + '"><b>' + esc(glLabel(k)) + '</b>　'
            + esc(glShort(k)) + '</div>';
    });
    $('#legend').innerHTML = html + '</div>';
  }

  /* ---------------- 新鮮度 ---------------- */

  function renderFreshness() {
    var m = S.meta || {};
    var parts = [];
    parts.push('<span><b>資料日期　' + esc(m.data_date || '—')
             + '</b>（資料本身的交易日）</span>');
    parts.push('<span><b>更新時間　' + esc(m.run_at_tpe || m.run_at || '—')
             + '</b>（自動抓取執行的時刻）</span>');
    if (m.same_day === false) {
      parts.push('<span class="lag">⚠ 官方資料還沒更新到今天 —— '
               + '這是正常的，收盤後要過一段時間才會發布</span>');
    }
    if (S.analysis && S.analysis.generated) {
      parts.push('<span>分析結果　<b>' + esc(S.analysis.generated)
               + '</b>（本機推送，不會自動更新）</span>');
    }
    $('#freshness').innerHTML = parts.join('');

    var notes = (m.notes || []).filter(Boolean);
    if (notes.length) {
      $('#warn').hidden = false;
      $('#warn').innerHTML = notes.map(esc).join('<br>');
    } else {
      $('#warn').hidden = true;
    }
    $('#credit').textContent =
      '資料來源：' + (m.sources || []).join('、') + '　|　' + (m.license || '');
  }

  /* ---------------- 分頁 ---------------- */

  function renderTabs() {
    var zh = (S.meta && S.meta.board_zh) || {};
    var boards = (S.meta && S.meta.boards) || [];
    var html = '';
    boards.forEach(function (b) {
      html += '<button data-tab="board" data-board="' + b + '">'
            + esc(zh[b] || b) + '</button>';
    });
    html += '<button data-tab="analysis">分析與選股</button>';
    html += '<button data-tab="about">說明</button>';
    $('#tabs').innerHTML = html;
    $('#tabs').addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.tab === 'board') {
        S.board = b.dataset.board;
        S.topN = Math.min(S.topN, (S.meta.store_n || 300));
        var def = (S.meta.top_n || {})[S.board];
        if (def) { S.topN = def; $('#top-sel').value = String(def); }
        show('board');
      } else {
        show(b.dataset.tab);
      }
      markTab();
    });
  }

  function markTab() {
    [].forEach.call($('#tabs').children, function (b) {
      var on = (b.dataset.tab === 'board' && S.view === 'board'
                && b.dataset.board === S.board)
            || (b.dataset.tab !== 'board' && S.view === b.dataset.tab);
      b.classList.toggle('on', on);
    });
  }

  function show(v) {
    S.view = v;
    $('#board-view').hidden = v !== 'board';
    $('#analysis-view').hidden = v !== 'analysis';
    $('#about-view').hidden = v !== 'about';
    if (v === 'board') renderBoard();
    if (v === 'analysis') renderAnalysis();
    if (v === 'about') renderAbout();
    markTab();
  }

  /* ---------------- 榜單 ---------------- */

  function boardRows() {
    /* latest.json 的欄位：
       [stock_id, rank, metric, streak, is_new, is_back, move]
       ★ streak / is_new / move 是用 store_n（300）算的。
         使用者把顯示名次調小時，必須重算 —— 「連續在前 50 名 5 天」
         和「連續在前 100 名 5 天」是兩件不同的事。
         所以淺層的切換要回頭看歷史，見 recompute()。*/
    var raw = (S.latest.boards || {})[S.board] || [];
    return raw.filter(function (r) { return r[1] <= S.topN; });
  }

  function names() { return S.universe || {}; }

  function renderBoard() {
    if (!S.latest) return;
    var unit = UNIT[S.board] || '';
    var rows = S.rows.length ? S.rows : boardRows();
    var nm = names();

    var sorted = rows.slice();
    var k = S.sortKey, desc = S.sortDesc;
    var idx = { rank: 1, metric: 2, streak: 3, move: 6 }[k];
    sorted.sort(function (a, b) {
      var x, y;
      if (k === 'code') { x = a[0]; y = b[0]; }
      else if (k === 'name') { x = nm[a[0]] || ''; y = nm[b[0]] || ''; }
      else { x = a[idx]; y = b[idx]; }
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      if (x < y) return desc ? 1 : -1;
      if (x > y) return desc ? -1 : 1;
      return 0;
    });

    var html = '';
    sorted.forEach(function (r) {
      var mark, cls = '';
      if (r[5]) { mark = '↩ 回榜'; cls = 'new'; }
      else if (r[4]) { mark = '🆕 新進榜'; cls = 'new'; }
      else { mark = '連續 ' + r[3] + ' 天'; }
      var mv = r[6];
      var mvHtml = (mv === null || mv === undefined) ? ''
        : '<span class="' + (mv > 0 ? 'up' : mv < 0 ? 'down' : 'muted') + '">'
          + (mv > 0 ? '+' : '') + mv + '</span>';
      html += '<tr>'
        + '<td class="num">' + r[1] + '</td>'
        + '<td class="code">' + esc(r[0]) + '</td>'
        + '<td>' + esc(nm[r[0]] || '') + '</td>'
        + '<td class="num">' + money(r[2], unit) + '</td>'
        + '<td class="num">' + r[3] + '</td>'
        + '<td class="' + cls + '">' + mark + '</td>'
        + '<td class="num">' + mvHtml + '</td>'
        + '</tr>';
    });
    $('#board-table tbody').innerHTML = html || emptyMsg();

    var gone = (S.gone.length ? S.gone
                : ((S.latest.gone || {})[S.board] || []))
      .filter(function (g) { return g[1] <= S.topN; });
    $('#gone-count').textContent = '（' + gone.length + ' 檔）';
    $('#gone-table tbody').innerHTML = gone.map(function (g) {
      return '<tr><td class="num">' + g[1] + '</td><td class="code">'
        + esc(g[0]) + '</td><td>' + esc(nm[g[0]] || '') + '</td></tr>';
    }).join('');

    $('#board-note').textContent =
      '顯示前 ' + S.topN + ' 名，實際 ' + sorted.length + ' 筆'
      + (sorted.length < S.topN ? '（當天符合條件的就這麼多）' : '');

    [].forEach.call(document.querySelectorAll('#board-table thead th'),
      function (th) {
        th.classList.toggle('sorted', th.dataset.sort === S.sortKey);
        th.classList.toggle('desc', th.dataset.sort === S.sortKey && S.sortDesc);
      });

    renderLegend();
  }

  /* ---------------- 分析結果 ---------------- */

  function renderAnalysis() {
    var box = $('#analysis-body');
    if (!S.analysis) {
      box.innerHTML = '<div class="card"><h2>還沒有分析結果</h2>'
        + '<p class="muted">分析（回測、選股、分群）需要五年歷史與財報資料，'
        + '那些資料的授權不允許公開散布，所以是在本機電腦上計算的，'
        + '再把<b>結論</b>推送上來。</p>'
        + '<p class="muted">推送之後這裡就會出現。</p></div>';
      return;
    }
    var a = S.analysis, html = '';

    html += '<div class="card"><h2>分析結果</h2><dl class="kv">'
      + '<dt>產生時間</dt><dd>' + esc(a.generated) + '</dd>'
      + '<dt>資料截至</dt><dd>' + esc(a.data_date || '—') + '</dd>'
      + '<dt>標的範圍</dt><dd>' + esc(a.universe || '—') + '</dd>'
      + '</dl><p class="muted">★ 這一頁是本機推送的，<b>不會每天自動更新</b>。'
      + '上面的「產生時間」就是它的新鮮度。</p></div>';

    if (a.screen && a.screen.length) {
      html += '<div class="card"><h2>今日選股</h2>'
        + '<p class="muted">★「進場」是指<b>明天開盤買</b>，不是今天 —— '
        + '回測就是這樣計算的。</p><div class="tablewrap"><table><thead><tr>'
        + '<th>代號</th><th>名稱</th><th>分群</th><th>指派策略</th>'
        + '<th>可信度</th><th>上次驗證</th><th>今日訊號</th>'
        + '<th class="num">訊號強度</th></tr></thead><tbody>';
      a.screen.forEach(function (r) {
        html += '<tr><td class="code">' + esc(r.stock_id) + '</td>'
          + '<td>' + esc(r.name) + '</td><td>' + esc(r.group) + '</td>'
          + '<td>' + esc(r.strategy) + '</td>'
          + '<td class="grade-' + esc(r.grade) + '" data-tipraw="'
          + esc(r.reason) + '">' + esc(r.grade_label) + '</td>'
          + '<td>' + esc(r.verified || '—') + '</td>'
          + '<td>' + esc(r.signal_label) + '</td>'
          + '<td class="num">' + esc(r.strength) + '</td></tr>';
      });
      html += '</tbody></table></div></div>';
    }

    if (a.backtests && a.backtests.length) {
      html += '<div class="card"><h2>策略回測</h2><div class="tablewrap">'
        + '<table><thead><tr><th>策略</th><th class="num">總報酬</th>'
        + '<th class="num">年化</th><th class="num">最大回撤</th>'
        + '<th class="num">夏普</th><th class="num">勝率</th>'
        + '<th class="num">交易次數</th><th class="num">樣本內</th>'
        + '<th class="num">樣本外</th></tr></thead><tbody>';
      a.backtests.forEach(function (b) {
        html += '<tr><td>' + esc(b.name) + '</td>'
          + pct(b.total) + pct(b.cagr) + pct(b.mdd, false)
          + num(b.sharpe) + pct(b.win_rate, false) + num(b.trades, 0)
          + pct(b.in_sample) + pct(b.out_sample) + '</tr>';
      });
      html += '</tbody></table></div>'
        + '<p class="muted">★ 判斷策略能不能用，看<b>樣本外</b>那一欄。'
        + '樣本內好、樣本外差 = 過度最佳化。</p>';
      if (a.benchmark) {
        html += '<p class="muted">同期基準：'
          + Object.keys(a.benchmark).map(function (k) {
              return esc(k) + ' ' + a.benchmark[k].toFixed(2) + '%';
            }).join('　') + '　—— 打不贏基準的策略不值得執行。</p>';
      }
      html += '</div>';
    }

    if (a.trades && a.trades.length) {
      html += '<div class="card"><h2>逐筆交易</h2>'
        + '<p class="muted">★ 價格是<b>還原股價</b>（已調整除權息），'
        + '和當天在看盤軟體上看到的成交價不一樣 —— 直接對 K 線會對不上。</p>'
        + '<div class="tablewrap"><table><thead><tr>'
        + '<th>策略</th><th>代號</th><th>名稱</th><th>進場日</th>'
        + '<th class="num">進場價</th><th>出場日</th><th class="num">出場價</th>'
        + '<th class="num">天數</th><th class="num">報酬</th>'
        + '<th>出場原因</th></tr></thead><tbody>';
      a.trades.slice(0, 500).forEach(function (t) {
        html += '<tr><td>' + esc(t.strategy) + '</td>'
          + '<td class="code">' + esc(t.stock_id) + '</td>'
          + '<td>' + esc(t.name) + '</td><td>' + esc(t.entry_date) + '</td>'
          + num(t.entry_px) + '<td>' + esc(t.exit_date) + '</td>'
          + num(t.exit_px) + num(t.days, 0) + pct(t.ret)
          + '<td>' + esc(t.reason) + '</td></tr>';
      });
      html += '</tbody></table></div>';
      if (a.trades.length > 500) {
        html += '<p class="muted">共 ' + a.trades.length
              + ' 筆，只顯示前 500 筆。</p>';
      }
      html += '</div>';
    }

    if (a.groups && a.groups.length) {
      html += '<div class="card"><h2>分群結果</h2>'
        + '<p class="muted">分群只用<b>事前可觀察的統計特徵</b>'
        + '（Hurst 指數、波動度、流動性），刻意不看任何回測結果 —— '
        + '否則就變成「因為這個策略賺錢所以這檔適合它」的循環論證。</p>'
        + '<div class="tablewrap"><table><thead><tr><th>分群</th>'
        + '<th class="num">檔數</th><th class="num">Hurst</th>'
        + '<th class="num">ATR%</th><th>說明</th></tr></thead><tbody>';
      a.groups.forEach(function (g) {
        html += '<tr><td><b>' + esc(g.name) + '</b></td>'
          + num(g.count, 0) + num(g.hurst, 3) + num(g.atr_pct, 2)
          + '<td class="muted">' + esc(g.note) + '</td></tr>';
      });
      html += '</tbody></table></div></div>';
    }

    if (a.verdict) {
      html += '<div class="card"><h2>結構判斷</h2><p>' + esc(a.verdict)
            + '</p></div>';
    }
    box.innerHTML = html;
  }

  function emptyMsg() {
    /* 空白表格對使用者沒有幫助，要說清楚為什麼空。
       振幅榜最常見：它需要「前一交易日收盤價」當分母，
       所以本站累積的第一天一定是空的。 */
    var why;
    if (S.board === 'swing' && (S.meta.trading_days || 0) < 2) {
      why = '振幅是（最高價 − 最低價）÷ <b>前一交易日收盤價</b>，'
          + '所以本站累積的第一個交易日沒有東西可以比。明天起就會有。';
    } else if (S.board === 'chip_buy' || S.board === 'chip_sell') {
      why = '官方只提供<b>上櫃</b>的三大法人買賣超；'
          + '上市的那份官方沒有開放，本站不會有。';
    } else {
      why = '這一天沒有符合條件的資料。';
    }
    return '<tr><td colspan="7" class="muted" style="white-space:normal;'
         + 'padding:18px 10px">' + why + '</td></tr>';
  }

  function pct(v, plus) {
    if (v === null || v === undefined) return '<td class="num">—</td>';
    var cls = v > 0 ? 'up' : v < 0 ? 'down' : 'muted';
    var s = (plus === false ? '' : (v > 0 ? '+' : '')) + v.toFixed(2) + '%';
    return '<td class="num ' + cls + '">' + s + '</td>';
  }

  function num(v, nd) {
    if (v === null || v === undefined) return '<td class="num">—</td>';
    return '<td class="num">' + v.toFixed(nd === undefined ? 2 : nd) + '</td>';
  }

  /* ---------------- 說明頁 ---------------- */

  function renderAbout() {
    var m = S.meta || {};
    var html = '<div class="card"><h2>這個網站是什麼</h2>'
      + '<p>每天自動抓取臺灣證券交易所與證券櫃檯買賣中心的公開資料，'
      + '計算成交量、成交金額、振幅、三大法人四個面向的排行榜，'
      + '並標記每檔股票連續在榜幾天、是不是今天才進榜。</p>'
      + '<p class="muted">排名是<b>自己計算</b>的，不是直接拿官方的排行榜。'
      + '因為官方的排行只有前 20 名、分上市上櫃兩份，而且'
      + '<b>今天的前 20 名裡有 13 檔是 ETF</b> —— '
      + '直接用的話「前 100 檔股票」會有三分之二不是股票。</p></div>';

    html += '<div class="card"><h2>資料更新</h2><dl class="kv">'
      + '<dt>自動抓取</dt><dd>每個交易日台灣時間 18:30，'
      + '若當時官方尚未更新，22:30 會再試一次</dd>'
      + '<dt>資料日期</dt><dd>' + esc(m.data_date || '—') + '</dd>'
      + '<dt>累積天數</dt><dd>' + esc(m.trading_days || 0) + ' 個交易日'
      + (m.first_date ? '（自 ' + esc(m.first_date) + ' 起）' : '') + '</dd>'
      + '<dt>白名單</dt><dd>' + esc(m.stocks || 0) + ' 檔</dd>'
      + '</dl>'
      + '<p class="muted">★ 歷史是<b>從本站上線那天開始累積</b>的。'
      + '官方 API 不支援查詢過去的日期（實測確認），所以補不回來。'
      + '「連續在榜天數」要累積一段時間才有參考價值。</p></div>';

    html += '<div class="card"><h2>榜單怎麼算</h2>'
      + '<dl class="kv">'
      + '<dt>成交量榜</dt><dd>' + esc(glShort('board.volume')) + '</dd>'
      + '<dt>成交金額榜</dt><dd>' + esc(glShort('board.amount')) + '</dd>'
      + '<dt>振幅榜</dt><dd>' + esc(glShort('board.swing')) + '</dd>'
      + '<dt>三大法人榜</dt><dd>' + esc(glShort('board.chip')) + '</dd>'
      + '</dl>'
      + '<p class="muted">榜單排除 ETF、權證、特別股、TDR 與興櫃 —— '
      + '只保留「真正的個股」。做法是用官方的<b>上市／上櫃公司基本資料</b>'
      + '當白名單（ETF 不是公司，根本不在那張表裡），'
      + '再加一層「代號恰好四位純數字」。</p></div>';

    html += '<div class="card"><h2>名詞說明</h2><dl class="kv">';
    Object.keys(S.gl).forEach(function (k) {
      var g = S.gl[k];
      html += '<dt>' + esc(g.label) + '</dt><dd>' + esc(g.short) + '</dd>';
    });
    html += '</dl></div>';

    html += '<div class="card"><h2>資料來源與授權</h2>'
      + '<p>' + esc(m.license || '') + '</p>'
      + '<p class="muted">分析與選股的部分使用了其他來源的歷史資料，'
      + '那些資料的授權不允許再散布，因此本站只公開<b>計算後的結論</b>，'
      + '不公開原始資料。</p></div>';
    $('#about-body').innerHTML = html;
  }

  /* ---------------- 匯出 ---------------- */

  function currentRows() {
    var rows = S.rows.length ? S.rows : boardRows();
    return rows.filter(function (r) { return r[1] <= S.topN; });
  }

  function copyCodes() {
    var txt = currentRows().map(function (r) { return r[0]; }).join(',');
    navigator.clipboard.writeText(txt).then(function () {
      $('#btn-codes').textContent = '已複製 ' + currentRows().length + ' 檔';
      setTimeout(function () {
        $('#btn-codes').textContent = '複製代號清單';
      }, 1800);
    });
  }

  function downloadCsv() {
    var nm = names(), unit = UNIT[S.board] || '';
    var lines = ['排名,代號,名稱,指標值,連續在榜,標記,排名變化'];
    currentRows().forEach(function (r) {
      var mark = r[5] ? '回榜' : (r[4] ? '新進榜' : '連續 ' + r[3] + ' 天');
      /* 代號用 ="0050" 的文字格式，否則 Excel 會把前面的 0 吃掉 */
      lines.push([r[1], '="' + r[0] + '"', nm[r[0]] || '',
                  money(r[2], unit), r[3], mark,
                  (r[6] === null || r[6] === undefined) ? '' : r[6]].join(','));
    });
    var zh = (S.meta.board_zh || {})[S.board] || S.board;
    var blob = new Blob(['﻿' + lines.join('\n')],
                        { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = S.latest.date + '_' + zh + '_前' + S.topN + '名.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  /* ---------------- 提示框 ---------------- */

  function bindTips() {
    var tip = $('#tip');
    document.addEventListener('mouseover', function (e) {
      var el = e.target.closest('[data-gl],[data-tipraw]');
      if (!el) { tip.classList.remove('on'); return; }
      var txt = el.dataset.tipraw || glFull(el.dataset.gl);
      if (!txt) { tip.classList.remove('on'); return; }
      tip.textContent = txt;
      tip.classList.add('on');
      var r = el.getBoundingClientRect();
      tip.style.left = Math.min(r.left, innerWidth - tip.offsetWidth - 12) + 'px';
      tip.style.top = (r.bottom + 6) + 'px';
    });
    document.addEventListener('mouseout', function () {
      tip.classList.remove('on');
    });
  }

  /* ---------------- 啟動 ---------------- */

  function boot() {
    Promise.all([
      get('data/meta.json'),
      get('data/latest.json'),
      get('data/glossary.json'),
      get('data/universe.json')
    ]).then(function (r) {
      S.meta = r[0]; S.latest = r[1]; S.gl = r[2];
      S.universe = {};
      Object.keys(r[3].stocks || {}).forEach(function (k) {
        S.universe[k] = r[3].stocks[k][0];
      });
      S.board = (S.meta.boards || ['volume'])[0];
      S.topN = (S.meta.top_n || {})[S.board] || 100;
      $('#top-sel').value = String(S.topN);
      $('#date-sel').innerHTML =
        '<option>' + esc(S.latest.date) + '</option>';
      renderFreshness();
      renderTabs();
      show('board');
      return get('data/analysis/summary.json').catch(function () { return null; });
    }).then(function (a) {
      if (a) { S.analysis = a; renderFreshness(); markTab(); }
    }).catch(function (e) {
      $('#freshness').innerHTML =
        '<span class="lag">載入失敗：' + esc(e.message) + '</span>';
      console.error(e);
    });

    $('#top-sel').addEventListener('change', function () {
      S.topN = parseInt(this.value, 10) || 100;
      renderBoard();
    });
    $('#btn-codes').addEventListener('click', copyCodes);
    $('#btn-csv').addEventListener('click', downloadCsv);
    document.querySelector('#board-table thead')
      .addEventListener('click', function (e) {
        var th = e.target.closest('th[data-sort]');
        if (!th) return;
        if (S.sortKey === th.dataset.sort) S.sortDesc = !S.sortDesc;
        else { S.sortKey = th.dataset.sort; S.sortDesc = false; }
        renderBoard();
      });
    bindTips();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
