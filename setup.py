#!/usr/bin/env python3
"""Setup and start script for the Frappe development bench.

Drop this script into an empty directory and run it — it bootstraps
everything: clones Frappe, creates configs, installs deps, builds
frontend, creates a site, and starts all services.

For non-interactive site creation set the MariaDB root password:
    MARIADB_ROOT_PASSWORD=secret python setup.py

Usage:
  python setup.py            # Full setup (install + start)
  python setup.py install    # Install deps, build, create site
  python setup.py start      # Start all services
  python setup.py stop       # Stop all services
  python setup.py status     # Show service status
  python setup.py check      # Verify setup is ready
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS_DIR = ROOT / "apps"
ENV_DIR = ROOT / "env"
CONFIG_DIR = ROOT / "config"
PIDS_DIR = CONFIG_DIR / "pids"
SITES_DIR = ROOT / "sites"
LOGS_DIR = ROOT / "logs"

PYTHON = ENV_DIR / "bin" / "python"


# ── helpers ────────────────────────────────────────────────────────────────


def run(cmd, *, cwd=None, env=None, check=True, capture=False, input_text=None):
    """Run a command, printing it and streaming output."""
    cmd = [str(c) for c in cmd]
    print(f"\033[36m→ {' '.join(cmd)}\033[0m")
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    kwargs: dict = {"cwd": cwd or ROOT, "env": merged_env}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    if input_text is not None:
        kwargs["input"] = input_text
        kwargs["text"] = True
    return subprocess.run(cmd, check=check, **kwargs)


def which(name):
    return shutil.which(name) is not None


def pid_path(name):
    return PIDS_DIR / f"{name}.pid"


def read_pid(name):
    pp = pid_path(name)
    if pp.exists():
        return int(pp.read_text().strip())
    return None


def is_running(name):
    pid = read_pid(name)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def write_pid(name, pid):
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    pid_path(name).write_text(str(pid))


def remove_pid(name):
    pp = pid_path(name)
    if pp.exists():
        pp.unlink()


def env_with_venv():
    env = os.environ.copy()
    env["PATH"] = f"{ENV_DIR / 'bin'}{os.pathsep}{env['PATH']}"
    return env


def _open_log(path):
    """Open a log file for appending, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a")


def _is_frappe_app(path):
    """Check if a directory looks like a Frappe app by finding hooks.py or modules.txt."""
    # Standard nested: apps/alis/alis/hooks.py
    if ((path / path.name / "hooks.py").exists() or
        (path / path.name / "modules.txt").exists()):
        return True
    # Flat: apps/alis/hooks.py
    if ((path / "hooks.py").exists() or
        (path / "modules.txt").exists()):
        return True
    # Has pyproject.toml or setup.py (older style).
    if ((path / "pyproject.toml").exists() or
        (path / "setup.py").exists()):
        return True
    return False


def discover_apps():
    """Return sorted list of app names found in apps/.

    Detects standard (pyproject.toml), nested (alis/alis/hooks.py),
    and flat (alis/hooks.py) layouts. Skips well-known non-app dirs.
    """
    if not APPS_DIR.exists():
        return []
    skip = {"node_modules", ".git", "__pycache__", "dist", "public", "build"}
    apps = []
    for d in sorted(APPS_DIR.iterdir()):
        if not d.is_dir() or d.name in skip or d.name.startswith("."):
            continue
        if _is_frappe_app(d):
            apps.append(d.name)
    return apps


def _frontend_fresh():
    """True if frontend assets exist and are newer than source."""
    assets = SITES_DIR / "assets"
    if not assets.exists():
        return False
    # Check at least one built JS file exists.
    for dist_dir in assets.glob("*/dist"):
        if list(dist_dir.glob("**/*.js")):
            return True
    return False



def bootstrap_apps():
    """Clone Frappe if apps/ is empty or missing."""
    frappe_dir = APPS_DIR / "frappe"
    if frappe_dir.exists() and (frappe_dir / "pyproject.toml").exists():
        return  # already present

    APPS_DIR.mkdir(parents=True, exist_ok=True)
    print("Cloning Frappe (--depth 1) into apps/frappe …")
    run([
        "git", "clone", "--depth", "1",
        "https://github.com/frappe/frappe.git",
        str(frappe_dir),
    ])
    print("  Frappe cloned.\n")



