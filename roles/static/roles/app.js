/* =========================================================================
 * Britam Group Role Library — client application
 *
 * Replaces the legacy inline <script>. Behaviour of Browse / Compare /
 * Career Path / Role detail is preserved exactly; what changed:
 *
 *   1. Data comes from GET /api/roles/ instead of a hard-coded array, so the
 *      page reflects whatever is in SQLite.
 *   2. Roles are addressed by database id, not array index. Indexes broke as
 *      soon as rows could be inserted or hidden.
 *   3. Every value interpolated into innerHTML goes through esc(). The old
 *      page trusted its data because the data was baked in by a developer.
 *      Now HR staff type it, so an unescaped "<img onerror=...>" in a role
 *      purpose would be stored XSS against every visitor.
 *   4. The AI Assistant posts to /api/ai/ — the API key never reaches here.
 *   5. New Manage tab: create / edit / hide / delete, staff only.
 *   6. alert() replaced with inline banners (a blocked modal dialog freezes
 *      the whole page on mobile Safari).
 * ====================================================================== */

(function () {
  'use strict';

  // ----------------------------------------------------------------------
  // Configuration handed over by the Django template
  // ----------------------------------------------------------------------
  var CONFIG = { canEdit: false, aiEnabled: false, version: 'dev' };
  try {
    var configEl = document.getElementById('app-config');
    if (configEl) { CONFIG = JSON.parse(configEl.textContent); }
  } catch (err) {
    console.error('[role-library] could not read app config', err);
  }

  var PAGES = ['browse', 'compare', 'career', 'ai', 'manage'];

  var state = {
    roles: [],          // array, display order
    byId: {},           // id -> role
    bus: [],            // [{id, name, slug, role_count}]
    bands: [],
    levels: [],
    currentBU: 'all',
    slotA: null,
    slotB: null,
    loaded: false,
    deleteTarget: null
  };

  // ======================================================================
  // Utilities
  // ======================================================================

  /** Escape a value for safe interpolation into innerHTML. */
  function esc(value) {
    if (value === null || value === undefined) { return ''; }
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** Escape for use inside a single-quoted JS string in an inline handler. */
  function escAttr(value) {
    return esc(value).replace(/\\/g, '\\\\');
  }

  function byId(id) { return document.getElementById(id); }

  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : null;
  }

  /** Band badge colour class, unchanged from the legacy page. */
  function bc(band) {
    if (!band) { return 'b6'; }
    var n = parseFloat(String(band).replace(/[^0-9.]/g, ''));
    if (isNaN(n)) { return 'b6'; }
    if (n < 2) { return 'b1'; } if (n < 3) { return 'b2'; }
    if (n < 4) { return 'b3'; } if (n < 5) { return 'b4'; }
    if (n < 6) { return 'b5'; } if (n < 7) { return 'b6'; }
    if (n < 8) { return 'b7'; } if (n < 9) { return 'b8'; }
    if (n < 10) { return 'b9'; }
    return 'b10';
  }

  function bandSortValue(band) {
    var n = parseFloat(String(band || '').replace(/[^0-9.]/g, ''));
    return isNaN(n) ? 999 : n;
  }

  // ======================================================================
  // API client
  // ======================================================================

  var API = {
    /**
     * fetch wrapper that unwraps the {error:{code,message,details}} envelope
     * and retries idempotent GETs with exponential backoff. A dropped request
     * on a phone switching between wifi and mobile data is common enough that
     * one retry saves a support call.
     */
    request: function (url, options, attempt) {
      options = options || {};
      attempt = attempt || 1;
      var maxAttempts = (!options.method || options.method === 'GET') ? 3 : 1;

      var headers = { 'Accept': 'application/json' };
      if (options.body) { headers['Content-Type'] = 'application/json'; }
      if (options.method && options.method !== 'GET' && options.method !== 'HEAD') {
        var token = getCookie('csrftoken');
        if (token) { headers['X-CSRFToken'] = token; }
      }
      Object.keys(options.headers || {}).forEach(function (key) {
        headers[key] = options.headers[key];
      });

      return fetch(url, {
        method: options.method || 'GET',
        headers: headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
        credentials: 'same-origin'
      }).then(function (response) {
        if (response.status === 204) { return null; }
        return response.text().then(function (text) {
          var payload = null;
          if (text) {
            try { payload = JSON.parse(text); }
            catch (err) { payload = null; }
          }
          if (!response.ok) {
            var envelope = (payload && payload.error) || {};
            var error = new Error(envelope.message || ('Request failed (HTTP ' + response.status + ')'));
            error.code = envelope.code || ('HTTP-' + response.status);
            error.details = envelope.details || {};
            error.status = response.status;
            error.requestId = envelope.request_id || response.headers.get('X-Request-ID') || '';
            throw error;
          }
          return payload;
        });
      }).catch(function (error) {
        var isNetwork = !error.status;
        if (isNetwork && attempt < maxAttempts) {
          var delay = 300 * Math.pow(2, attempt - 1);
          return new Promise(function (resolve) {
            setTimeout(resolve, delay + Math.random() * 150);
          }).then(function () {
            return API.request(url, options, attempt + 1);
          });
        }
        if (isNetwork) {
          error.code = 'NET-OFFLINE';
          error.message = 'Could not reach the server. Check your connection and try again.';
        }
        throw error;
      });
    },

    listRoles: function (includeInactive) {
      var url = '/api/roles/?page_size=2000&ordering=position';
      if (includeInactive) { url += '&include_inactive=1&active='; }
      return API.request(url);
    },
    meta: function () { return API.request('/api/meta/'); },
    createRole: function (payload) {
      return API.request('/api/roles/', { method: 'POST', body: payload });
    },
    updateRole: function (id, payload) {
      return API.request('/api/roles/' + encodeURIComponent(id) + '/', { method: 'PUT', body: payload });
    },
    deleteRole: function (id) {
      return API.request('/api/roles/' + encodeURIComponent(id) + '/', { method: 'DELETE' });
    },
    ask: function (question) {
      return API.request('/api/ai/', { method: 'POST', body: { question: question } });
    }
  };

  // ======================================================================
  // Bootstrap
  // ======================================================================

  function normalise(record) {
    // The API already emits legacy-compatible aliases (pos, bu, desc, ...).
    // Guarantee the fields the render code reads are strings, never null.
    return {
      id: record.id,
      bu: record.bu || '',
      bu_id: record.bu_id || null,
      pos: record.position || record.pos || '',
      band: record.band || '',
      bandN: (record.bandN === null || record.bandN === undefined) ? null : Number(record.bandN),
      level: record.level || '',
      exp: record.experience || '',
      quals: record.qualifications || '',
      desc: record.purpose || '',
      focus: record.focus_areas || '',
      kras: record.kras || '',
      reports: record.direct_reports || '',
      techcomp: record.technical_competencies || '',
      leadcomp: record.leadership_competencies || '',
      is_active: record.is_active !== false
    };
  }

  function load(includeInactive) {
    return Promise.all([API.listRoles(includeInactive), API.meta()])
      .then(function (results) {
        var rolesPayload = results[0];
        var meta = results[1];
        var rows = (rolesPayload && rolesPayload.results) ? rolesPayload.results : (rolesPayload || []);

        state.roles = rows.map(normalise);
        state.byId = {};
        state.roles.forEach(function (role) { state.byId[role.id] = role; });

        state.bus = (meta && meta.business_units) || [];
        state.bands = (meta && meta.bands) || [];
        state.levels = (meta && meta.levels) || [];
        state.counts = (meta && meta.counts) || {};
        state.aiEnabled = !!(meta && meta.ai_enabled);
        state.loaded = true;
        return state;
      });
  }

  function init() {
    var loadState = byId('load-state');

    load(false).then(function () {
      if (loadState) { loadState.style.display = 'none'; }
      populateStats();
      populateFilters();
      populateBUTabs();
      populateCareerSelects();
      filterRoles();

      if (CONFIG.canEdit) {
        var tab = byId('tab-manage');
        var bnav = byId('bnav-manage');
        if (tab) { tab.style.display = ''; }
        if (bnav) { bnav.style.display = ''; }
        Manage.init();
      }

      // Deep link support: /?page=manage&role=123
      var params = new URLSearchParams(window.location.search);
      var page = params.get('page');
      if (page && PAGES.indexOf(page) !== -1 && (page !== 'manage' || CONFIG.canEdit)) {
        showPage(page);
      }
      var roleParam = params.get('role');
      if (roleParam && state.byId[roleParam]) { openModal(roleParam); }
    }).catch(function (error) {
      console.error('[role-library] initial load failed', error);
      if (loadState) {
        loadState.innerHTML =
          '<strong>The role library could not be loaded.</strong><br>' +
          esc(error.message) +
          '<br><span style="font-size:11px">Error code: ' + esc(error.code || 'UNKNOWN') +
          (error.requestId ? ' &middot; request ' + esc(error.requestId) : '') + '</span>' +
          '<br><button class="mg-btn secondary" style="margin-top:12px" onclick="window.location.reload()">Retry</button>';
      }
    });
  }

  function populateStats() {
    var counts = state.counts || {};
    var setText = function (id, value) {
      var el = byId(id);
      if (el) { el.textContent = value; }
    };
    setText('stat-roles', counts.roles !== undefined ? counts.roles : state.roles.length);
    setText('stat-bus', counts.business_units !== undefined ? counts.business_units : state.bus.length);
    setText('stat-bands', counts.bands !== undefined ? counts.bands : state.bands.length);
  }

  function populateFilters() {
    var buFilter = byId('bu-filter');
    var bandFilter = byId('band-filter');
    var levelFilter = byId('level-filter');

    if (buFilter) {
      buFilter.innerHTML = '<option value="">All BUs / Functions</option>' +
        state.bus.map(function (bu) {
          return '<option value="' + esc(bu.name) + '">' + esc(bu.name) + '</option>';
        }).join('');
    }
    if (bandFilter) {
      bandFilter.innerHTML = '<option value="">All bands</option>' +
        state.bands.map(function (band) {
          return '<option value="' + esc(band) + '">' + esc(band) + '</option>';
        }).join('');
    }
    if (levelFilter) {
      levelFilter.innerHTML = '<option value="">All levels</option>' +
        state.levels.map(function (level) {
          return '<option value="' + esc(level) + '">' + esc(level) + '</option>';
        }).join('');
    }
  }

  function populateBUTabs() {
    var tabs = byId('bu-tabs');
    if (!tabs) { return; }
    tabs.innerHTML =
      '<button class="bu-tab active" data-bu="all" type="button">All roles</button>' +
      state.bus.map(function (bu) {
        return '<button class="bu-tab" data-bu="' + esc(bu.name) + '" type="button">' + esc(bu.name) + '</button>';
      }).join('');
    // Delegated listener rather than an inline onclick with an interpolated
    // name — BU names contain quotes and ampersands ("Foundation & IR").
    tabs.addEventListener('click', function (event) {
      var button = event.target.closest('.bu-tab');
      if (!button) { return; }
      setBU(button.getAttribute('data-bu'));
    });
  }

  function populateCareerSelects() {
    var current = byId('cp-current');
    var target = byId('cp-target');
    if (!current || !target) { return; }
    var options = '<option value="">Select your role&hellip;</option>' +
      state.roles.map(function (role) {
        return '<option value="' + esc(role.id) + '">' +
          esc(role.pos) + ' (' + esc(role.bu) + ' · ' + esc(role.band) + ')</option>';
      }).join('');
    current.innerHTML = options;
    target.innerHTML = options.replace('Select your role&hellip;', 'Select your target role&hellip;');
  }

  // ======================================================================
  // Browse
  // ======================================================================

  function setBU(value) {
    state.currentBU = value;
    document.querySelectorAll('.bu-tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.getAttribute('data-bu') === value);
    });
    filterRoles();
  }

  function filterRoles() {
    var searchEl = byId('search');
    var q = (searchEl ? searchEl.value : '').toLowerCase();
    var bu = (byId('bu-filter') || {}).value || '';
    var band = (byId('band-filter') || {}).value || '';
    var level = (byId('level-filter') || {}).value || '';

    var matches = state.roles.filter(function (role) {
      if (state.currentBU !== 'all' && role.bu !== state.currentBU) { return false; }
      if (bu && role.bu !== bu) { return false; }
      if (band && role.band !== band) { return false; }
      if (level && role.level !== level) { return false; }
      if (q) {
        var haystack = [role.pos, role.bu, role.band, role.level, role.desc,
          role.focus, role.quals, role.kras, role.reports, role.techcomp,
          role.leadcomp].join(' ').toLowerCase();
        if (haystack.indexOf(q) === -1) { return false; }
      }
      return true;
    });

    var grid = byId('roles-grid');
    var none = byId('no-results');
    if (!grid) { return; }

    if (!matches.length) {
      grid.innerHTML = '';
      if (none) { none.style.display = 'block'; }
      return;
    }
    if (none) { none.style.display = 'none'; }

    grid.innerHTML = matches.map(function (role) {
      return '<div class="role-card" data-role-id="' + esc(role.id) + '">' +
        '<div class="rc-top"><div class="rc-name">' + esc(role.pos) + '</div>' +
        '<span class="band-badge ' + bc(role.band) + '">' + esc(role.band) + '</span></div>' +
        '<div class="rc-meta">' + esc(role.bu) + (role.level ? ' · ' + esc(role.level) : '') + '</div>' +
        '<div class="rc-desc">' + esc(role.desc) + '</div>' +
        '</div>';
    }).join('');
  }

  // ======================================================================
  // Navigation
  // ======================================================================

  function showPage(page) {
    PAGES.forEach(function (name) {
      var el = byId('page-' + name);
      if (el) { el.style.display = (name === page) ? 'block' : 'none'; }
    });
    document.querySelectorAll('.nav-tab').forEach(function (tab) {
      var onclick = tab.getAttribute('onclick') || '';
      tab.classList.toggle('active', onclick.indexOf("'" + page + "'") !== -1);
    });
    PAGES.forEach(function (name) {
      var button = byId('bnav-' + name);
      if (button) { button.classList.toggle('active', name === page); }
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // ======================================================================
  // Compare
  // ======================================================================

  function searchSlot(slot) {
    var input = byId('search-' + slot);
    var drop = byId('drop-' + slot);
    if (!input || !drop) { return; }
    var q = input.value.toLowerCase();
    if (!q) { drop.classList.remove('open'); return; }

    var matches = state.roles.filter(function (role) {
      return (role.pos + ' ' + role.bu).toLowerCase().indexOf(q) !== -1;
    }).slice(0, 8);

    if (!matches.length) {
      drop.innerHTML = '<div class="slot-opt" style="color:#aaa">No roles found</div>';
      drop.classList.add('open');
      return;
    }
    drop.innerHTML = matches.map(function (role) {
      return '<div class="slot-opt" data-slot="' + esc(slot) + '" data-role-id="' + esc(role.id) + '">' +
        '<div class="slot-opt-name">' + esc(role.pos) + '</div>' +
        '<div class="slot-opt-meta">' + esc(role.bu) + ' · ' + esc(role.band) +
        (role.level ? ' · ' + esc(role.level) : '') + '</div></div>';
    }).join('');
    drop.classList.add('open');
  }

  function openDrop(slot) {
    var input = byId('search-' + slot);
    if (input && input.value) { searchSlot(slot); }
  }

  function pickSlot(slot, roleId) {
    var role = state.byId[roleId];
    if (!role) { return; }
    if (slot === 'a') { state.slotA = role; } else { state.slotB = role; }

    byId('search-' + slot).value = '';
    byId('drop-' + slot).classList.remove('open');
    byId('sel-' + slot).classList.add('show');
    byId('sel-' + slot + '-name').textContent = role.pos;
    byId('sel-' + slot + '-meta').innerHTML =
      '<span class="band-badge ' + bc(role.band) + '">' + esc(role.band) + '</span> &nbsp;' + esc(role.bu);
    renderCompare();
  }

  function clearSlot(slot) {
    if (slot === 'a') { state.slotA = null; } else { state.slotB = null; }
    byId('sel-' + slot).classList.remove('show');
    byId('search-' + slot).value = '';
    renderCompare();
  }

  function renderCompare() {
    var wrap = byId('compare-table-wrap');
    var table = byId('compare-table');
    if (!wrap || !table) { return; }
    if (!state.slotA || !state.slotB) { wrap.classList.remove('show'); return; }
    wrap.classList.add('show');

    var rows = [
      ['BU / Function', 'bu'], ['Job band', 'band'], ['Leadership level', 'level'],
      ['Experience required', 'exp'], ['Qualifications', 'quals'], ['Role purpose', 'desc'],
      ['Key focus areas', 'focus'], ['Key performance measures', 'kras'],
      ['Direct reports', 'reports'], ['Technical / functional competencies', 'techcomp'],
      ['Leadership competencies', 'leadcomp']
    ];
    table.innerHTML =
      '<tr><th>Field</th><th>' + esc(state.slotA.pos) + '</th><th>' + esc(state.slotB.pos) + '</th></tr>' +
      rows.map(function (row) {
        var label = row[0], key = row[1];
        return '<tr><td>' + esc(label) + '</td><td>' + (esc(state.slotA[key]) || '&mdash;') +
          '</td><td>' + (esc(state.slotB[key]) || '&mdash;') + '</td></tr>';
      }).join('');
  }

  // ======================================================================
  // Career path
  // ======================================================================

  function careerNotice(message, kind) {
    var result = byId('cp-result');
    if (!result) { return; }
    result.innerHTML = '<div class="mg-banner ' + (kind || 'bad') + ' show">' + esc(message) + '</div>';
    result.classList.add('show');
  }

  function buildCareerPath() {
    var currentId = (byId('cp-current') || {}).value || '';
    var targetId = (byId('cp-target') || {}).value || '';
    var years = parseInt((byId('cp-years') || {}).value, 10) || 0;
    var qual = (byId('cp-qual') || {}).value || '';

    if (!currentId || !targetId) {
      careerNotice('Please select both your current role and your target role.');
      return;
    }
    if (currentId === targetId) {
      careerNotice('Your current and target role are the same. Please choose a different target.');
      return;
    }

    var current = state.byId[currentId];
    var target = state.byId[targetId];
    if (!current || !target) {
      careerNotice('One of the selected roles is no longer available. Reload the page and try again.');
      return;
    }
    if (current.bandN === null || target.bandN === null) {
      careerNotice('One of the selected roles has no numeric job band, so a banded path cannot be built. Please pick another role.');
      return;
    }

    var between = function (role) {
      return role.id !== current.id && role.id !== target.id && role.bandN !== null &&
        role.bandN > target.bandN && role.bandN < current.bandN;
    };
    var pool = state.roles.filter(function (r) { return between(r) && r.bu === target.bu; });
    if (!pool.length) { pool = state.roles.filter(function (r) { return between(r) && r.bu === current.bu; }); }
    if (!pool.length) { pool = state.roles.filter(between); }

    var intermediates = pool.sort(function (a, b) { return b.bandN - a.bandN; }).slice(0, 2);
    var path = [current].concat(intermediates, [target]);

    var qScore = ({
      diploma: 1, bachelor: 2, masters: 3, professional: 2.5,
      both: 3.5, 'masters-prof': 4
    })[qual] || 0;

    function gaps(role, isCurrent) {
      if (isCurrent) { return []; }
      var q = (role.quals || '').toLowerCase();
      var list = [];
      if (q.indexOf('master') !== -1) { list.push({ ok: qScore >= 3, text: "Master's degree" }); }
      if (q.indexOf('bachelor') !== -1 && q.indexOf('master') === -1) {
        list.push({ ok: qScore >= 2, text: "Bachelor's degree" });
      }
      if (q.indexOf('chrp') !== -1) {
        list.push({ ok: ['both', 'masters-prof', 'professional'].indexOf(qual) !== -1, text: 'CHRP qualification' });
      }
      if (q.indexOf('ihrm practicing') !== -1) {
        list.push({ ok: qScore >= 3.5, text: 'IHRM Practicing License' });
      }
      if ((q.indexOf('cpa') !== -1 || q.indexOf('acca') !== -1) && q.indexOf('hrm') === -1) {
        list.push({ ok: qScore >= 2.5, text: 'Professional accounting qualification (CPA/ACCA)' });
      }
      var expMatch = (role.exp || '').match(/(\d+)/);
      var needed = expMatch ? parseInt(expMatch[1], 10) : 0;
      if (needed) {
        list.push({ ok: years >= needed, text: needed + '+ years of experience (you have ' + years + ')' });
      }
      return list.length ? list : [{ ok: true, text: 'Qualifications and experience appear aligned' }];
    }

    var html = '<div class="path-header"><div style="font-size:32px">&#127919;</div><div>' +
      '<h3>Your path: ' + esc(current.pos) + ' &#8594; ' + esc(target.pos) + '</h3>' +
      '<p>' + (path.length - 1) + ' step' + (path.length > 2 ? 's' : '') + ' &middot; ' +
      esc(current.band) + ' &#8594; ' + esc(target.band) + ' &middot; ' +
      esc(current.bu) + ' to ' + esc(target.bu) + '</p></div></div><div class="path-steps">';

    path.forEach(function (role, index) {
      var isFirst = index === 0;
      var isLast = index === path.length - 1;
      var tag = isFirst ? 'YOUR CURRENT ROLE' : (isLast ? 'TARGET ROLE' : 'STEP ' + index);
      var list = gaps(role, isFirst);

      html += '<div class="path-step"><div class="step-spine">' +
        '<div class="step-dot' + (isFirst ? ' current' : '') + '">' + (isFirst ? '&#9733;' : (index + 1)) + '</div>' +
        (!isLast ? '<div class="step-line"></div>' : '') +
        '</div><div class="step-body">' +
        '<div class="step-tag">' + esc(tag) + '</div>' +
        '<div class="step-role">' + esc(role.pos) + '</div>' +
        '<div class="step-meta">' + esc(role.bu) + ' &middot; ' + esc(role.band) +
        (role.level ? ' &middot; ' + esc(role.level) : '') + '</div>';

      if (!isFirst && list.length) {
        html += '<div class="step-gaps">' + list.map(function (item) {
          return '<div class="gap-item"><span class="' + (item.ok ? 'gap-ok' : 'gap-miss') + '">' +
            (item.ok ? '&#10003;' : '&#10007;') + '</span><span>' + esc(item.text) + '</span></div>';
        }).join('') + '</div>';
      }
      if (!isFirst) {
        html += '<div style="font-size:11px;color:var(--text-light);margin-top:6px">Experience required: ' +
          (esc(role.exp) || 'See role detail') + '</div>' +
          '<div style="font-size:11px;color:var(--text-light);margin-top:2px">Qualifications: ' +
          (esc(role.quals) || 'See role detail') + '</div>';
      }
      html += '</div></div>';
    });

    html += '</div><div class="reco-box"><h4>&#128161; Recommended next steps</h4><ul>' +
      '<li>Share this career plan with your HR Business Partner or line manager</li>' +
      '<li>Include your target role in your next Performance Development Review (PDR)</li>' +
      '<li>Explore L&amp;D programmes in Britam\'s learning catalogue to close qualification gaps</li>' +
      '<li>Request stretch assignments or cross-functional exposure relevant to your target BU</li>' +
      '<li>Review the target role detail and note any certifications to begin working towards</li>' +
      '</ul></div><div class="cp-actions">' +
      '<button class="cp-action-btn primary" type="button" onclick="window.print()">&#128424; Print my career plan</button>' +
      '<button class="cp-action-btn secondary" type="button" data-open-role="' + esc(target.id) + '">View target role in full</button>' +
      '</div>';

    var result = byId('cp-result');
    result.innerHTML = html;
    result.classList.add('show');
    result.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ======================================================================
  // Role detail modal
  // ======================================================================

  function section(title, body) {
    if (!body) { return ''; }
    return '<div class="modal-section"><div class="modal-section-title">' + esc(title) +
      '</div><div class="modal-section-body">' + esc(body) + '</div></div>';
  }

  function openModal(roleId) {
    var role = state.byId[roleId];
    if (!role) { return; }

    var focusTags = '';
    if (role.focus) {
      focusTags = '<div class="modal-section"><div class="modal-section-title">Key focus areas</div>' +
        '<div class="focus-tags">' + role.focus.split(/[,|]/).map(function (item) {
          var trimmed = item.trim();
          return trimmed ? '<span class="focus-tag">' + esc(trimmed) + '</span>' : '';
        }).join('') + '</div></div>';
    }

    var editButton = CONFIG.canEdit
      ? '<button class="modal-btn secondary" type="button" data-edit-role="' + esc(role.id) + '">&#9998; Edit this role</button>'
      : '';

    byId('modal-body').innerHTML =
      '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:6px">' +
      '<div class="modal-title">' + esc(role.pos) + '</div>' +
      '<span class="band-badge ' + bc(role.band) + '" style="margin-top:6px">' + esc(role.band) + '</span></div>' +
      '<div class="modal-bu">' + esc(role.bu) + (role.level ? ' &middot; ' + esc(role.level) : '') + '</div>' +
      section('Role purpose', role.desc) +
      section('Qualifications required', role.quals) +
      section('Experience required', role.exp) +
      focusTags +
      section('Key performance measures', role.kras) +
      section('Direct reports', role.reports) +
      section('Technical / functional competencies', role.techcomp) +
      section('Leadership competencies', role.leadcomp) +
      '<div class="modal-actions">' +
      '<button class="modal-btn primary" type="button" data-target-role="' + esc(role.id) + '">Set as my target role &#8594;</button>' +
      '<button class="modal-btn secondary" type="button" data-compare-role="' + esc(role.id) + '">Compare this role</button>' +
      editButton +
      '</div>';

    byId('modal').classList.add('open');
  }

  function closeModal() {
    var modal = byId('modal');
    if (modal) { modal.classList.remove('open'); }
  }

  // ======================================================================
  // AI assistant (server-side proxy)
  // ======================================================================

  var aiBusy = false;

  function askAI() {
    var input = byId('ai-q');
    var messages = byId('ai-msgs');
    if (!input || !messages || aiBusy) { return; }

    var question = input.value.trim();
    if (!question) { return; }
    if (question.length > 1000) {
      question = question.slice(0, 1000);
    }

    messages.insertAdjacentHTML('beforeend', '<div class="ai-msg user">' + esc(question) + '</div>');
    input.value = '';

    var thinkingId = 'ai-thinking-' + Date.now();
    messages.insertAdjacentHTML('beforeend',
      '<div class="ai-msg bot" id="' + thinkingId + '">Thinking&hellip;</div>');
    messages.scrollTop = messages.scrollHeight;

    aiBusy = true;
    var sendButton = document.querySelector('.ai-send');
    if (sendButton) { sendButton.disabled = true; }

    API.ask(question).then(function (payload) {
      var bubble = byId(thinkingId);
      if (bubble) { bubble.textContent = (payload && payload.answer) || 'No answer was returned.'; }
    }).catch(function (error) {
      var bubble = byId(thinkingId);
      if (bubble) {
        bubble.textContent = error.message +
          (error.code ? ' (' + error.code + ')' : '');
      }
    }).then(function () {
      aiBusy = false;
      if (sendButton) { sendButton.disabled = false; }
      messages.scrollTop = messages.scrollHeight;
    });
  }

  // ======================================================================
  // Manage tab (staff only)
  // ======================================================================

  var FORM_FIELDS = [
    'position', 'band', 'level', 'purpose', 'experience', 'qualifications',
    'focus_areas', 'kras', 'direct_reports', 'technical_competencies',
    'leadership_competencies'
  ];

  var Manage = {
    rows: [],

    init: function () {
      Manage.reload();
    },

    reload: function () {
      var showInactive = !!(byId('mg-show-inactive') || {}).checked;
      return load(showInactive).then(function () {
        Manage.rows = state.roles.slice();
        Manage.populateOptions();
        Manage.renderList();
        // Browse must reflect edits immediately too.
        populateStats();
        populateFilters();
        populateCareerSelects();
        filterRoles();
      }).catch(function (error) {
        Manage.banner('Could not load roles: ' + error.message, 'bad');
      });
    },

    populateOptions: function () {
      var buList = byId('mg-bu-options');
      var buFilter = byId('mg-bu-filter');
      var levelList = byId('mg-level-options');
      var options = state.bus.map(function (bu) {
        return '<option value="' + esc(bu.name) + '"></option>';
      }).join('');
      if (buList) { buList.innerHTML = options; }
      if (levelList) {
        levelList.innerHTML = state.levels.map(function (level) {
          return '<option value="' + esc(level) + '"></option>';
        }).join('');
      }
      if (buFilter) {
        var previous = buFilter.value;
        buFilter.innerHTML = '<option value="">All business units</option>' +
          state.bus.map(function (bu) {
            return '<option value="' + esc(bu.name) + '">' + esc(bu.name) + ' (' + esc(bu.role_count) + ')</option>';
          }).join('');
        buFilter.value = previous;
      }
    },

    renderList: function () {
      var tbody = byId('mg-tbody');
      if (!tbody) { return; }
      var q = ((byId('mg-search') || {}).value || '').toLowerCase();
      var bu = (byId('mg-bu-filter') || {}).value || '';

      var rows = Manage.rows.filter(function (role) {
        if (bu && role.bu !== bu) { return false; }
        if (q) {
          var haystack = (role.pos + ' ' + role.bu + ' ' + role.band + ' ' + role.level).toLowerCase();
          if (haystack.indexOf(q) === -1) { return false; }
        }
        return true;
      }).sort(function (a, b) {
        if (a.bu !== b.bu) { return a.bu.localeCompare(b.bu); }
        var diff = bandSortValue(a.band) - bandSortValue(b.band);
        return diff !== 0 ? diff : a.pos.localeCompare(b.pos);
      });

      var count = byId('mg-count');
      if (count) {
        count.textContent = rows.length + ' of ' + Manage.rows.length + ' role(s) shown';
      }

      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-light);padding:2rem">No roles match this filter.</td></tr>';
        return;
      }

      tbody.innerHTML = rows.map(function (role) {
        return '<tr' + (role.is_active ? '' : ' class="mg-inactive"') + '>' +
          '<td><strong>' + esc(role.pos) + '</strong>' +
          (role.is_active ? '' : ' <span style="font-size:10px;color:#C0392B">(hidden)</span>') + '</td>' +
          '<td>' + esc(role.bu) + '</td>' +
          '<td><span class="band-badge ' + bc(role.band) + '">' + esc(role.band) + '</span></td>' +
          '<td>' + esc(role.level) + '</td>' +
          '<td><div class="mg-row-actions">' +
          '<button class="mg-mini" type="button" data-edit-role="' + esc(role.id) + '">Edit</button>' +
          '<button class="mg-mini" type="button" data-open-role="' + esc(role.id) + '">View</button>' +
          '<button class="mg-mini danger" type="button" data-delete-role="' + esc(role.id) + '">Delete</button>' +
          '</div></td></tr>';
      }).join('');
    },

    banner: function (message, kind) {
      var el = byId('mg-banner');
      if (!el) { return; }
      el.className = 'mg-banner show ' + (kind || 'info');
      el.innerHTML = message;
      if (kind === 'ok') {
        window.setTimeout(function () { el.classList.remove('show'); }, 6000);
      }
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    clearErrors: function () {
      FORM_FIELDS.concat(['bu']).forEach(function (field) {
        var errEl = byId('e-' + field);
        var inputEl = byId('f-' + field);
        if (errEl) { errEl.textContent = ''; }
        if (inputEl) { inputEl.classList.remove('err'); }
      });
    },

    newRole: function () {
      Manage.clearErrors();
      byId('mg-editor-title').textContent = 'New role';
      byId('f-id').value = '';
      byId('f-bu').value = (byId('mg-bu-filter') || {}).value || '';
      FORM_FIELDS.forEach(function (field) {
        var el = byId('f-' + field);
        if (el) { el.value = ''; }
      });
      byId('f-is_active').checked = true;
      Manage.previewBand();
      Manage.openEditor();
    },

    edit: function (roleId) {
      var role = state.byId[roleId];
      if (!role) {
        Manage.banner('That role is no longer available. Refreshing the list&hellip;', 'bad');
        Manage.reload();
        return;
      }
      Manage.clearErrors();
      closeModal();
      showPage('manage');
      byId('mg-editor-title').textContent = 'Edit: ' + role.pos;
      byId('f-id').value = role.id;
      byId('f-bu').value = role.bu;
      byId('f-position').value = role.pos;
      byId('f-band').value = role.band;
      byId('f-level').value = role.level;
      byId('f-purpose').value = role.desc;
      byId('f-experience').value = role.exp;
      byId('f-qualifications').value = role.quals;
      byId('f-focus_areas').value = role.focus;
      byId('f-kras').value = role.kras;
      byId('f-direct_reports').value = role.reports;
      byId('f-technical_competencies').value = role.techcomp;
      byId('f-leadership_competencies').value = role.leadcomp;
      byId('f-is_active').checked = role.is_active;
      Manage.previewBand();
      Manage.openEditor();
    },

    openEditor: function () {
      byId('mg-list').style.display = 'none';
      byId('mg-editor').classList.add('show');
      byId('mg-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
      var first = byId('f-bu');
      if (first) { first.focus(); }
    },

    closeEditor: function () {
      byId('mg-editor').classList.remove('show');
      byId('mg-list').style.display = '';
      Manage.clearErrors();
      var note = byId('mg-save-note');
      if (note) { note.textContent = ''; }
    },

    previewBand: function () {
      var el = byId('f-band');
      var preview = byId('f-band-preview');
      if (!el || !preview) { return; }
      var value = parseFloat(el.value.replace(/[^0-9.]/g, ''));
      preview.textContent = isNaN(value)
        ? 'No number found in the label — this role will sort last.'
        : 'Sorts as ' + value + '.';
    },

    /** Client-side pre-validation. The server validates again; this is UX. */
    validate: function (payload) {
      var errors = {};
      if (!payload.business_unit_name) { errors.bu = 'Business unit is required.'; }
      if (!payload.position) { errors.position = 'Job title is required.'; }
      else if (payload.position.length < 2) { errors.position = 'Job title must be at least 2 characters.'; }
      if (payload.band && !/^[A-Za-z0-9 .\-/&()]{0,40}$/.test(payload.band)) {
        errors.band = 'Band may only contain letters, digits, spaces and . - / & ( ).';
      }
      FORM_FIELDS.forEach(function (field) {
        if (payload[field] && payload[field].length > 8000) {
          errors[field] = 'Must be at most 8000 characters.';
        }
      });
      return errors;
    },

    showErrors: function (errors) {
      Manage.clearErrors();
      var firstBad = null;
      Object.keys(errors).forEach(function (field) {
        var message = errors[field];
        var text = Array.isArray(message) ? message.join(' ') : String(message);
        var errEl = byId('e-' + field);
        var inputEl = byId('f-' + field);
        if (errEl) { errEl.textContent = text; }
        if (inputEl) {
          inputEl.classList.add('err');
          if (!firstBad) { firstBad = inputEl; }
        }
      });
      if (firstBad) { firstBad.focus(); firstBad.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    },

    submit: function (event) {
      event.preventDefault();
      var saveButton = byId('mg-save');
      var note = byId('mg-save-note');
      if (saveButton.disabled) { return false; }

      var id = byId('f-id').value;
      var payload = {
        business_unit_name: byId('f-bu').value.trim(),
        is_active: byId('f-is_active').checked
      };
      FORM_FIELDS.forEach(function (field) {
        payload[field] = byId('f-' + field).value.trim();
      });

      var errors = Manage.validate(payload);
      if (Object.keys(errors).length) {
        Manage.showErrors(errors);
        Manage.banner('Please correct the highlighted fields.', 'bad');
        return false;
      }

      Manage.clearErrors();
      saveButton.disabled = true;
      saveButton.innerHTML = '<span class="mg-spinner"></span>Saving&hellip;';
      if (note) { note.textContent = ''; }

      var request = id ? API.updateRole(id, payload) : API.createRole(payload);

      request.then(function (saved) {
        Manage.banner(
          (id ? 'Updated ' : 'Created ') + '<strong>' + esc(saved.position) + '</strong> in ' +
          esc(saved.bu) + '.', 'ok');
        Manage.closeEditor();
        return Manage.reload();
      }).catch(function (error) {
        if (error.status === 403 || error.status === 401) {
          Manage.banner(
            'Your session has expired. <a href="/accounts/login/?next=/?page=manage">Sign in again</a> to continue.',
            'bad');
        } else if (error.details && Object.keys(error.details).length) {
          var mapped = {};
          Object.keys(error.details).forEach(function (key) {
            mapped[key === 'business_unit' || key === 'business_unit_name' ? 'bu' : key] = error.details[key];
          });
          Manage.showErrors(mapped);
          Manage.banner(esc(error.message) + ' <code>' + esc(error.code) + '</code>', 'bad');
        } else {
          Manage.banner(
            esc(error.message) + ' <code>' + esc(error.code || 'UNKNOWN') + '</code>' +
            (error.requestId ? ' <span style="font-size:11px">request ' + esc(error.requestId) + '</span>' : ''),
            'bad');
        }
      }).then(function () {
        saveButton.disabled = false;
        saveButton.textContent = 'Save role';
      });

      return false;
    },

    askDelete: function (roleId) {
      var role = state.byId[roleId];
      if (!role) { return; }
      state.deleteTarget = roleId;
      var box = byId('mg-confirm');
      byId('mg-confirm-text').innerHTML =
        'Delete <strong>' + esc(role.pos) + '</strong> (' + esc(role.bu) + ')? ' +
        'This cannot be undone — to hide it instead, edit it and untick &ldquo;Visible on the public site&rdquo;.';
      box.classList.add('show');
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    cancelDelete: function () {
      state.deleteTarget = null;
      byId('mg-confirm').classList.remove('show');
    },

    confirmDelete: function () {
      var roleId = state.deleteTarget;
      if (!roleId) { return; }
      var role = state.byId[roleId];
      var button = byId('mg-confirm-yes');
      button.disabled = true;
      button.innerHTML = '<span class="mg-spinner"></span>Deleting&hellip;';

      API.deleteRole(roleId).then(function () {
        Manage.banner('Deleted <strong>' + esc(role ? role.pos : roleId) + '</strong>.', 'ok');
        Manage.cancelDelete();
        return Manage.reload();
      }).catch(function (error) {
        Manage.banner('Could not delete: ' + esc(error.message) +
          ' <code>' + esc(error.code || 'UNKNOWN') + '</code>', 'bad');
      }).then(function () {
        button.disabled = false;
        button.textContent = 'Yes, delete';
      });
    }
  };

  // ======================================================================
  // Event wiring
  //
  // Delegated listeners rather than inline onclick attributes carrying
  // interpolated data. Role titles and BU names contain apostrophes and
  // ampersands, which broke the legacy inline-handler approach and were a
  // script-injection route once users could type those fields.
  // ======================================================================

  document.addEventListener('click', function (event) {
    var target = event.target;

    var card = target.closest('.role-card[data-role-id]');
    if (card) { openModal(card.getAttribute('data-role-id')); return; }

    var slotOption = target.closest('.slot-opt[data-role-id]');
    if (slotOption) {
      pickSlot(slotOption.getAttribute('data-slot'), slotOption.getAttribute('data-role-id'));
      return;
    }

    var openRole = target.closest('[data-open-role]');
    if (openRole) { openModal(openRole.getAttribute('data-open-role')); return; }

    var editRole = target.closest('[data-edit-role]');
    if (editRole) { Manage.edit(editRole.getAttribute('data-edit-role')); return; }

    var deleteRole = target.closest('[data-delete-role]');
    if (deleteRole) { Manage.askDelete(deleteRole.getAttribute('data-delete-role')); return; }

    var targetRole = target.closest('[data-target-role]');
    if (targetRole) {
      var id = targetRole.getAttribute('data-target-role');
      closeModal();
      showPage('career');
      window.setTimeout(function () {
        var select = byId('cp-target');
        if (select) { select.value = id; }
      }, 100);
      return;
    }

    var compareRole = target.closest('[data-compare-role]');
    if (compareRole) {
      var compareId = compareRole.getAttribute('data-compare-role');
      closeModal();
      showPage('compare');
      window.setTimeout(function () { pickSlot('a', compareId); }, 100);
      return;
    }

    // Close the compare dropdowns when clicking outside them.
    if (!target.closest('.slot-search-wrap')) {
      document.querySelectorAll('.slot-dropdown').forEach(function (drop) {
        drop.classList.remove('open');
      });
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeModal();
      Manage.cancelDelete();
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    var confirmYes = byId('mg-confirm-yes');
    if (confirmYes) { confirmYes.addEventListener('click', Manage.confirmDelete); }
    init();
  });

  // ----------------------------------------------------------------------
  // Exports for the inline handlers that remain in the original markup
  // (onclick="showPage('browse')", oninput="filterRoles()", and so on).
  // ----------------------------------------------------------------------
  window.showPage = showPage;
  window.setBU = setBU;
  window.filterRoles = filterRoles;
  window.searchSlot = searchSlot;
  window.openDrop = openDrop;
  window.pickSlot = pickSlot;
  window.clearSlot = clearSlot;
  window.buildCareerPath = buildCareerPath;
  window.openModal = openModal;
  window.closeModal = closeModal;
  window.askAI = askAI;
  window.Manage = Manage;
})();
