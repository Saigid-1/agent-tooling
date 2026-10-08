import json, os, re
from pathlib import Path
from setuptools import setup
from setuptools.command.build_py import build_py
class Build(build_py):
    def run(self):
        super().run()
        revision = os.environ.get("SOURCE_REVISION", "")
        target = Path(self.build_lib) / "kp_agent_tooling/_impl/build_identity.json"
        if re.fullmatch(r"[0-9a-f]{40}", revision):
            target.write_text(json.dumps({"source_revision":revision}))
        else:
            target.unlink(missing_ok=True)
setup(cmdclass={"build_py":Build})
