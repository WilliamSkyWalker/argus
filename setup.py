"""Include the managed installer's allowlisted source in installed wheels."""
import importlib.util
from pathlib import Path
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSetupSource(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location('saygo_release_builder', root / 'scripts/build_release.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        destination = Path(self.build_lib) / 'saygo' / '_setup_source.zip'
        builder.archive(destination, {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in builder.source_files()
        })


setup(cmdclass={'build_py': BuildWithSetupSource})
