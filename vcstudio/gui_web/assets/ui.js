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
  function tr(key, fallback, params) {
    const vcs = window.VCS;
    return vcs && typeof vcs.t === 'function' ? vcs.t(key, params || {}, fallback) : fallback;
  }

  function localizedAccordionSummary(el) {
    const fallback = el.getAttribute('data-sum') || '';
    const key = el.getAttribute('data-i18n-sum') || '';
    return key ? tr(key, fallback) : fallback;
  }

  function refreshAccordionSummaries(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-acc][data-sum]').forEach(el => {
      const head = el.querySelector(':scope > .acc-h > button.acc-toggle');
      const summaries = head ? Array.from(head.querySelectorAll(':scope > .acc-sum')) : [];
      const summary = summaries.shift() || null;
      summaries.forEach(extra => extra.remove());
      if (summary) summary.textContent = localizedAccordionSummary(el);
    });
  }

  // ── 1) 分区手风琴 ──────────────────────────────────────────────
  // 约定:分区根元素带 data-acc="page:key",内部含直接子标题 .acc-h，
  // 标题内必须使用原生 button.acc-toggle。按钮获得确定性的 aria-controls，
  // 每个被控制的直接子内容也获得稳定 id；点击切换 data-open，每页默认仅展开首个。
  function enhanceAccordion(root) {
    const seenPage = {};
    root.querySelectorAll('[data-acc]').forEach(el => {
      if (el._accReady) return;
      const heading = el.querySelector(':scope > .acc-h');
      const head = heading && heading.querySelector(':scope > button.acc-toggle');
      if (!heading || !head) return;
      el._accReady = true;
      const key = el.getAttribute('data-acc') || '';
      const page = key.split(':')[0];
      if (!head.querySelector('.acc-caret')) {
        const car = document.createElement('span');
        car.className = 'acc-caret';
        car.setAttribute('aria-hidden', 'true');
        head.insertBefore(car, head.firstChild);
      }
      const sum = localizedAccordionSummary(el);
      const existingSummaries = Array.from(head.querySelectorAll(':scope > .acc-sum'));
      existingSummaries.slice(1).forEach(extra => extra.remove());
      if (sum && !existingSummaries.length) {
        const s = document.createElement('span');
        s.className = 'acc-sum';
        s.textContent = sum;
        head.appendChild(s);
      }
      const idStem = (key || ('section-' + (++accordionSequence)))
        .replace(/[^A-Za-z0-9_-]+/g, '-');
      const panels = Array.from(el.children).filter(child => child !== heading);
      panels.forEach((panel, index) => {
        if (!panel.id) panel.id = 'vcs-acc-' + idStem + '-panel-' + (index + 1);
      });
      if (panels.length) head.setAttribute('aria-controls', panels.map(panel => panel.id).join(' '));
      const first = !seenPage[page];
      seenPage[page] = true;
      const stored = LS.get('vcs.acc.' + key, null);
      const dflt = el.getAttribute('data-acc-default');   // 显式默认态优先于"每页首个"规则
      let open = stored != null ? stored : (dflt != null ? dflt : (first ? '1' : '0'));
      apply(open);
      head.type = 'button';
      function apply(v) {
        el.setAttribute('data-open', v);
        head.setAttribute('aria-expanded', v === '1' ? 'true' : 'false');
      }
      el._accSetOpen = (value, persist = false) => {
        open = value === true || value === '1' ? '1' : '0';
        apply(open);
        if (persist) LS.set('vcs.acc.' + key, open);
      };
      function toggle() {
        open = open === '1' ? '0' : '1';
        el._accSetOpen(open, true);
      }
      head.addEventListener('click', toggle);
    });
  }

  // ── 2) 多选下拉 ───────────────────────────────────────────────
  // multiselect(host, opts): 保留 host(原容器)id,清空内部改渲染。
  // opts.groups:[{group,items:[{val,label,exp,note}]}] 或 opts.items:[{val,label,exp,note}]
  // opts.selected:[val...] · opts.placeholder · opts.onChange(selArr)
  // 返回 controller,并挂到 host._ms:{getSelected,setSelected,setGroups,setItems}
  let accordionSequence = 0;
  let multiselectSequence = 0;
  let segmentedSequence = 0;
  function setPopOpen(pop, open, returnFocus) {
    if (!pop) return;
    pop.hidden = !open;
    const button = pop._msButton;
    if (button) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open && returnFocus) button.focus();
    }
  }
  function closeAllPops(except) {
    document.querySelectorAll('.ms-pop').forEach(p => {
      if (p !== except) setPopOpen(p, false, false);
    });
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
    const idStem = host.id ? host.id.replace(/[^A-Za-z0-9_-]/g, '-')
      : 'vcs-multiselect-' + (++multiselectSequence);
    btn.id = idStem + '-toggle';
    pop.id = idStem + '-options';
    btn.setAttribute('aria-controls', pop.id);
    btn.setAttribute('aria-expanded', 'false');
    pop.setAttribute('role', 'group');
    pop.setAttribute('aria-labelledby', btn.id);
    pop._msButton = btn;

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
      btn.textContent = n
        ? tr('multiselect.selected_count', '已选 {count} 项', { count: n })
        : (opts.placeholder || '点此选择');
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
      pills.innerHTML = Array.from(state.sel).map(v => {
        const label = labelOf(v);
        const remove = tr('multiselect.remove', '移除 {label}', { label });
        return '<button type="button" class="ms-pill" data-v="' + esc(v) +
          '" aria-label="' + esc(remove) + '">' + esc(label) +
          '<b aria-hidden="true">×</b></button>';
      }).join('');
    }
    function fire() { renderBtn(); renderPills(); if (opts.onChange) opts.onChange(Array.from(state.sel)); }

    btn.addEventListener('click', e => {
      e.stopPropagation();
      const willShow = pop.hidden;
      closeAllPops(pop);
      setPopOpen(pop, willShow, false);
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
    host.addEventListener('keydown', e => {
      if (e.key !== 'Escape' || pop.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      setPopOpen(pop, false, true);
    });
    document.addEventListener('click', e => {
      if (!host.contains(e.target)) setPopOpen(pop, false, false);
    });

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
      seg.setAttribute('role', 'group');
      if (!seg.hasAttribute('aria-label') && !seg.hasAttribute('aria-labelledby')) {
        const bar = seg.closest('.seg-bar');
        const label = bar && bar.querySelector('.seg-lbl');
        if (label) {
          if (!label.id) label.id = 'vcs-segment-label-' + (++segmentedSequence);
          seg.setAttribute('aria-labelledby', label.id);
        } else {
          seg.setAttribute('aria-label', '切换选项');
        }
      }
      btns.forEach(b => { b.type = 'button'; });
      const saved = store ? LS.get('vcs.seg.' + store, null) : null;
      let cur = (saved != null && btns.some(b => b.dataset.segVal === saved)) ? saved
        : (btns[0] ? btns[0].dataset.segVal : '');
      const run = v => {
        cur = v;
        btns.forEach(b => {
          const selected = b.dataset.segVal === v;
          b.classList.toggle('on', selected);
          b.setAttribute('aria-pressed', selected ? 'true' : 'false');
          b.setAttribute('tabindex', selected ? '0' : '-1');
        });
        groupToggle(page, attr, v);
        if (store) LS.set('vcs.seg.' + store, v);
      };
      seg.addEventListener('click', e => {
        const b = e.target.closest('button[data-seg-val]');
        if (b) run(b.dataset.segVal);
      });
      seg.addEventListener('keydown', e => {
        const b = e.target.closest('button[data-seg-val]');
        if (!b || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
        const enabled = btns.filter(item => !item.disabled);
        if (!enabled.length) return;
        const index = Math.max(0, enabled.indexOf(b));
        let next;
        if (e.key === 'Home') next = enabled[0];
        else if (e.key === 'End') next = enabled[enabled.length - 1];
        else {
          const step = e.key === 'ArrowRight' ? 1 : -1;
          next = enabled[(index + step + enabled.length) % enabled.length];
        }
        e.preventDefault();
        next.click();
        next.focus();
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

  function setAccordionOpen(target, value, persist = false) {
    const el = typeof target === 'string' ? document.querySelector(target) : target;
    if (!el || !el.matches || !el.matches('[data-acc]')) return false;
    const open = value === true || value === '1';
    if (typeof el._accSetOpen === 'function') {
      el._accSetOpen(open, persist);
      return true;
    }
    el.setAttribute('data-open', open ? '1' : '0');
    const toggle = el.querySelector(':scope > .acc-h > button.acc-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (persist) {
      const key = el.getAttribute('data-acc') || '';
      if (key) LS.set('vcs.acc.' + key, open ? '1' : '0');
    }
    return true;
  }

  window.VCS = window.VCS || {};
  window.VCS.ui = {
    multiselect, groupToggle, enhanceAll,
    enhanceAccordion, enhanceSwitchers, enhanceSegmented, setAccordionOpen,
    refreshAccordionSummaries, esc,
  };

  document.addEventListener('vcs:language', () => {
    refreshAccordionSummaries(document);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => enhanceAll(document));
  } else {
    enhanceAll(document);
  }
})();
