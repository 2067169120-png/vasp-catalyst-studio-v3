// ui.js — v3.2 紧凑重设计通用组件。参照 starpivot DFT 紧凑排版:
//   1) 分区手风琴(标题+摘要+箭头,每页默认仅展开首个;状态存 localStorage)
//   2) 多选下拉(替代 chips 墙:已选 N 项按钮 + 浮层复选 + pill 摘要)
//   3) 分区切换器(下拉/分段:选中只显示对应分区,用于④分析类型、①结构来源)
// 纯前端;不触碰任何 api 契约;保留调用方容器 id,只改内部渲染。零 emoji。
'use strict';
(function () {
  const LS = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式忽略 */ } },
  };
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ── 1) 分区手风琴 ──────────────────────────────────────────────
  // 约定:分区根元素带 data-acc="page:key",内部含直接子 .acc-h 头部。
  // 头部注入箭头 + 摘要(取 data-sum);点击/键盘切换 data-open;每页默认仅展开首个。
  function enhanceAccordion(root) {
    const seenPage = {};
    root.querySelectorAll('[data-acc]').forEach(el => {
      if (el._accReady) return;
      const head = el.querySelector(':scope > .acc-h');
      if (!head) return;
      el._accReady = true;
      const key = el.getAttribute('data-acc') || '';
      const page = key.split(':')[0];
      if (!head.querySelector('.acc-caret')) {
        const car = document.createElement('span');
        car.className = 'acc-caret';
        car.setAttribute('aria-hidden', 'true');
        head.insertBefore(car, head.firstChild);
      }
      const sum = el.getAttribute('data-sum');
      if (sum && !head.querySelector('.acc-sum')) {
        const s = document.createElement('span');
        s.className = 'acc-sum';
        s.textContent = sum;
        head.appendChild(s);
      }
      const first = !seenPage[page];
      seenPage[page] = true;
      const stored = LS.get('vcs.acc.' + key, null);
      const dflt = el.getAttribute('data-acc-default');   // 显式默认态优先于"每页首个"规则
      let open = stored != null ? stored : (dflt != null ? dflt : (first ? '1' : '0'));
      apply(open);
      head.setAttribute('role', 'button');
      head.setAttribute('tabindex', '0');
      function apply(v) {
        el.setAttribute('data-open', v);
        head.setAttribute('aria-expanded', v === '1' ? 'true' : 'false');
      }
      function toggle() {
        open = open === '1' ? '0' : '1';
        apply(open);
        LS.set('vcs.acc.' + key, open);
      }
      head.addEventListener('click', toggle);
      head.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      });
    });
  }

  // ── 2) 多选下拉 ───────────────────────────────────────────────
  // multiselect(host, opts): 保留 host(原容器)id,清空内部改渲染。
  // opts.groups:[{group,items:[{val,label,exp,note}]}] 或 opts.items:[{val,label,exp,note}]
  // opts.selected:[val...] · opts.placeholder · opts.onChange(selArr)
  // 返回 controller,并挂到 host._ms:{getSelected,setSelected,setGroups,setItems}
  function closeAllPops(except) {
    document.querySelectorAll('.ms-pop').forEach(p => { if (p !== except) p.hidden = true; });
  }
  function multiselect(host, opts) {
    opts = opts || {};
    const state = { sel: new Set(opts.selected || []), groups: normGroups(opts) };
    host.classList.add('ms');
    host.innerHTML = '';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ms-btn';
    const pop = document.createElement('div');
    pop.className = 'ms-pop';
    pop.hidden = true;
    const pills = document.createElement('div');
    pills.className = 'ms-pills';
    host.appendChild(btn);
    host.appendChild(pop);
    host.appendChild(pills);

    function normGroups(o) {
      if (o.groups && o.groups.length) return o.groups;
      return [{ group: '', items: (o.items || []) }];
    }
    function allItems() {
      const out = [];
      state.groups.forEach(g => (g.items || []).forEach(it => out.push(it)));
      return out;
    }
    function labelOf(val) {
      const hit = allItems().find(it => it.val === val);
      return hit ? hit.label : val;
    }
    function renderBtn() {
      const n = state.sel.size;
      btn.textContent = n ? ('已选 ' + n + ' 项') : (opts.placeholder || '点此选择');
      btn.classList.toggle('has', n > 0);
    }
    function renderPop() {
      pop.innerHTML = state.groups.map(g => {
        const items = (g.items || []).map(it =>
          '<label class="ms-opt' + (it.exp ? ' exp' : '') + '"' +
          (it.note ? ' title="' + esc(it.note) + '"' : '') + '>' +
          '<input type="checkbox" data-v="' + esc(it.val) + '"' + (state.sel.has(it.val) ? ' checked' : '') + '>' +
          '<span>' + esc(it.label) + (it.exp ? ' <i>实验性</i>' : '') + '</span></label>').join('');
        return (g.group ? '<div class="ms-grp">' + esc(g.group) + '</div>' : '') + items;
      }).join('') || '<div class="ms-empty">暂无可选项</div>';
    }
    function renderPills() {
      pills.innerHTML = Array.from(state.sel).map(v =>
        '<span class="ms-pill" data-v="' + esc(v) + '">' + esc(labelOf(v)) + '<b aria-hidden="true">×</b></span>').join('');
    }
    function fire() { renderBtn(); renderPills(); if (opts.onChange) opts.onChange(Array.from(state.sel)); }

    btn.addEventListener('click', e => {
      e.stopPropagation();
      const willShow = pop.hidden;
      closeAllPops(pop);
      pop.hidden = !willShow;
    });
    pop.addEventListener('change', e => {
      const cb = e.target.closest('input[data-v]');
      if (!cb) return;
      const v = cb.getAttribute('data-v');
      if (cb.checked) state.sel.add(v); else state.sel.delete(v);
      fire();
    });
    pills.addEventListener('click', e => {
      const p = e.target.closest('.ms-pill');
      if (!p) return;
      state.sel.delete(p.getAttribute('data-v'));
      renderPop();
      fire();
    });
    document.addEventListener('click', e => { if (!host.contains(e.target)) pop.hidden = true; });

    renderPop(); renderBtn(); renderPills();
    const ctrl = {
      getSelected() { return Array.from(state.sel); },
      setSelected(arr) { state.sel = new Set(arr || []); renderPop(); renderBtn(); renderPills(); },
      setGroups(groups, keepSel) {
        state.groups = (groups && groups.length) ? groups : [{ group: '', items: [] }];
        if (!keepSel) state.sel = new Set();
        renderPop(); renderBtn(); renderPills();
      },
      setItems(items, sel) {
        state.groups = [{ group: '', items: items || [] }];
        if (sel) state.sel = new Set(sel);
        renderPop(); renderBtn(); renderPills();
      },
    };
    host._ms = ctrl;
    return ctrl;
  }

  // ── 3) 分区切换器 ─────────────────────────────────────────────
  // groupToggle(scope, attr, value):scope 内所有 [attr] 元素,值匹配才显示(支持空格分隔多值)。
  function groupToggle(scope, attr, value) {
    scope.querySelectorAll('[' + attr + ']').forEach(el => {
      const tokens = (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean);
      el.hidden = tokens.indexOf(value) < 0;
    });
  }
  // 下拉切换:<select data-switch="attr" data-switch-store="key"> 控本页 [attr]
  function enhanceSwitchers(root) {
    root.querySelectorAll('select[data-switch]').forEach(sel => {
      if (sel._swReady) return;
      sel._swReady = true;
      const page = sel.closest('[data-page]') || document;
      const attr = sel.getAttribute('data-switch');
      const store = sel.getAttribute('data-switch-store');
      const saved = store ? LS.get('vcs.sw.' + store, null) : null;
      if (saved != null && Array.from(sel.options).some(o => o.value === saved)) sel.value = saved;
      const run = () => {
        groupToggle(page, attr, sel.value);
        if (store) LS.set('vcs.sw.' + store, sel.value);
      };
      sel.addEventListener('change', run);
      run();
    });
  }
  // 分段控件:.seg[data-seg="attr"][data-seg-store] > button[data-seg-val]
  function enhanceSegmented(root) {
    root.querySelectorAll('.seg[data-seg]').forEach(seg => {
      if (seg._segReady) return;
      seg._segReady = true;
      const page = seg.closest('[data-page]') || document;
      const attr = seg.getAttribute('data-seg');
      const store = seg.getAttribute('data-seg-store');
      const btns = Array.from(seg.querySelectorAll('button[data-seg-val]'));
      const saved = store ? LS.get('vcs.seg.' + store, null) : null;
      let cur = (saved != null && btns.some(b => b.dataset.segVal === saved)) ? saved
        : (btns[0] ? btns[0].dataset.segVal : '');
      const run = v => {
        cur = v;
        btns.forEach(b => b.classList.toggle('on', b.dataset.segVal === v));
        groupToggle(page, attr, v);
        if (store) LS.set('vcs.seg.' + store, v);
      };
      seg.addEventListener('click', e => {
        const b = e.target.closest('button[data-seg-val]');
        if (b) run(b.dataset.segVal);
      });
      run(cur);
    });
  }

  function enhanceAll(root) {
    root = root || document;
    enhanceAccordion(root);
    enhanceSegmented(root);
    enhanceSwitchers(root);
  }

  window.VCS = window.VCS || {};
  window.VCS.ui = {
    multiselect, groupToggle, enhanceAll,
    enhanceAccordion, enhanceSwitchers, enhanceSegmented, esc,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => enhanceAll(document));
  } else {
    enhanceAll(document);
  }
})();
