import os, re

TEMPLATES = r"C:\Users\mohan\Documents\mywealthlens\templates"
TOKEN = '      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}" />\n'

for fname in os.listdir(TEMPLATES):
    if not fname.endswith(".html"):
        continue
    fpath = os.path.join(TEMPLATES, fname)
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    new_content = content
    pattern = re.compile(r'(<form\b[^>]*method=["\']POST["\'][^>]*>)', re.IGNORECASE)
    for m in pattern.finditer(content):
        form_tag = m.group(1)
        after = content[m.start():m.start()+300]
        if "csrf_token" not in after:
            new_content = new_content.replace(form_tag, form_tag + "\n" + TOKEN, 1)
    if new_content != content:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)
        print("Fixed: " + fname)

print("Done! Restart Flask: py app.py")