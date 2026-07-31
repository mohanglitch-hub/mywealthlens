"""Writes base.html directly to templates folder."""
import os

TMPL = r"C:\Users\mohan\Documents\mywealthlens\templates"

content = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{% block title %}MyWealthLens{% endblock %}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Mono:wght@400;600&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root, [data-theme="light"] {
      --bg:        #F8FAFC;
      --surface:   #FFFFFF;
      --surface2:  #F1F5F9;
      --border:    #E2E8F0;
      --border2:   #CBD5E1;
      --text:      #0F172A;
      --text2:     #334155;
      --muted:     #64748B;
      --muted2:    #94A3B8;
      --accent:    #0F766E;
      --accent-bg: #F0FDF9;
      --accent2:   #0284C7;
      --up:        #16A34A;
      --up-bg:     #F0FDF4;
      --down:      #DC2626;
      --down-bg:   #FEF2F2;
      --warning:   #D97706;
      --warning-bg:#FFFBEB;
      --font-ui:   'Inter', sans-serif;
      --font-data: 'Roboto Mono', monospace;
      --radius:    12px;
      --shadow-sm: 0 1px 3px rgba(15,23,42,0.08), 0 1px 2px rgba(15,23,42,0.04);
      --shadow-md: 0 4px 16px rgba(15,23,42,0.08), 0 2px 6px rgba(15,23,42,0.04);
      --shadow-lg: 0 8px 40px rgba(15,23,42,0.10), 0 4px 12px rgba(15,23,42,0.05);
    }

    [data-theme="dark"] {
      --bg:        #0d0f14;
      --surface:   #13161f;
      --surface2:  #1a1e2d;
      --border:    #1e2130;
      --border2:   #2a2f45;
      --text:      #e2e8f0;
      --text2:     #cbd5e1;
      --muted:     #64748b;
      --muted2:    #475569;
      --accent:    #00d4aa;
      --accent-bg: #00d4aa11;
      --accent2:   #0ea5e9;
      --up:        #22c55e;
      --up-bg:     #22c55e11;
      --down:      #ef4444;
      --down-bg:   #ef444411;
      --warning:   #f59e0b;
      --warning-bg:#f59e0b11;
      --shadow-sm: 0 1px 3px rgba(0,0,0,0.3);
      --shadow-md: 0 4px 16px rgba(0,0,0,0.3);
      --shadow-lg: 0 8px 40px rgba(0,0,0,0.4);
    }

    html, body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-ui);
      min-height: 100vh;
      transition: background 0.25s, color 0.25s;
    }

    .navbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 32px;
      height: 60px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      position: relative;
      box-shadow: var(--shadow-sm);
    }

    .wordmark {
      font-family: var(--font-data);
      font-size: 1rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-decoration: none;
    }
    .wordmark span.accent { color: var(--accent); }
    .wordmark span.dim    { color: var(--muted2); font-weight: 400; }

    .nav-links {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .nav-links a {
      color: var(--muted);
      text-decoration: none;
      font-size: 0.82rem;
      font-weight: 500;
      padding: 6px 8px;
      border-radius: 6px;
      transition: color 0.2s, background 0.2s;
    }
    .nav-links a:hover { color: var(--accent); background: var(--accent-bg); }
    .nav-links .btn-logout {
      background: var(--surface2);
      border: 1px solid var(--border);
      color: var(--text2);
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      font-family: var(--font-ui);
      text-decoration: none;
      transition: all 0.2s;
      margin-left: 6px;
    }
    .nav-links .btn-logout:hover { border-color: var(--down); color: var(--down); background: var(--down-bg); }

    .hamburger {
      display: none;
      flex-direction: column;
      justify-content: center;
      gap: 5px;
      background: none;
      border: none;
      cursor: pointer;
      padding: 4px;
      z-index: 101;
    }
    .hamburger span {
      display: block;
      width: 22px;
      height: 2px;
      background: var(--text);
      border-radius: 2px;
      transition: all 0.25s;
    }
    .hamburger.open span:nth-child(1) { transform: translateY(7px) rotate(45deg); }
    .hamburger.open span:nth-child(2) { opacity: 0; }
    .hamburger.open span:nth-child(3) { transform: translateY(-7px) rotate(-45deg); }

    .mobile-nav {
      display: none;
      position: absolute;
      top: 100%;
      left: 0; right: 0;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 8px 0;
      z-index: 100;
      flex-direction: column;
      box-shadow: var(--shadow-md);
    }
    .mobile-nav.open { display: flex; }
    .mobile-nav a {
      color: var(--muted);
      text-decoration: none;
      font-size: 0.9rem;
      font-weight: 500;
      padding: 12px 24px;
      border-bottom: 1px solid var(--border);
      transition: color 0.2s, background 0.2s;
    }
    .mobile-nav a:last-child { border-bottom: none; }
    .mobile-nav a:hover { color: var(--accent); background: var(--accent-bg); }
    .mobile-nav .btn-logout-mobile { color: var(--down) !important; font-weight: 600; }

    .flash-container {
      max-width: 440px;
      margin: 20px auto 0;
      padding: 0 20px;
    }
    .flash {
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 0.875rem;
      margin-bottom: 10px;
      border: 1px solid transparent;
    }
    .flash.success { background: var(--up-bg); border-color: var(--up); color: var(--up); }
    .flash.error   { background: var(--down-bg); border-color: var(--down); color: var(--down); }

    .page { padding: 40px 20px 80px; min-height: calc(100vh - 60px); }

    .auth-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 44px 40px;
      width: 100%;
      max-width: 440px;
      margin: 0 auto;
      box-shadow: var(--shadow-lg);
    }
    .auth-title    { font-size: 1.5rem; font-weight: 700; margin-bottom: 6px; color: var(--text); }
    .auth-subtitle { color: var(--muted); font-size: 0.875rem; margin-bottom: 28px; line-height: 1.5; }

    .form-group { margin-bottom: 18px; }
    .form-group label {
      display: block;
      font-size: 0.775rem;
      font-weight: 600;
      color: var(--muted);
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 6px;
    }
    .form-group input,
    .form-group select,
    .form-group textarea {
      width: 100%;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 11px 14px;
      color: var(--text);
      font-family: var(--font-ui);
      font-size: 0.9rem;
      outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    .form-group input:focus,
    .form-group select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-bg); }
    .form-group input::placeholder { color: var(--muted2); }

    .input-wrapper { position: relative; display: flex; align-items: center; }
    .input-wrapper input { padding-right: 60px; }
    .toggle-pw {
      position: absolute;
      right: 12px;
      background: none;
      border: none;
      color: var(--accent);
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      font-family: var(--font-ui);
      padding: 0;
    }
    .toggle-pw:hover { opacity: 0.75; }

    .btn-primary {
      width: 100%;
      background: var(--accent);
      color: #ffffff;
      border: none;
      border-radius: 8px;
      padding: 12px;
      font-size: 0.9rem;
      font-weight: 700;
      font-family: var(--font-ui);
      cursor: pointer;
      transition: opacity 0.2s, transform 0.1s;
      margin-top: 6px;
    }
    .btn-primary:hover  { opacity: 0.9; }
    .btn-primary:active { transform: scale(0.99); }

    .auth-footer { text-align: center; margin-top: 20px; font-size: 0.85rem; color: var(--muted); }
    .auth-footer a { color: var(--accent); text-decoration: none; font-weight: 600; }
    .auth-footer a:hover { text-decoration: underline; }

    @media (max-width: 768px) {
      .navbar     { padding: 0 16px; }
      .nav-links  { display: none; }
      .hamburger  { display: flex; }
      .auth-card  { padding: 32px 24px; border-radius: 12px; }
      .auth-title { font-size: 1.3rem; }
      .page       { padding: 24px 16px 60px; }
    }
    @media (min-width: 769px) {
      .mobile-nav { display: none !important; }
      .hamburger  { display: none !important; }
    }
  </style>