# ── setup ──────────────────────────────────────────────────────────────────


def bootstrap_bench():
    """Ensure minimal bench directory structure exists.

    If the project directory is bare (no apps/, no config/, etc.),
    create the required directories and default config files.
    """
    SITES_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Default common_site_config.json
    site_config = SITES_DIR / "common_site_config.json"
    if not site_config.exists():
        site_config.write_text(json.dumps({
            "background_workers": 1,
            "default_site": "site1.localhost",
            "file_watcher_port": 6795,
            "frappe_user": os.environ.get("USER", "frappe"),
            "gunicorn_workers": 2,
            "live_reload": True,
            "rebase_on_pull": False,
            "redis_cache": "redis://127.0.0.1:13008",
            "redis_queue": "redis://127.0.0.1:11008",
            "redis_socketio": "redis://127.0.0.1:13008",
            "restart_supervisor_on_update": False,
            "restart_systemd_on_update": False,
            "serve_default_site": True,
            "shallow_clone": True,
            "socketio_port": 9008,
            "use_redis_auth": False,
            "webserver_port": 8008,
        }, indent=1) + "\n")
        print(f"Created {site_config}")

    # Default Redis configs
    for name, port, acl in [
        ("redis_cache.conf", 13008, "redis_cache.acl"),
        ("redis_queue.conf", 11008, "redis_queue.acl"),
    ]:
        conf_path = CONFIG_DIR / name
        acl_path = CONFIG_DIR / acl
        if not conf_path.exists():
            conf_path.write_text(
                f"port {port}\n"
                "bind 127.0.0.1\n"
                "daemonize no\n"
                "loglevel notice\n"
                "databases 1\n"
                "save \"\"\n"
            )
            print(f"Created {conf_path}")
        if not acl_path.exists():
            acl_path.write_text("")
            print(f"Created {acl_path}")



