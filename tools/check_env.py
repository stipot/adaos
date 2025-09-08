import re, shutil, subprocess, sys

def ver(cmd, pat=r"(\d+\.\d+\.\d+)"):
    try:
        out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()
        m = re.search(pat, out)
        return m.group(1) if m else out
    except Exception:
        return None

def has(cmd): return shutil.which(cmd) is not None

msgs = []
def info(s): msgs.append(s)
def warn(s): msgs.append("⚠ " + s)
def err(s):  msgs.append("❌ " + s)

py = ver("python --version")
node = ver("node --version")
npmv = ver("npm --version")
pnpmv = ver("pnpm --version")
ionicv = ver("ionic --version")

info(f"Python: {py or 'not found'} (need ≥3.10, recommended 3.11)")
info(f"Node:   {node or 'not found'} (LTS 20.x recommended)")
info(f"npm:    {npmv or 'not found'}")
info(f"pnpm:   {pnpmv or '—'} (optional)")
info(f"Ionic:  {ionicv or '—'} (optional)")

problems = []
if not has("python"): problems.append("Python")
if not has("node"): problems.append("Node.js")
if not has("npm"): problems.append("npm")

# строгая проверка версии Python (фикс строки с версией!)
try:
    out = subprocess.check_output(
        'python -c "import sys; print(str(sys.version_info[0])+str(sys.version_info[1]))"',
        shell=True, text=True
    ).strip()
    major, minor = map(int, out.split("."))
    if (major, minor) < (3, 10):
        err("обнаружен Python < 3.10. Совет: `py -3.11 -m venv .venv` и переустановить зависимости.")
        problems.append("PythonVersion")
except Exception as e:
    warn(f"не удалось определить минорную версию Python автоматически: {e}")

print("\n".join(msgs))
if problems:
    print("\nПодсказка: установи/выбери нужные версии (на Windows удобно: `py -3.11`).")
    sys.exit(1)  # <-- ВАЖНО: при проблемах выходим с ненулевым кодом
