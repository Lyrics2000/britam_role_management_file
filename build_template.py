#!/usr/bin/env python3
"""
One-shot build step: turn the legacy single-file page into a Django template.

Run from the repo root:  python build_template.py

It takes everything above the original inline <script> (doctype, head, all the
CSS, the entire body markup) verbatim, then:
  * injects the CSS the new Manage tab needs
  * adds the Manage nav tab + sign in / sign out control to the header
  * adds the Manage page markup and its bottom-nav button
  * drops the inline data + script and links roles/static/roles/app.js instead

Kept in the repo (rather than run once and deleted) so that if HR ever hands
over a refreshed design of the legacy HTML, regenerating the template is one
command instead of a manual merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SOURCE = REPO / "Britam_Role_Library.html"
TARGET = REPO / "roles" / "templates" / "roles" / "role_library.html"

EXTRA_CSS = """
/* ── Added for the Django build: auth control + Manage tab ───────────── */
.header-actions{display:flex;align-items:center;gap:10px;flex-shrink:0}
.auth-link{font-size:12px;color:rgba(255,255,255,0.85);text-decoration:none;border:1px solid rgba(255,255,255,0.3);border-radius:6px;padding:6px 12px;transition:all 0.2s;white-space:nowrap;background:none;cursor:pointer;font-family:'DM Sans',sans-serif}
.auth-link:hover{background:rgba(255,255,255,0.15);color:#fff}
.auth-user{font-size:11px;color:rgba(255,255,255,0.7);white-space:nowrap}
.mg-toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:1rem}
.mg-toolbar input{flex:1;min-width:200px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;font-family:'DM Sans',sans-serif;outline:none}
.mg-toolbar input:focus{border-color:var(--green)}
.mg-btn{padding:9px 18px;font-size:13px;font-weight:600;border-radius:8px;cursor:pointer;font-family:'DM Sans',sans-serif;border:none;transition:opacity 0.2s}
.mg-btn:hover{opacity:0.86}
.mg-btn:disabled{opacity:0.45;cursor:not-allowed}
.mg-btn.primary{background:var(--green);color:#fff}
.mg-btn.secondary{background:none;border:1px solid var(--green);color:var(--green)}
.mg-btn.danger{background:#C0392B;color:#fff}
.mg-btn.ghost{background:none;border:1px solid var(--border);color:var(--text-mid)}
.mg-table{width:100%;border-collapse:collapse;font-size:12px}
.mg-table th{background:var(--green);color:#fff;padding:9px 10px;text-align:left;font-weight:600;position:sticky;top:0;z-index:2}
.mg-table td{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
.mg-table tr:nth-child(even) td{background:var(--bg)}
.mg-table-wrap{max-height:520px;overflow:auto;border:1px solid var(--border);border-radius:var(--radius)}
.mg-row-actions{display:flex;gap:6px;flex-wrap:wrap}
.mg-mini{padding:4px 10px;font-size:11px;border-radius:6px;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--text-mid);font-family:'DM Sans',sans-serif}
.mg-mini:hover{border-color:var(--green);color:var(--green)}
.mg-mini.danger:hover{border-color:#C0392B;color:#C0392B}
.mg-inactive td{opacity:0.55}
.mg-form{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.mg-field{display:flex;flex-direction:column}
.mg-field.full{grid-column:1/-1}
.mg-field label{font-size:11px;font-weight:600;color:var(--text-mid);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
.mg-field .req{color:#C0392B;margin-left:3px}
.mg-field input,.mg-field select,.mg-field textarea{width:100%;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;background:var(--surface);color:var(--text);font-family:'DM Sans',sans-serif;outline:none;transition:border-color 0.2s}
.mg-field textarea{min-height:78px;resize:vertical;line-height:1.5}
.mg-field input:focus,.mg-field select:focus,.mg-field textarea:focus{border-color:var(--green)}
.mg-field input.err,.mg-field textarea.err,.mg-field select.err{border-color:#C0392B;background:#FDEDEC}
.mg-hint{font-size:11px;color:var(--text-light);margin-top:4px;line-height:1.4}
.mg-err{font-size:11px;color:#C0392B;margin-top:4px;font-weight:500}
.mg-banner{padding:11px 14px;border-radius:8px;font-size:13px;margin-bottom:1rem;display:none;line-height:1.5}
.mg-banner.show{display:block}
.mg-banner.ok{background:#D1F5E0;color:#1A6B3C;border:1px solid rgba(26,107,60,0.25)}
.mg-banner.bad{background:#FDEDEC;color:#7B241C;border:1px solid rgba(123,36,28,0.25)}
.mg-banner.info{background:var(--green-light);color:#1A3D2B;border:1px solid rgba(0,107,60,0.2)}
.mg-banner code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;background:rgba(0,0,0,0.06);padding:1px 5px;border-radius:4px}
.mg-form-actions{display:flex;gap:10px;margin-top:1.25rem;flex-wrap:wrap;align-items:center}
.mg-editor{display:none}
.mg-editor.show{display:block}
.mg-count{font-size:12px;color:var(--text-light)}
.mg-confirm{background:#FDEDEC;border:1px solid rgba(123,36,28,0.3);border-radius:8px;padding:10px 14px;margin-bottom:1rem;font-size:13px;color:#7B241C;display:none;align-items:center;gap:12px;flex-wrap:wrap}
.mg-confirm.show{display:flex}
.mg-spinner{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,0.4);border-top-color:#fff;border-radius:50%;animation:mgspin 0.7s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes mgspin{to{transform:rotate(360deg)}}
.load-state{text-align:center;padding:3rem;color:var(--text-light);font-size:13px}
@media(max-width:720px){
  .mg-form{grid-template-columns:1fr}
  .header-actions{width:100%;justify-content:flex-end}
  .mg-table-wrap{max-height:none}
}
"""

MANAGE_NAV_TAB = """      <button class="nav-tab" id="tab-manage" onclick="showPage('manage')" style="display:none">&#9998; Manage</button>
"""

HEADER_ACTIONS = """    <div class="header-actions">
      {% if request.user.is_authenticated %}
        <span class="auth-user">{{ request.user.get_username }}</span>
        <form method="post" action="{% url 'logout' %}" style="display:inline">{% csrf_token %}<button type="submit" class="auth-link">Sign out</button></form>
      {% else %}
        <a class="auth-link" href="{% url 'login' %}?next=/">Sign in to edit</a>
      {% endif %}
    </div>
"""

MANAGE_PAGE = """
  <!-- MANAGE (staff only; rendered but hidden for everyone else) -->
  <div id="page-manage" style="display:none">
    <div class="section-card">
      <div class="section-head">
        <div class="section-icon">&#9998;</div>
        <h2>Manage roles</h2>
      </div>

      <div class="mg-banner" id="mg-banner"></div>
      <div class="mg-confirm" id="mg-confirm">
        <span id="mg-confirm-text"></span>
        <span style="display:flex;gap:8px">
          <button class="mg-btn danger" id="mg-confirm-yes" type="button">Yes, delete</button>
          <button class="mg-btn ghost" type="button" onclick="Manage.cancelDelete()">Cancel</button>
        </span>
      </div>

      <!-- list view -->
      <div id="mg-list">
        <div class="mg-toolbar">
          <input type="text" id="mg-search" placeholder="Filter by title, BU, band&hellip;" oninput="Manage.renderList()">
          <select id="mg-bu-filter" class="mg-filter" onchange="Manage.renderList()" style="padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;font-family:'DM Sans',sans-serif;min-width:170px">
            <option value="">All business units</option>
          </select>
          <label class="mg-count" style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="mg-show-inactive" onchange="Manage.reload()" style="width:auto"> Show hidden
          </label>
          <button class="mg-btn primary" type="button" onclick="Manage.newRole()">+ New role</button>
        </div>
        <div class="mg-count" id="mg-count" style="margin-bottom:8px"></div>
        <div class="mg-table-wrap">
          <table class="mg-table">
            <thead>
              <tr><th>Position</th><th style="width:150px">BU / Function</th><th style="width:90px">Band</th><th style="width:140px">Level</th><th style="width:170px">Actions</th></tr>
            </thead>
            <tbody id="mg-tbody"></tbody>
          </table>
        </div>
      </div>

      <!-- create / edit form -->
      <div class="mg-editor" id="mg-editor">
        <h3 style="font-size:14px;font-weight:600;margin-bottom:1rem" id="mg-editor-title">New role</h3>
        <form id="mg-form" onsubmit="return Manage.submit(event)" novalidate>
          <input type="hidden" id="f-id">
          <div class="mg-form">
            <div class="mg-field">
              <label for="f-bu">BU / Function<span class="req">*</span></label>
              <input list="mg-bu-options" id="f-bu" maxlength="120" autocomplete="off" required>
              <datalist id="mg-bu-options"></datalist>
              <div class="mg-hint">Pick an existing unit or type a new name to create it.</div>
              <div class="mg-err" id="e-bu"></div>
            </div>
            <div class="mg-field">
              <label for="f-position">Job title<span class="req">*</span></label>
              <input type="text" id="f-position" maxlength="255" required>
              <div class="mg-hint">Must be unique within the business unit.</div>
              <div class="mg-err" id="e-position"></div>
            </div>
            <div class="mg-field">
              <label for="f-band">Job band</label>
              <input type="text" id="f-band" maxlength="40" placeholder="Band 6.2" oninput="Manage.previewBand()">
              <div class="mg-hint" id="f-band-preview">Sort value is read from the number in the label.</div>
              <div class="mg-err" id="e-band"></div>
            </div>
            <div class="mg-field">
              <label for="f-level">Leadership level</label>
              <input list="mg-level-options" id="f-level" maxlength="80" autocomplete="off">
              <datalist id="mg-level-options"></datalist>
              <div class="mg-err" id="e-level"></div>
            </div>
            <div class="mg-field full">
              <label for="f-purpose">Role purpose</label>
              <textarea id="f-purpose" maxlength="8000"></textarea>
              <div class="mg-err" id="e-purpose"></div>
            </div>
            <div class="mg-field">
              <label for="f-experience">Experience required</label>
              <textarea id="f-experience" maxlength="8000"></textarea>
              <div class="mg-err" id="e-experience"></div>
            </div>
            <div class="mg-field">
              <label for="f-qualifications">Qualifications</label>
              <textarea id="f-qualifications" maxlength="8000"></textarea>
              <div class="mg-err" id="e-qualifications"></div>
            </div>
            <div class="mg-field">
              <label for="f-focus_areas">Key focus areas</label>
              <textarea id="f-focus_areas" maxlength="8000"></textarea>
              <div class="mg-hint">Separate with commas &mdash; each becomes a tag on the role card.</div>
              <div class="mg-err" id="e-focus_areas"></div>
            </div>
            <div class="mg-field">
              <label for="f-kras">Key performance measures</label>
              <textarea id="f-kras" maxlength="8000"></textarea>
              <div class="mg-err" id="e-kras"></div>
            </div>
            <div class="mg-field">
              <label for="f-direct_reports">Direct reports</label>
              <textarea id="f-direct_reports" maxlength="8000"></textarea>
              <div class="mg-err" id="e-direct_reports"></div>
            </div>
            <div class="mg-field">
              <label for="f-technical_competencies">Technical / functional competencies</label>
              <textarea id="f-technical_competencies" maxlength="8000"></textarea>
              <div class="mg-hint">Separate individual competencies with a pipe ( | ).</div>
              <div class="mg-err" id="e-technical_competencies"></div>
            </div>
            <div class="mg-field full">
              <label for="f-leadership_competencies">Leadership competencies</label>
              <textarea id="f-leadership_competencies" maxlength="8000"></textarea>
              <div class="mg-hint">Separate individual competencies with a pipe ( | ).</div>
              <div class="mg-err" id="e-leadership_competencies"></div>
            </div>
            <div class="mg-field full">
              <label style="display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:13px;font-weight:500;color:var(--text)">
                <input type="checkbox" id="f-is_active" checked style="width:auto"> Visible on the public site
              </label>
            </div>
          </div>
          <div class="mg-form-actions">
            <button class="mg-btn primary" type="submit" id="mg-save">Save role</button>
            <button class="mg-btn ghost" type="button" onclick="Manage.closeEditor()">Cancel</button>
            <span class="mg-count" id="mg-save-note"></span>
          </div>
        </form>
      </div>
    </div>
  </div>
"""

MANAGE_BOTTOM_NAV = """    <button class="bottom-nav-btn" id="bnav-manage" onclick="showPage('manage')" style="display:none">
      <div class="bnav-icon">&#9998;</div>
      <span class="bnav-label">Manage</span>
    </button>
"""

FOOTER_SCRIPTS = """
{{ app_config|json_script:"app-config" }}
<script src="{% static 'roles/app.js' %}"></script>
</body>
</html>
"""


def fail(message: str) -> None:
    print(f"build_template: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(haystack: str, needle: str, replacement: str, label: str) -> str:
    count = haystack.count(needle)
    if count != 1:
        fail(f"anchor {label!r} matched {count} times, expected exactly 1")
    return haystack.replace(needle, replacement, 1)


def main() -> None:
    if not SOURCE.is_file():
        fail(f"source not found: {SOURCE}")

    html = SOURCE.read_text(encoding="utf-8")

    script_at = html.find("<script>")
    if script_at == -1:
        fail("no <script> block found in the source HTML")
    markup = html[:script_at].rstrip()

    # 1. extra CSS ---------------------------------------------------------
    markup = replace_once(markup, "\n</style>", EXTRA_CSS + "</style>", "</style>")

    # 2. Manage nav tab + auth control -------------------------------------
    ai_tab = """      <button class="nav-tab" onclick="showPage('ai')">&#129302; AI Assistant</button>\n    </nav>\n"""
    markup = replace_once(
        markup,
        ai_tab,
        """      <button class="nav-tab" onclick="showPage('ai')">&#129302; AI Assistant</button>\n"""
        + MANAGE_NAV_TAB
        + "    </nav>\n"
        + HEADER_ACTIONS,
        "nav-tabs",
    )

    # 3. Manage page, inserted just before </div> that closes .app-body -----
    app_body_close = "\n</div>\n\n<!-- MOBILE BOTTOM NAV -->"
    markup = replace_once(
        markup,
        app_body_close,
        "\n" + MANAGE_PAGE + "\n</div>\n\n<!-- MOBILE BOTTOM NAV -->",
        "app-body close",
    )

    # 4. bottom nav button --------------------------------------------------
    ai_bnav = """    <button class="bottom-nav-btn" id="bnav-ai" onclick="showPage('ai')">
      <div class="bnav-icon">&#129302;</div>
      <span class="bnav-label">AI Chat</span>
    </button>
"""
    markup = replace_once(markup, ai_bnav, ai_bnav + MANAGE_BOTTOM_NAV, "bottom nav")

    # 5. loading placeholder in the roles grid ------------------------------
    markup = replace_once(
        markup,
        '<div class="roles-grid" id="roles-grid">',
        '<div class="load-state" id="load-state">Loading the role library&hellip;</div>\n'
        '    <div class="roles-grid" id="roles-grid">',
        "roles grid",
    )

    header = (
        "{% load static %}\n"
        "{% comment %}\n"
        "  GENERATED FILE — do not hand-edit.\n"
        "  Source of truth: Britam_Role_Library.html (markup + CSS) and\n"
        "  roles/static/roles/app.js (behaviour). Regenerate with:\n"
        "      python build_template.py\n"
        "{% endcomment %}\n"
    )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(header + markup + FOOTER_SCRIPTS, encoding="utf-8")

    print(f"build_template: wrote {TARGET.relative_to(REPO)} ({TARGET.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
