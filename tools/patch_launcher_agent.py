"""Prepare the launcher to use the Girinworld 26.2 client core as a Java agent."""
from pathlib import Path
import shutil
import sys

launcher = Path(sys.argv[1])
agent_source = Path(sys.argv[2])
assets = launcher / "assets"
assets.mkdir(exist_ok=True)
agent_target = assets / "girinworld-core-26.2.jar"
if agent_source.resolve() != agent_target.resolve():
    shutil.copy2(agent_source, agent_target)

meta = launcher / "app_meta.py"
text = meta.read_text(encoding="utf-8")
text = text.replace('APP_VERSION = "0.3.9"', 'APP_VERSION = "2.6.4"')
text = text.replace('APP_VERSION = "0.4.5"', 'APP_VERSION = "2.6.4"')
text = text.replace('APP_VERSION = "2.6.1"', 'APP_VERSION = "2.6.4"')
text = text.replace('APP_VERSION = "2.6.2"', 'APP_VERSION = "2.6.4"')
text = text.replace('APP_VERSION = "2.6.3"', 'APP_VERSION = "2.6.4"')
text = text.replace('APP_VERSION = "2.6.4"', 'APP_VERSION = "2.6.5"')
text = text.replace('APP_VERSION = "2.6.5"', 'APP_VERSION = "2.6.6"')
text = text.replace('APP_VERSION = "2.6.6"', 'APP_VERSION = "2.6.7"')
text = text.replace('APP_VERSION = "2.6.7"', 'APP_VERSION = "2.6.8"')
text = text.replace('APP_VERSION = "2.6.8"', 'APP_VERSION = "2.6.9"')
meta.write_text(text, encoding="utf-8")

launch_py = launcher / "launcher.py"
text = launch_py.read_text(encoding="utf-8")
old = '"jvmArguments": [f"-Xmx{max_mem_mb}M"],'
new = '''"jvmArguments": [f"-Xmx{max_mem_mb}M", f"-javaagent:{os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(__file__)), 'assets', 'girinworld-core-26.2.jar')}"],'''
if old not in text:
    raise SystemExit("launcher.py JVM argument marker not found")
text = text.replace(old, new)
marker = "    return _command.get_minecraft_command(\n"
cleanup = """    # Load the Girinworld client core as a Java agent without the temporary Fabric replacement.
    mods_dir = os.path.join(instance.instance_dir(), "mods")
    if os.path.isdir(mods_dir):
        for filename in os.listdir(mods_dir):
            if filename.lower().startswith("yoonseul-core-fabric-") and filename.lower().endswith(".jar"):
                try:
                    os.remove(os.path.join(mods_dir, filename))
                except OSError:
                    pass

"""
if marker not in text:
    raise SystemExit("launcher.py command marker not found")
text = text.replace(marker, cleanup + marker, 1)
launch_py.write_text(text, encoding="utf-8")

html = launcher / "frontend" / "nether-glass-launcher-standalone.html"
text = html.read_text(encoding="utf-8")
text = text.replace("윤슬 런처 0.3.9", "윤슬 런처 2.6.4")
text = text.replace("윤슬 런처 0.4.5", "윤슬 런처 2.6.4")
text = text.replace("윤슬 런처 2.6.1", "윤슬 런처 2.6.4")
text = text.replace("윤슬 런처 2.6.2", "윤슬 런처 2.6.4")
text = text.replace("윤슬 런처 2.6.3", "윤슬 런처 2.6.4")
text = text.replace("윤슬 런처 2.6.4", "윤슬 런처 2.6.5")
text = text.replace("윤슬 런처 2.6.5", "윤슬 런처 2.6.6")
text = text.replace("윤슬 런처 2.6.6", "윤슬 런처 2.6.7")
text = text.replace("윤슬 런처 2.6.7", "윤슬 런처 2.6.8")
text = text.replace("윤슬 런처 2.6.8", "윤슬 런처 2.6.9")
html.write_text(text, encoding="utf-8")
