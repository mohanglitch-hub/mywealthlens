"""
Phase 2 — Fix: Write account.html directly to templates folder
"""
import os

BASE = r"C:\Users\mohan\Documents\mywealthlens"
dest = os.path.join(BASE, "templates", "account.html")

content = r"""{% extends "base.html" %}
{% block title %}Account — MyWealthLens{% endblock %}

{% block content %}
<div style="max-width:600px; margin:0 auto;">

  <h1 style="font-size:1.4rem; font-weight:700; margin-bottom:6px; color:var(--text);">Your Account</h1>
  <p style="color:var(--muted); font-size:0.875rem; margin-bottom:32px;">Your profile and account details</p>

  <!-- Profile card -->
  <div class="settings-card" style="margin-bottom:20px;">
    <div style="display:flex; align-items:center; gap:20px; margin-bottom:28px; flex-wrap:wrap;">
      <div class="avatar">
        <span>{{ user.name[0].upper() }}</span>
      </div>
      <div>
        <div style="font-size:1.1rem; font-weight:700; color:var(--text);">{{ user.name }}</div>
        <div style="font-size:0.85rem; color:var(--muted); margin-top:2px;">{{ user.email }}</div>
      </div>
    </div>
    <div class="profile-field">
      <div class="field-label">Full Name</div>
      <div class="field-value">{{ user.name }}</div>
    </div>
    <div class="profile-field">
      <div class="field-label">Email Address</div>
      <div class="field-value">{{ user.email }}</div>
    </div>
    <div class="profile-field" style="border-bottom:none; padding-bottom:0; margin-bottom:0;">
      <div class="field-label">Password</div>
      <div class="field-value" style="color:var(--muted2); letter-spacing:0.15em;">••••••••••••</div>
    </div>
  </div>

  <!-- Change Password -->
  <div class="settings-card" style="margin-bottom:20px;">
    <div style="font-weight:600; color:var(--text); margin-bottom:4px;">Change Password</div>
    <div style="color:var(--muted); font-size:0.875rem; margin-bottom:20px;">Choose a strong password of at least 8 characters</div>

    <form method="POST" action="/account/change-password" id="pwForm" novalidate>
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}" />

      <div class="form-group">
        <label for="current_password">Current Password</label>
        <div class="input-wrapper">
          <input type="password" id="current_password" name="current_password" placeholder="Enter current password" required />
          <button type="button" class="toggle-pw" onclick="togglePw('current_password',this)">Show</button>
        </div>
      </div>

      <div class="form-group">
        <label for="new_password">New Password</label>
        <div class="input-wrapper">
          <input type="password" id="new_password" name="new_password" placeholder="Minimum 8 characters" required minlength="8" oninput="checkStrength(this.value)" />
          <button type="button" class="toggle-pw" onclick="togglePw('new_password',this)">Show</button>
        </div>
        <div style="margin-top:8px;">
          <div style="height:4px; background:var(--border); border-radius:2px; overflow:hidden;">
            <div id="strengthBar" style="height:4px; width:0%; border-radius:2px; transition:width 0.3s, background 0.3s;"></div>
          </div>
          <div id="strengthLabel" style="font-size:0.72rem; color:var(--muted2); margin-top:4px; min-height:16px;"></div>
        </div>
        <div class="field-error" id="err-pw"></div>
      </div>

      <div class="form-group">
        <label for="confirm_password">Confirm New Password</label>
        <div class="input-wrapper">
          <input type="password" id="confirm_password" name="confirm_password" placeholder="Repeat new password" required minlength="8" />
          <button type="button" class="toggle-pw" onclick="togglePw('confirm_password',this)">Show</button>
        </div>
        <div class="field-error" id="err-confirm"></div>
      </div>

      <button type="submit" class="btn-primary" id="changeBtn" style="width:auto; padding:10px 28px;">Change Password</button>
    </form>
  </div>

  <!-- Stats -->
  <div class="settings-card">
    <div class="section-label">Account Summary</div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
      <div class="stat-box">
        <div class="stat-label">Member Since</div>
        <div class="stat-value">{{ user.created_at.strftime('%b %Y') if user.created_at else '—' }}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Account Status</div>
        <div class="stat-value" style="color:var(--up);">Active ✓</div>
      </div>
    </div>
  </div>

</div>

<style>
  .settings-card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:28px; box-shadow:var(--shadow-sm); }
  .avatar { width:64px; height:64px; border-radius:50%; background:var(--accent-bg); border:2px solid var(--accent); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  .avatar span { font-size:1.5rem; font-weight:700; color:var(--accent); }
  .profile-field { padding:16px 0; border-bottom:1px solid var(--border); }
  .field-label { font-size:0.72rem; font-weight:600; color:var(--muted); letter-spacing:0.08em; text-transform:uppercase; margin-bottom:5px; }
  .field-value { font-size:0.95rem; color:var(--text); font-weight:500; }
  .section-label { font-size:0.72rem; font-weight:600; color:var(--muted); letter-spacing:0.1em; text-transform:uppercase; margin-bottom:16px; }
  .stat-box { background:var(--surface2); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .stat-label { font-size:0.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px; }
  .stat-value { font-size:1rem; font-weight:600; color:var(--text); }
  .field-error { font-size:0.78rem; color:var(--down); margin-top:5px; min-height:16px; }
  @media (max-width:480px) { .settings-card { padding:20px; } }
</style>

<script>
  function togglePw(id, btn) {
    const inp = document.getElementById(id);
    const hidden = inp.type === 'password';
    inp.type = hidden ? 'text' : 'password';
    btn.textContent = hidden ? 'Hide' : 'Show';
  }
  function checkStrength(val) {
    const bar = document.getElementById('strengthBar');
    const label = document.getElementById('strengthLabel');
    if (!val) { bar.style.width='0%'; label.textContent=''; return; }
    let score = 0;
    if (val.length >= 8) score++;
    if (val.length >= 12) score++;
    if (/[A-Z]/.test(val)) score++;
    if (/[0-9]/.test(val)) score++;
    if (/[^A-Za-z0-9]/.test(val)) score++;
    const levels = [
      {pct:'20%',color:'#DC2626',text:'Very weak'},
      {pct:'40%',color:'#EA580C',text:'Weak'},
      {pct:'60%',color:'#D97706',text:'Fair'},
      {pct:'80%',color:'#65A30D',text:'Strong'},
      {pct:'100%',color:'#16A34A',text:'Very strong'},
    ];
    const lvl = levels[Math.min(score-1,4)] || levels[0];
    bar.style.width = lvl.pct;
    bar.style.background = lvl.color;
    label.style.color = lvl.color;
    label.textContent = lvl.text;
  }
  document.getElementById('pwForm').addEventListener('submit', function(e) {
    let valid = true;
    const pw  = document.getElementById('new_password').value;
    const cpw = document.getElementById('confirm_password').value;
    document.getElementById('err-pw').textContent = '';
    document.getElementById('err-confirm').textContent = '';
    if (pw.length < 8) { document.getElementById('err-pw').textContent = 'Password must be at least 8 characters.'; valid = false; }
    if (pw !== cpw) { document.getElementById('err-confirm').textContent = 'Passwords do not match.'; valid = false; }
    if (!valid) { e.preventDefault(); return; }
    const btn = document.getElementById('changeBtn');
    btn.textContent = 'Saving…';
    btn.disabled = true;
  });
</script>
{% endblock %}
"""

with open(dest, "w", encoding="utf-8") as f:
    f.write(content)
print(f"✓ account.html written directly to {dest}")
print("\nDone! Restart Flask: py app.py")