import os, re, subprocess, sys, shutil


def get_ver(cmd, pat=r"(\d+\.\d+\.\d+)"):
    try:
        out = subprocess.check_output(cmd, shell=True, text=True).strip()
        m = re.search(pat, out)
        return m.group(1) if m else out
    except Exception:
        return None


def need(cmd):
    return shutil.which(cmd) is not None


problems = []
py = get_ver("python --version")
node = get_ver("node --version")
npmv = get_ver("npm --version")
pnpmv = get_ver("pnpm --version")  # optional for миграции
ionicv = get_ver("ionic --version")  # optional: может быть локально через npx
ngv = get_ver("ng version", r"Angular CLI:\s*([\d\.]+)")  # best effort

print(f"Python: {py or 'not found'} (need ≥3.10)")
print(f"Node:   {node or 'not found'} (LTS 20.x рекомендовано)")
print(f"npm:    {npmv or 'not found'}")
print(f"pnpm:   {pnpmv or '—'}")
print(f"Ionic:  {ionicv or '—'}")
print(f"Angular CLI: {ngv or '—'}")

if not need("python"):
    problems.append("Python")
if not need("node"):
    problems.append("Node.js")
if not need("npm"):
    problems.append("npm")
# ionic/ng не обязательны, можно запускать через npx

if problems:
    print("\n❌ Missing tools:", ", ".join(problems))
    sys.exit(1)