def ensure_uv():
    """Install uv if it's not on PATH."""
    if which("uv"):
        return
    print("uv not found — installing…")

    # Try pip with --break-system-packages (pip >= 23).
    subprocess.run(
        ["python3", "-m", "pip", "install", "uv", "--break-system-packages"],
        check=False, capture_output=True,
    )
    if which("uv"):
        print("  uv installed via pip.\n")
        return

    # Try pip without the flag (older pip, or inside venv).
    subprocess.run(
        ["python3", "-m", "pip", "install", "uv"],
        check=False, capture_output=True,
    )
    if which("uv"):
        print("  uv installed via pip.\n")
        return

    # Fallback: standalone installer (curl | sh).
    if which("curl"):
        subprocess.run(
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            shell=True, check=False,
        )
        for extra in [Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"]:
            if (extra / "uv").exists():
                os.environ["PATH"] = f"{extra}{os.pathsep}{os.environ['PATH']}"
                break
        if which("uv"):
            print("  uv installed via curl | sh.\n")
            return

    print("\033[31mCould not install uv. Install manually: https://docs.astral.sh/uv/\033[0m")
    sys.exit(1)


def ensure_venv():
    """Create the virtual env if it doesn't exist."""
    if PYTHON.exists():
        return
    print("Creating virtual environment at env/ …")
    run(["uv", "venv", "env", "--python", "3.14"])
    run(["uv", "pip", "install", "pip", "--python", str(PYTHON)])
    print("  Virtual environment ready.\n")

def _detect_distro():
    """Return ('arch'|'ubuntu'|'redhat'|'unknown') from /etc/os-release."""
    try:
        release = (Path("/etc/os-release").read_text()
                   .replace('"', '').replace("'", ""))
    except Exception:
        return "unknown"
    info = {}
    for line in release.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip()
    _id = info.get("ID", "").lower()
    id_like = info.get("ID_LIKE", "").lower()
    combined = f"{_id} {id_like}"
    if "arch" in combined:
        return "arch"
    if "ubuntu" in combined or "debian" in combined:
        return "ubuntu"
    if "rhel" in combined or "fedora" in combined or "centos" in combined:
        return "redhat"
    if _id in ("ubuntu", "debian"):
        return "ubuntu"
    if _id in ("fedora", "rhel", "centos", "almalinux", "rocky"):
        return "redhat"
    return "unknown"


_DISTRO = _detect_distro()


def _find_bin(name):
    """Like which() but also checks /usr/sbin and /sbin."""
    if which(name):
        return True
    for extra in ["/usr/sbin", "/sbin", "/usr/local/sbin"]:
        if (Path(extra) / name).exists():
            return True
    return False


def _try_install(pkg, cmd_name=None):
    """Try to install a package using the distro's package manager.

    Returns True if the command is now available.
    """
    target = cmd_name or pkg
    if _find_bin(target):
        return True

    pm_map = {
        "arch":   ("pacman", ["sudo", "pacman", "-S", "--noconfirm", pkg]),
        "ubuntu": ("apt-get", ["sudo", "apt-get", "install", "-y", pkg]),
        "redhat": ("dnf",    ["sudo", "dnf", "install", "-y", pkg]),
    }
    pm_bin, cmd = pm_map.get(_DISTRO, (None, None))
    if not pm_bin or not shutil.which(pm_bin):
        return False

    print(f"  Installing {target} ({' '.join(cmd)})…")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Try without sudo (already root, or passwordless).
        cmd2 = cmd[1:] if cmd[0] == "sudo" else cmd
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            print(f"    \033[33minstall failed: {r2.stderr.strip().splitlines()[-1] if r2.stderr else 'unknown error'}\033[0m")
            return False
    return _find_bin(target)

def _install_yarn():
    """Install yarn via npm or corepack."""
    if which("yarn"):
        return True
    if which("npm"):
        subprocess.run(["npm", "install", "-g", "yarn"], capture_output=True)
        if which("yarn"):
            return True
    if which("corepack"):
        subprocess.run(["corepack", "enable"], capture_output=True)
        if which("yarn"):
            return True
    return False


def check_prerequisites():
    print(f"Checking prerequisites…  (detected: {_DISTRO})")
    issues = []

    if not _find_bin("git"):
        if not _try_install("git"):
            issues.append("git not found. Install git.")

    if not _find_bin("redis-server"):
        pkg = "redis" if _DISTRO in ("arch", "redhat") else "redis-server"
        if not _try_install(pkg, "redis-server"):
            # RHEL needs EPEL for redis.
            if _DISTRO == "redhat":
                print("  Enabling EPEL for redis…")
                subprocess.run(
                    ["sudo", "dnf", "install", "-y",
                     "https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm"],
                    capture_output=True,
                )
                if _try_install("redis", "redis-server"):
                    pass  # installed via EPEL
            if not _find_bin("redis-server"):
                issues.append("redis-server not found. Install redis.")

    # Check node version is >= 24.
    node_ok = False
    if _find_bin("node"):
        r = subprocess.run(["node", "--version"], capture_output=True, text=True)
        ver = r.stdout.strip().lstrip("v")
        try:
            major = int(ver.split(".")[0])
            if major >= 24:
                node_ok = True
            else:
                print(f"  Node {ver} found, but >= 24 required — upgrading…")
        except (ValueError, IndexError):
            pass

    if not node_ok:
        print("  Setting up Node.js 24 via NodeSource…")
        if _DISTRO == "ubuntu":
            subprocess.run(
                "curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -",
                shell=True, capture_output=True,
            )
        elif _DISTRO in ("arch", "redhat"):
            subprocess.run(
                "curl -fsSL https://rpm.nodesource.com/setup_24.x | sudo bash -",
                shell=True, capture_output=True,
            )
        if not _try_install("nodejs", "node"):
            issues.append("node >= 24 not found. Install Node.js >= 24.")

        if not _install_yarn():
            issues.append("yarn not found. Install with: npm install -g yarn")

    if not which("bench"):
        issues.append(
            "bench CLI not found. Install with: pipx install frappe-bench"
        )

    if issues:
        print(f"\n\033[31mPrerequisites missing ({_DISTRO}):\033[0m")
        # Print distro-specific tips.
        tips = {
            "arch":   "sudo pacman -S git redis nodejs npm && npm install -g yarn",
            "ubuntu": "sudo apt install git redis-server && curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - && sudo apt install -y nodejs && npm install -g yarn",
            "redhat": "sudo dnf install git redis && curl -fsSL https://rpm.nodesource.com/setup_24.x | sudo bash - && sudo dnf install -y nodejs && npm install -g yarn",
        }
        tip = tips.get(_DISTRO, "")
        if tip:
            print(f"  Quick install: {tip}")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("  All prerequisites met.\n")


def write_apps_txt():
    """Write sites/apps.txt so bench CLI knows which apps are installed."""
    apps = discover_apps()
    apps_txt = SITES_DIR / "apps.txt"
    apps_txt.write_text("\n".join(apps) + "\n")
    print(f"Wrote {apps_txt}: {apps}")


def install_python_deps():
    print("Installing Python dependencies…")
    for app_dir in sorted(APPS_DIR.iterdir()):
        if not app_dir.is_dir():
            continue
        pyproject = app_dir / "pyproject.toml"
        if not pyproject.exists():
            continue
        print(f"  Installing {app_dir.name}…")
        run([
            "uv", "pip", "install", "-e", str(app_dir),
            "--python", str(PYTHON),
        ])
    print()


def install_node_deps():
    print("Installing Node.js dependencies…")
    for app_dir in sorted(APPS_DIR.iterdir()):
        if not app_dir.is_dir():
            continue
        pkg_json = app_dir / "package.json"
        if not pkg_json.exists():
            continue
        if (app_dir / "yarn.lock").exists():
            print(f"  yarn install --frozen-lockfile for {app_dir.name}…")
            run(["yarn", "install", "--frozen-lockfile"], cwd=app_dir)
        else:
            print(f"  yarn install for {app_dir.name} (no lockfile)…")
            run(["yarn", "install"], cwd=app_dir)
    print()


def build_frontend():
    """Build frontend assets via 'bench build'.

    bench build creates the symlinks in sites/assets/ that esbuild's
    postcss plugin needs to resolve cross-app CSS imports.  Raw
    'yarn production' fails without these symlinks.
    """
    print("Building frontend assets (bench build)…")
    run(["bench", "build"], cwd=ROOT, env=env_with_venv())
    print()


def create_site():
    """Create or repair a Frappe site.

    Requires MariaDB to be running.  Set MARIADB_ROOT_PASSWORD in the
    environment for non-interactive use:

        MARIADB_ROOT_PASSWORD=secret python setup.py
    """
    site_name = os.environ.get("FRAFFE_SITE", "site1.localhost")
    existing = list(SITES_DIR.glob("*/site_config.json"))

    # Check if existing site has a working DB connection.
    if existing:
        site_name = existing[0].parent.name
        result = subprocess.run(
            [str(PYTHON), "-c",
             f"import frappe; frappe.init(site='{site_name}');"
             "frappe.connect(); print('ok')"],
            cwd=ROOT, env=env_with_venv(),
            capture_output=True, text=True,
        )
        if result.returncode == 0 and "ok" in result.stdout:
            print(f"Site ready: {site_name}")
            return site_name
        print(f"Site exists but DB is unreachable — recreating…")

    # Build the command.
    cmd = ["bench", "new-site", site_name, "--no-mariadb-socket", "--force",
           "--admin-password", "admin"]
    db_root_pw = os.environ.get("MARIADB_ROOT_PASSWORD", "")
    if db_root_pw:
        cmd += ["--db-root-password", db_root_pw]

    if not sys.stdin.isatty() and not db_root_pw:
        print(
            "\033[33mCannot create site: stdin is not a TTY and"
            " MARIADB_ROOT_PASSWORD is not set.\033[0m\n"
            f"  Run interactively: bench new-site {site_name} --no-mariadb-socket\n"
            "  Or: MARIADB_ROOT_PASSWORD=secret python setup.py"
        )
        return None

    print(f"Creating site: {site_name}")
    try:
        run(cmd, cwd=ROOT, env=env_with_venv())
        print(f"  Site {site_name} ready.\n")
        return site_name
    except subprocess.CalledProcessError:
        print(
            "\033[33mSite creation failed — is MariaDB running?\033[0m\n"
            f"  Run: bench new-site {site_name} --no-mariadb-socket"
        )
        return None

def apply_patches():
    """Apply bench-level patches listed in patches.txt.

    These typically require a running site + MariaDB.  If they fail the
    script continues; patches can be applied later via 'bench migrate'.
    """
    patches_file = ROOT / "patches.txt"
    if not patches_file.exists():
        return
    patches = [
        line.strip()
        for line in patches_file.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not patches:
        return
    print("Applying patches…")
    failed = []
    for patch in patches:
        print(f"  {patch} … ", end="", flush=True)
        try:
            result = run(
                ["bench", "console"],
                input_text=f"{patch}\n",
                cwd=ROOT,
                env=env_with_venv(),
                capture=True,
                check=False,
            )
            if result.returncode == 0:
                print("\033[32mOK\033[0m")
            else:
                print(f"\033[33mskipped (exit {result.returncode})\033[0m")
                failed.append(patch)
        except Exception:
            print("\033[33mskipped (error)\033[0m")
            failed.append(patch)
    if failed:
        print(
            f"\n\033[33m{len(failed)} patch(es) skipped.\033[0m"
            "  Apply manually with: bench migrate"
        )
    print()
# ── service management ─────────────────────────────────────────────────────


def _port_in_use(port):
    """Check if a TCP port is already bound."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0



def _ensure_pid_dir():
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def start_redis(detach=True):
    for name, conf, port in [
        ("redis_cache", "redis_cache.conf", 13008),
        ("redis_queue", "redis_queue.conf", 11008),
    ]:
        if is_running(name):
            print(f"  {name} already running (pid {read_pid(name)})")
            continue
        if _port_in_use(port):
            print(f"  Port {port} in use — attempting to free it…")
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True, check=False,
            )
            time.sleep(0.5)
        print(f"  Starting {name}…")
        kwargs = {}
        if detach:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            ["redis-server", str(CONFIG_DIR / conf)],
            stdout=_open_log(LOGS_DIR / f"{name}.log"),
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        write_pid(name, proc.pid)
        print(f"    pid {proc.pid}")


def start_web(detach=True):
    name = "web"
    if is_running(name):
        print(f"  {name} already running (pid {read_pid(name)})")
        return
    print(f"  Starting {name} (bench serve --port 8008)…")
    kwargs = {}
    if detach:
        kwargs["start_new_session"] = True
        kwargs["stdout"] = _open_log(LOGS_DIR / f"{name}.log")
        kwargs["stderr"] = subprocess.STDOUT
    proc = subprocess.Popen(
        ["bench", "serve", "--port", "8008"],
        cwd=ROOT,
        env=env_with_venv(),
        **kwargs,
    )
    write_pid(name, proc.pid)
    if detach:
        print(f"    pid {proc.pid}")
    else:
        print(f"    pid {proc.pid} — running in foreground. Ctrl+C to stop.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            print("\n  Shutting down…")
            proc.terminate()
            proc.wait()
        # When web dies, kill the rest.
        cmd_stop(None)


def start_socketio(detach=True):
    name = "socketio"
    if is_running(name):
        print(f"  {name} already running (pid {read_pid(name)})")
        return
    node = shutil.which("node") or "node"
    print("  Starting socketio…")
    kwargs = {"start_new_session": True} if detach else {}
    proc = subprocess.Popen(
        [node, str(APPS_DIR / "frappe" / "socketio.js")],
        cwd=ROOT,
        env=env_with_venv(),
        stdout=_open_log(LOGS_DIR / f"{name}.log"),
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    write_pid(name, proc.pid)
    print(f"    pid {proc.pid}")


def start_worker(detach=True):
    name = "worker"
    if is_running(name):
        print(f"  {name} already running (pid {read_pid(name)})")
        return
    print("  Starting worker…")
    kwargs = {"start_new_session": True} if detach else {}
    proc = subprocess.Popen(
        ["bench", "worker"],
        cwd=ROOT,
        env=env_with_venv(),
        stdout=_open_log(LOGS_DIR / "worker.log"),
        stderr=_open_log(LOGS_DIR / "worker.error.log"),
        **kwargs,
    )
    write_pid(name, proc.pid)
    print(f"    pid {proc.pid}")


def start_schedule(detach=True):
    name = "schedule"
    if is_running(name):
        print(f"  {name} already running (pid {read_pid(name)})")
        return
    print("  Starting scheduler…")
    kwargs = {"start_new_session": True} if detach else {}
    proc = subprocess.Popen(
        ["bench", "schedule"],
        cwd=ROOT,
        env=env_with_venv(),
        stdout=_open_log(LOGS_DIR / f"{name}.log"),
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    write_pid(name, proc.pid)
    print(f"    pid {proc.pid}")


def stop_service(name):
    pid = read_pid(name)
    if pid is None:
        print(f"  {name}: not running")
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        print(f"  {name}: stopped (pid {pid})")
    except ProcessLookupError:
        print(f"  {name}: already dead (stale pid {pid})")
    remove_pid(name)


# ── commands ───────────────────────────────────────────────────────────────


SERVICES = ["schedule", "worker", "socketio", "web", "redis_cache", "redis_queue"]

def ensure_bench_dirs():
    """Create directories that bench requires to recognize the project."""
    for d in [LOGS_DIR, PIDS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def cmd_install(_args):
    """Install everything: venv, deps, build, site, patches."""
    # Discover apps first — show what we're working with.
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    apps = discover_apps()
    if not apps:
        print("\033[33mWARNING: No apps found in apps/ — will clone Frappe.\033[0m\n")
    else:
        print(f"\033[1mUsing apps from apps/: \033[0m{', '.join(apps)} "
              f"({len(apps)} app(s))\n")

    ensure_uv()
    bootstrap_bench()
    bootstrap_apps()
    ensure_venv()
    ensure_bench_dirs()
    check_prerequisites()

    # Refresh after possible clone.
    apps = discover_apps()

    # Fast path: everything already installed.
    if verify_setup(silent=True) and _frontend_fresh():
        print("Everything is already set up.\n")
        return


    write_apps_txt()
    install_python_deps()
    install_node_deps()
    build_frontend()

    # bench new-site needs Redis to be running.
    start_redis()
    time.sleep(1)
    site = create_site()
    # Stop Redis after site creation so start command starts fresh.
    stop_service("redis_cache")
    stop_service("redis_queue")
    if site:
        run(["bench", "use", site], cwd=ROOT, env=env_with_venv())
        extra = [a for a in apps if a != "frappe"]
        if extra:
            start_redis()
            time.sleep(1)
            for app in extra:
                print(f"  Installing app: {app}…")
                run(["bench", "--site", site, "install-app", app],
                    cwd=ROOT, env=env_with_venv())
            stop_service("redis_cache")
            stop_service("redis_queue")
    apply_patches()
    print("\n\033[32m✓ Installation complete.\033[0m")
    print(f"  Apps installed: {', '.join(apps)}")
    print("  Run: python setup.py start")

def verify_setup(silent=False):
    """Check the environment is ready. Returns True if all good."""
    ok = True

    def _ok(msg):
        if not silent:
            print(f"  \033[32m✓\033[0m {msg}")

    def _warn(msg):
        nonlocal ok
        ok = False
        print(f"  \033[33m✗\033[0m {msg}")

    if not PYTHON.exists():
        _warn(f"Virtual env missing ({ENV_DIR}). Run: python setup.py install")
    else:
        _ok(f"Virtual env: {ENV_DIR}")

    if not which("bench"):
        _warn("bench CLI not on PATH. Install: pipx install frappe-bench")
    else:
        _ok("bench CLI available")

    # Query the venv Python, not the script's own interpreter.
    result = subprocess.run(
        [str(PYTHON), "-c", "import frappe"],
        capture_output=True,
    )
    if result.returncode != 0:
        _warn("frappe not installed in venv. Run: python setup.py install")
    else:
        _ok("frappe package installed")

    apps_txt = SITES_DIR / "apps.txt"
    if not apps_txt.exists():
        _warn(f"{apps_txt} missing. Run: python setup.py install")
    else:
        _ok(f"{apps_txt} found")

    assets = SITES_DIR / "assets"
    if not assets.exists() or not list(assets.iterdir()):
        _warn("Frontend assets not built. Run: python setup.py install")
    else:
        _ok("Frontend assets built")

    sites = list(SITES_DIR.glob("*/site_config.json"))
    if not sites:
        _warn("No site found. Run interactively: bench new-site site1.localhost")
    else:
        _ok(f"Site: {sites[0].parent.name}")

    return ok


def cmd_start(args):
    """Start all services."""
    if not verify_setup():
        print("\n\033[33mSetup incomplete — run 'python setup.py install' first.\033[0m")
        sys.exit(1)

    detach = getattr(args, "detach", False)
    _ensure_pid_dir()

    print("\nStarting services…")
    start_redis(detach=detach)
    time.sleep(1)
    start_web(detach=detach)
    start_socketio(detach=detach)
    start_worker(detach=detach)
    start_schedule(detach=detach)

    if detach:
        print("\n\033[32m✓ All services started.\033[0m")
        print("  Web:       http://localhost:8008")
        print("  SocketIO:  port 9008")
        print("  Stop with: python setup.py stop")
    # In foreground mode, start_web blocks until Ctrl+C.



def cmd_stop(_args):
    """Stop all services."""
    print("Stopping services…")
    for name in SERVICES:
        stop_service(name)
    print("\n\033[32m✓ All services stopped.\033[0m")


def cmd_status(_args):
    """Show service status."""
    for name in SERVICES:
        pid = read_pid(name)
        if pid and is_running(name):
            print(f"  \033[32m●\033[0m {name}: running (pid {pid})")
        elif pid:
            print(f"  \033[31m✗\033[0m {name}: dead (stale pid {pid})")
        else:
            print(f"  \033[90m○\033[0m {name}: not running")


def cmd_check(_args):
    """Verify the environment is ready to start."""
    print("Checking setup…")
    if verify_setup():
        print("\n\033[32m✓ All checks passed — ready to start.\033[0m")
        print("  Run: python setup.py start")


def cmd_manual(_args):
    """Print manual setup instructions."""
    apps = discover_apps()
    site = os.environ.get("FRAFFE_SITE", "site1.localhost")

    print("Manual setup — run these commands in order:\n")
    print("# 1. Create virtual environment")
    print("  uv venv env --python 3.14")
    print("  uv pip install pip --python env/bin/python")
    print()
    print(f"# 2. Write apps.txt ({', '.join(apps)})")
    print(f"  echo '{apps[0]}' > sites/apps.txt")
    for app in apps[1:]:
        print(f"  echo '{app}' >> sites/apps.txt")
    print()
    print("# 3. Install Python deps for each app")
    for app in apps:
        print(f"  uv pip install -e apps/{app} --python env/bin/python")
    print()
    print("# 4. Install Node deps for each app")
    for app in apps:
        print(f"  (cd apps/{app} && yarn install --frozen-lockfile)")
    print()
    print("# 5. Build frontend")
    print("  bench build")
    print()
    print(f"# 6. Create site (needs MariaDB running)")
    print(f"  bench new-site {site} --no-mariadb-socket --admin-password admin")
    print(f"  bench use {site}")
    print()
    print("# 7. Apply patches")
    print("  bench migrate")
    print()
    print("# 8. Start Redis")
    print("  redis-server config/redis_cache.conf --daemonize yes")
    print("  redis-server config/redis_queue.conf --daemonize yes")
    print()
    print("# 9. Start services")
    print("  bench serve --port 8008 &")
    print("  node apps/frappe/socketio.js &")
    print("  bench worker &")
    print("  bench schedule &")
    print()
    print("# 10. Stop everything")
    print("  pkill -f 'bench serve'")
    print("  pkill -f 'socketio.js'")
    print("  pkill -f 'bench worker'")
    print("  pkill -f 'bench schedule'")
    print("  pkill -f 'redis-server.*config/'")
    print()
    print("Or just run: python setup.py")



def cmd_logs(args):
    """Tail service logs."""
    svc = getattr(args, "service", None)
    if svc:
        paths = [LOGS_DIR / f"{svc}.log"]
        if svc == "worker":
            paths.append(LOGS_DIR / "worker.error.log")
    else:
        paths = sorted(LOGS_DIR.glob("*.log"))
    if not paths:
        print("No log files found.")
        return
    for p in paths:
        if p.exists():
            print(f"\n\033[1m=== {p.name} ===\033[0m")
            run(["tail", "-n", "20", str(p)], check=False)


def cmd_restart(args):
    """Stop then start all services."""
    cmd_stop(None)
    time.sleep(0.5)
    cmd_start(args)

def cmd_shell(_args):
    """Open bench console for the default site."""
    if not verify_setup():
        print("\n\033[33mSetup incomplete — run 'python setup.py install' first.\033[0m")
        sys.exit(1)
    run(["bench", "console"], cwd=ROOT, env=env_with_venv())


def cmd_clean(_args):
    """Remove build artifacts, venv, logs, PIDs."""
    import shutil as _shutil
    dirs = ["env", "logs", "config/pids"]
    paths = [SITES_DIR / "assets"]
    for d in dirs:
        p = ROOT / d
        if p.exists():
            print(f"  Removing {d}/")
            _shutil.rmtree(p)
    for p in paths:
        if p.exists():
            print(f"  Removing {p}")
            _shutil.rmtree(p)
    for app_dir in APPS_DIR.iterdir():
        nm = app_dir / "node_modules"
        if nm.exists():
            print(f"  Removing {nm}")
            _shutil.rmtree(nm)
        dist = app_dir / "frappe" / "dist"
        if dist.exists():
            print(f"  Removing {dist}")
            _shutil.rmtree(dist)
    print("\n\033[32m✓ Cleaned. Run 'python setup.py' to rebuild.\033[0m")


def cmd_info(_args):
    """Show versions and paths."""
    print(f"  Project root : {ROOT}")
    print(f"  Apps         : {', '.join(discover_apps())}")
    print(f"  Python       : {PYTHON}  ({'exists' if PYTHON.exists() else 'missing'})")
    bench_path = shutil.which("bench") or "not found"
    print(f"  bench CLI    : {bench_path}")
    if bench_path != "not found":
        r = subprocess.run(["bench", "--version"], capture_output=True, text=True)
        print(f"  bench version: {r.stdout.strip()}")
    r = subprocess.run(["node", "--version"], capture_output=True, text=True)
    print(f"  Node.js      : {r.stdout.strip()}")
    r = subprocess.run(["yarn", "--version"], capture_output=True, text=True)
    print(f"  Yarn         : {r.stdout.strip()}")
    r = subprocess.run(["uv", "--version"], capture_output=True, text=True)
    print(f"  uv           : {r.stdout.strip()}")
    r = subprocess.run(["redis-server", "--version"], capture_output=True, text=True)
    print(f"  Redis        : {r.stdout.split()[2] if r.stdout else 'unknown'}")
    if PYTHON.exists():
        r = subprocess.run(
            [str(PYTHON), "-c", "import frappe; print(frappe.__version__)"],
            capture_output=True, text=True,
        )
        print(f"  Frappe       : {r.stdout.strip() if r.returncode == 0 else 'not installed'}")
    sites = list(SITES_DIR.glob("*/site_config.json"))
    print(f"  Sites        : {', '.join(s.parent.name for s in sites) if sites else 'none'}")


def cmd_migrate(_args):
    """Run bench migrate on the default site."""
    if not verify_setup():
        print("\n\033[33mSetup incomplete — run 'python setup.py install' first.\033[0m")
        sys.exit(1)
    run(["bench", "migrate"], cwd=ROOT, env=env_with_venv())


def cmd_setup(_args):
    """Full cycle: install + start."""
    cmd_install(None)
    cmd_start(None)


def main():
    parser = argparse.ArgumentParser(
        description="Frappe bench setup and start script.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="Install dependencies and create site")


    start_parser = sub.add_parser("start", help="Start all services (foreground)")
    start_parser.add_argument("-d", "--detach", action="store_true",
                              help="Run in background (survives terminal close)")

    sub.add_parser("stop", help="Stop all services")
    sub.add_parser("status", help="Show service status")
    sub.add_parser("check", help="Verify setup is ready")
    sub.add_parser("manual", help="Print manual setup commands")

    logs_parser = sub.add_parser("logs", help="Tail service logs")
    logs_parser.add_argument("service", nargs="?", help="Service name (web, worker, etc.)")

    restart_parser = sub.add_parser("restart", help="Stop then start (foreground)")
    restart_parser.add_argument("-d", "--detach", action="store_true",
                                help="Run in background (survives terminal close)")
    sub.add_parser("shell", help="Open bench console")
    sub.add_parser("clean", help="Remove build artifacts, venv, logs, PIDs")
    sub.add_parser("info", help="Show versions and paths")
    sub.add_parser("migrate", help="Run bench migrate")
    sub.add_parser("setup", help="Full setup: install + start")

    args = parser.parse_args()

    commands = {
        "install": cmd_install,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "check": cmd_check,
        "manual": cmd_manual,
        "logs": cmd_logs,
        "restart": cmd_restart,
        "shell": cmd_shell,
        "clean": cmd_clean,
        "info": cmd_info,
        "migrate": cmd_migrate,
        "setup": cmd_setup,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        cmd_setup(args)


if __name__ == "__main__":
    main()