</head>

<body>
  {% if current_user.is_authenticated %}
  <nav class="navbar">
    <a class="wordmark" href="/dashboard">
      <span class="accent">MyWealth</span><span class="dim">Lens</span>
    </a>

    <div class="nav-links">
      <a href="/dashboard">Dashboard</a>
      <a href="/assets">Assets</a>
      <a href="/upload">Upload CAS</a>
      <a href="/goals">Goals</a>
      <a href="/life-stage">Life Stage</a>
      <a href="/loans">Loans</a>
      <a href="/insurance">Insurance</a>
      <a href="/emergency">Emergency</a>
      <a href="/tax">Tax</a>
      <a href="/family">Family</a>
      <a href="/account">Account</a>
      <a href="/settings">Settings</a>
      <a href="/logout" class="btn-logout">Log out</a>
    </div>

    <button class="hamburger" id="hamburger" onclick="toggleNav()" aria-label="Toggle navigation">
      <span></span><span></span><span></span>
    </button>

    <div class="mobile-nav" id="mobileNav">
      <a href="/dashboard">Dashboard</a>
      <a href="/assets">Assets</a>
      <a href="/upload">Upload CAS</a>
      <a href="/goals">Goals</a>
      <a href="/life-stage">Life Stage</a>
      <a href="/loans">Loans</a>
      <a href="/insurance">Insurance</a>
      <a href="/emergency">Emergency</a>
      <a href="/tax">Tax</a>
      <a href="/family">Family</a>
      <a href="/account">Account</a>
      <a href="/settings">Settings</a>
      <a href="/logout" class="btn-logout-mobile">Log out</a>
    </div>
  </nav>

  <script>
    function toggleNav() {
      document.getElementById('hamburger').classList.toggle('open');
      document.getElementById('mobileNav').classList.toggle('open');
    }
    document.querySelectorAll('.mobile-nav a').forEach(function(a) {
      a.addEventListener('click', function() {
        document.getElementById('hamburger').classList.remove('open');
        document.getElementById('mobileNav').classList.remove('open');
      });
    });
    (function() {
      var saved = localStorage.getItem('mwl-theme') || 'light';
      document.documentElement.setAttribute('data-theme', saved);
    })();
    function setTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      localStorage.setItem('mwl-theme', t);
    }
  </script>
  {% endif %}

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="flash-container">
        {% for category, message in messages %}
          <div class="flash {{ category }}">{{ message }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  <div class="page">
    {% block content %}{% endblock %}
  </div>

</body>
</html>
"""

dest = os.path.join(TMPL, "base.html")
with open(dest, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Done! base.html written to {dest}")