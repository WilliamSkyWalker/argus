"""Install the Saygo Agent integration from a wheel or source checkout.

The wheel carries the same allowlisted source used by release installers. Setup
uses that exact version, never a mutable branch or an unverified GitHub download.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


@contextmanager
def installation_source():
    package = Path(__file__).resolve().parent
    archive = package / '_setup_source.zip'
    if archive.is_file():
        with tempfile.TemporaryDirectory(prefix='saygo-setup-') as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if not (root / member.filename).resolve().is_relative_to(root):
                        raise ValueError('Unsafe path in packaged installation source')
                bundle.extractall(root)
            yield root
    else:
        root = package.parent
        if not (root / 'scripts/install_agent_plugin.py').is_file():
            raise ValueError('Installation resources are missing; reinstall saygo-agent-control')
        yield root


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        with installation_source() as source:
            spec = importlib.util.spec_from_file_location('saygo_setup_installer', source / 'scripts/install_agent_plugin.py')
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            # Explicit developer sources/archives are still validated by the installer.
            explicit = any(arg in ('--source', '--archive') or arg.startswith(('--source=', '--archive='))
                           for arg in arguments)
            return installer.main(arguments if explicit else ['--source', str(source), *arguments])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Saygo setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
